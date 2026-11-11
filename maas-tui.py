#!/usr/bin/env python3
# ruff: noqa: BLE001, S110
"""Live view of the MaaS, driven like real clients.

Pick a set of simulated users, each with a request type, then watch the
gateway serve them: requests per replica, tokens counted by Limitador against
each user's subscription budget, and 429s once a budget runs out.

    ./maas-tui.py [namespace]     # uses the current oc login

The three stress subscriptions are fixed (tui-chat, tui-tool-call,
tui-research, one per request type); create them once with
playbooks/admin.yml -e maas_action=tui-setup. Each session then mints one key
per simulated user under a fresh identity (tui-<session>-<n>), so counters
start at zero, and sends that user's traffic with its key through the real
gateway path. On exit the keys are revoked.

The load generator runs inside the maas-client pod (one `oc exec`, keys passed
on stdin); maas-api is reached with a port-forward to a gateway pod's
localhost:8090.
"""
import curses
import json
import locale
import random
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

NS = sys.argv[1] if len(sys.argv) > 1 else "uat-project"
CLIENT = "maas-client"
WINDOW = 60  # seconds per budget window
TYPES = ["chat", "tool-call", "research"]
# Fixed token budget per 60-second window for each type's subscription.
LIMITS = {"chat": 10_000, "tool-call": 100_000, "research": 30_000}
SUBSCRIPTION = {t: f"tui-{t}" for t in TYPES}
MODEL = "maas-sim"
PUBLISHER = f"publishers/{NS}/models/{MODEL}"
ADMIN_GROUP = "maas-local-admin"
# The variable the governance role writes into every token limit.
VARIABLE = 'descriptors[0]["auth.identity.userid"]'

# Runs in maas-client. Standard library only.
GENERATOR = r'''
import json, random, sys, threading, time, urllib.error, urllib.request
cfg = json.loads(sys.stdin.readline())
lock = threading.Lock()
def emit(o):
    with lock:
        sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
def get(url):
    return json.loads(urllib.request.urlopen(url, timeout=5).read())
WORDS = ("model token budget gateway replica request prompt answer cache prefix "
         "limit counter window user router envoy picker queue latency stream").split()
# Per type: words in a per-user prefix, random words after it, max_tokens.
PROFILE = {"chat": (300, 20, 64), "tool-call": (0, 2000, 32), "research": (0, 500, 256)}
def run(u, stop):
    rng = random.Random(u["name"])
    pre_n, rand_n, max_tokens = PROFILE[u["type"]]
    prefix = " ".join(rng.choice(WORDS) for _ in range(pre_n))
    while time.time() < stop:
        text = (prefix + " " + " ".join(rng.choice(WORDS) for _ in range(rand_n))).strip()
        body = json.dumps({"model": cfg["model"], "max_tokens": max_tokens,
                           "messages": [{"role": "user", "content": text}]}).encode()
        req = urllib.request.Request(cfg["url"], data=body, headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + u["key"]})
        t0, up, tokens = time.time(), "", 0
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                status, up = r.status, r.headers.get("x-maas-upstream", "")
                tokens = json.loads(r.read()).get("usage", {}).get("total_tokens", 0)
        except urllib.error.HTTPError as e:
            status, up = e.code, e.headers.get("x-maas-upstream", "")
            e.read()
        except Exception:
            status = 0
        emit({"t": "req", "user": u["name"], "status": status, "up": up,
              "tokens": tokens, "ms": int((time.time() - t0) * 1000)})
        time.sleep(1.0 if status == 429 else rng.uniform(0.1, 0.5))
def poll():
    try:
        emit({"t": "counters", "data": get(cfg["counters"])})
    except Exception as e:
        emit({"t": "log", "msg": "counters: %s" % e})
finished = threading.Event()
def counters():
    while not finished.wait(1):
        poll()
stop = time.time() + cfg["duration"]
users = [threading.Thread(target=run, args=(u, stop)) for u in cfg["users"]]
watcher = threading.Thread(target=counters)
for t in users + [watcher]: t.start()
for t in users: t.join()
# One last read once every request, including long ones, has been counted.
finished.set(); watcher.join(); time.sleep(1); poll()
emit({"t": "done"})
'''

BASE_CFG = {"url": "http://maas-gateway:8080/v1/chat/completions",
            "limitador": "http://maas-limitador:8080",
            "counters": "http://maas-limitador:8080/counters/" + urllib.parse.quote(f"{NS}/{MODEL}", safe=""),
            "model": PUBLISHER}


def oc(*args, input=None):
    r = subprocess.run(["oc", "-n", NS, *args], input=input, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"oc {' '.join(args[:3])}: {r.stderr.strip()}")
    return r.stdout


GEN_CMD = ["oc", "-n", NS, "exec", "-i", CLIENT, "--", "python3", "-u", "-c", GENERATOR]


class MaasAPI:
    """maas-api on a gateway pod's localhost, through a port-forward held for
    the TUI's life. Identity comes from the headers Authorino would set."""

    def __init__(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.pf = subprocess.Popen(["oc", "-n", NS, "port-forward", "deploy/maas-gateway", f"{self.port}:8090"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2)
                return
            except Exception:
                time.sleep(0.5)
        self.close()
        raise RuntimeError("can't reach maas-api through a port-forward")

    def call(self, method, path, username, groups, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     method=method,
                                     headers={"X-MaaS-Username": username,
                                              "X-MaaS-Group": json.dumps(groups),
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"maas-api {path}: {e.code} {e.read().decode().strip()[:160]}")

    def close(self):
        if self.pf.poll() is None:
            self.pf.terminate()
            self.pf.wait()


def random_user(name, rng):
    t = rng.choice(TYPES)
    return {"name": name, "type": t, "budget": LIMITS[t], "on": True}


class Session:
    """State for one run, filled in from the generator's output."""

    def __init__(self, users, duration, api):
        self.users = users
        self.duration = duration
        self.api = api
        self.sid = f"{random.getrandbits(24):06x}"
        for n, u in enumerate(users, 1):
            u["identity"] = f"tui-{self.sid}-{n}"
            u["sub"] = SUBSCRIPTION[u["type"]]
            u["budget"] = LIMITS[u["type"]]
        self.by_identity = {u["identity"]: u["name"] for u in users}
        self.cleaned = False
        self.stats = {u["name"]: {"sent": 0, "ok": 0, "r429": 0, "err": 0, "tokens": 0,
                                  "ms": 0, "pods": {}} for u in users}
        self.counters = {}
        self.pods = {}  # ip -> pod name
        self.status = "starting"
        self.log = []
        self.start = None
        self.done = False
        self.proc = None
        self.cancelled = False
        self.lock = threading.Lock()

    def note(self, msg):
        with self.lock:
            self.status = msg
            self.log = (self.log + [msg])[-3:]

    def feed(self, line):
        try:
            m = json.loads(line)
        except ValueError:
            self.note(line.strip()[:120])
            return
        with self.lock:
            if m["t"] == "req":
                s = self.stats[m["user"]]
                s["sent"] += 1
                s["ms"] += m["ms"]
                if m["status"] == 200:
                    s["ok"] += 1
                    s["tokens"] += m["tokens"]
                elif m["status"] == 429:
                    s["r429"] += 1
                else:
                    s["err"] += 1
                if m["up"]:
                    pod = self.pods.get(m["up"].rsplit(":", 1)[0], m["up"])
                    s["pods"][pod] = s["pods"].get(pod, 0) + 1
            elif m["t"] == "counters":
                counters = {}
                for c in m["data"]:
                    identity = (c.get("set_variables") or {}).get(VARIABLE)
                    user = self.by_identity.get(identity)
                    if user:
                        rem = c["remaining"]
                        if rem >= 2**63:  # Limitador reports overshoot as a wrapped u64
                            rem -= 2**64
                        used = c["limit"]["max_value"] - rem
                        if user not in counters or used > counters[user][0]:
                            counters[user] = (used, c.get("expires_in_seconds"))
                self.counters = counters
            elif m["t"] == "done":
                self.done = True
                self.status = "finished"

    def cleanup(self):
        """Revoke this session's keys (bulk revoke per fresh identity)."""
        if self.cleaned:
            return
        self.cleaned = True
        for u in self.users:
            try:
                self.api.call("POST", "/v1/api-keys/bulk-revoke", "maas-tui-admin",
                              [ADMIN_GROUP], {"username": u["identity"]})
            except Exception:
                pass


def prepare(sess):
    """Mint the session's keys, then start traffic."""
    try:
        keys = {}
        for n, u in enumerate(sess.users, 1):
            sess.note(f"minting a key for {u['identity']} ({n}/{len(sess.users)})")
            r = sess.api.call("POST", "/v1/api-keys", u["identity"], [u["sub"]],
                              {"name": u["name"], "expiresIn": "24h", "subscription": u["sub"]})
            keys[u["name"]] = r["key"]
            if sess.cancelled:
                return
        pods = json.loads(oc("get", "pods", "-l", "app=maas-sim", "-o", "json"))["items"]
        with sess.lock:
            sess.pods = {p["status"].get("podIP"): p["metadata"]["name"] for p in pods}
        if sess.cancelled:
            return
        sess.note(f"running session {sess.sid}")
        cfg = {**BASE_CFG, "duration": sess.duration,
               "users": [{"name": u["name"], "type": u["type"], "key": keys[u["name"]]} for u in sess.users]}
        sess.proc = subprocess.Popen(GEN_CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
        sess.proc.stdin.write(json.dumps(cfg) + "\n")
        sess.proc.stdin.close()
        keys.clear()
        sess.start = time.time()
        for line in sess.proc.stdout:
            sess.feed(line)
        sess.proc.wait()
        with sess.lock:
            sess.done = True
            if sess.status.startswith("running"):
                sess.status = f"generator exited ({sess.proc.returncode})"
    except Exception as e:
        with sess.lock:
            sess.done = True
            sess.status = f"error: {e}"


def put(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h and x < w:
        try:
            win.addnstr(y, x, text, w - x - 1, attr)
        except curses.error:
            pass


def bar(frac, width):
    frac = max(0.0, frac)
    n = min(width, round(min(frac, 1.0) * width))
    return "█" * n + "·" * (width - n)


def setup_screen(scr, users, duration, rng):
    """Returns (users, duration) to run, or None to quit."""
    sel = 0
    while True:
        scr.erase()
        put(scr, 0, 0, "MaaS test · " + NS + " · pick users", curses.A_BOLD)
        put(scr, 1, 0, f"budget window {WINDOW}s · run for {duration}s")
        put(scr, 3, 0, f"   {'USER':<8}{'TYPE':<10}{'BUDGET/WINDOW':>14}{'SUBSCRIPTION':>16}", curses.A_UNDERLINE)
        for i, u in enumerate(users):
            mark = "[x]" if u["on"] else "[ ]"
            attr = curses.A_REVERSE if i == sel else 0
            put(scr, 4 + i, 0, f"{mark} {u['name']:<8}{u['type']:<10}{LIMITS[u['type']]:>12,}{SUBSCRIPTION[u['type']]:>16}", attr)
        y = 5 + len(users)
        put(scr, y, 0, "↑↓ move  space on/off  t type  r reroll types  +/- users", curses.A_DIM)
        put(scr, y + 1, 0, "[ ] duration -/+30s  enter start  q quit", curses.A_DIM)
        put(scr, y + 2, 0, "create the stress subscriptions once: admin.yml -e maas_action=tui-setup", curses.A_DIM)
        k = scr.getch()
        if k in (ord("q"), 27):
            return None
        if k in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            sel = min(len(users) - 1, sel + 1)
        elif k == ord(" "):
            users[sel]["on"] = not users[sel]["on"]
        elif k == ord("t"):
            old = users[sel]["type"]
            new = TYPES[(TYPES.index(old) + 1) % len(TYPES)]
            users[sel]["type"] = new
            users[sel]["budget"] = LIMITS[new]
        elif k == ord("r"):
            users[:] = [random_user(u["name"], rng) for u in users]
        elif k == ord("+") and len(users) < 20:
            users.append(random_user(f"user{len(users) + 1:02d}", rng))
        elif k == ord("-") and len(users) > 1:
            users.pop()
            sel = min(sel, len(users) - 1)
        elif k == ord("["):
            duration = max(30, duration - 30)
        elif k == ord("]"):
            duration = min(600, duration + 30)
        elif k in (10, 13, curses.KEY_ENTER):
            chosen = [u for u in users if u["on"]]
            if chosen:
                return chosen, duration


def run_screen(scr, sess):
    """Returns True to start a new session, False to quit."""
    scr.nodelay(True)
    try:
        while True:
            with sess.lock:
                draw_run(scr, sess)
            k = scr.getch()
            if k in (ord("q"), 27):
                return False
            if k == ord("n") and sess.done:
                return True
            time.sleep(0.25)
    finally:
        scr.nodelay(False)
        sess.cancelled = True
        if sess.proc and sess.proc.poll() is None:
            sess.proc.terminate()
        scr.erase()
        put(scr, 0, 0, "revoking this session's keys ...")
        scr.refresh()
        sess.cleanup()


def draw_run(scr, sess):
    scr.erase()
    pods = sorted(set(sess.pods.values()) | {p for s in sess.stats.values() for p in s["pods"]})
    short = {p: p.rsplit("-", 1)[-1] for p in pods}
    elapsed = int(time.time() - sess.start) if sess.start else 0
    if sess.done:
        elapsed = min(elapsed, sess.duration)
    put(scr, 0, 0, f"MaaS test · {sess.status}", curses.A_BOLD)
    put(scr, 1, 0, f"{bar(elapsed / sess.duration, 30)} {elapsed}/{sess.duration}s · budget window {WINDOW}s")
    hdr = (f"{'USER':<8}{'TYPE':<9}{'SENT':>5}{'OK':>5}{'429':>5}{'ERR':>4}{'AVG ms':>7}"
           f"{'TOKENS':>9}  {'LIMITADOR used/max':<34}")
    hdr += "".join(f"{short[p]:>7}" for p in pods)
    put(scr, 3, 0, hdr, curses.A_UNDERLINE)
    for i, u in enumerate(sess.users):
        s = sess.stats[u["name"]]
        used, expires = sess.counters.get(u["name"], (0, None))
        frac = used / u["budget"]
        lim = f"{bar(frac, 12)} {used:>7,}/{u['budget']:<7,}"
        lim += f" {expires:>2}s" if expires is not None else "    "
        avg = s["ms"] // s["sent"] if s["sent"] else 0
        y = 4 + i
        put(scr, y, 0, f"{u['name']:<8}{u['type']:<9}{s['sent']:>5}")
        put(scr, y, 22, f"{s['ok']:>5}", curses.color_pair(1))
        put(scr, y, 27, f"{s['r429']:>5}", curses.color_pair(2) if s["r429"] else 0)
        put(scr, y, 32, f"{s['err']:>4}{avg:>7}{s['tokens']:>9,}  ")
        put(scr, y, 54, lim, curses.color_pair(2) if frac >= 1 else curses.color_pair(3))
        served = sum(s["pods"].values())
        for j, p in enumerate(pods):
            pct = 100 * s["pods"].get(p, 0) / served if served else 0
            put(scr, y, 88 + 7 * j, f"{pct:>6.0f}%")
    y = 5 + len(sess.users)
    total = {p: sum(s["pods"].get(p, 0) for s in sess.stats.values()) for p in pods}
    alln = sum(total.values())
    put(scr, y, 0, "all requests by replica:", curses.A_BOLD)
    for j, p in enumerate(pods):
        put(scr, y + 1 + j, 2, f"{p:<32} {bar(total[p] / alln if alln else 0, 20)} {total[p]:>5}")
    y += 2 + len(pods)
    for j, msg in enumerate(sess.log[-2:]):
        put(scr, y + j, 0, msg, curses.A_DIM)
    put(scr, y + 3, 0, ("n new session  " if sess.done else "") + "q quit (keys revoked)", curses.A_DIM)


def main(scr):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_CYAN, -1)
    rng = random.Random()
    users = [random_user(f"user{i:02d}", rng) for i in range(1, 7)]
    duration = 120
    api = MaasAPI()
    try:
        while True:
            picked = setup_screen(scr, users, duration, rng)
            if not picked:
                return
            chosen, duration = picked
            sess = Session([dict(u) for u in chosen], duration, api)
            threading.Thread(target=prepare, args=(sess,), daemon=True).start()
            if not run_screen(scr, sess):
                return
    finally:
        api.close()


if __name__ == "__main__":
    try:
        subprocess.run(["oc", "whoami"], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        sys.exit("needs a working oc login")
    locale.setlocale(locale.LC_ALL, "")
    # A closed terminal or a kill runs the same cleanup as Ctrl-C: revoke the
    # session's keys.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(1))
    try:
        curses.wrapper(main)
    except SystemExit:
        pass
    print("session keys revoked")
