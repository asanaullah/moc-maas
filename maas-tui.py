#!/usr/bin/env python3
"""Live view of the MaaS, driven like real clients.

Pick a set of simulated users, each with a random request type and token
budget, then watch the gateway serve them: requests per replica, tokens
counted by Limitador against each user's budget, and 429s once a budget runs
out.

    ./maas-tui.py [namespace]     # uses the current oc login

Each session creates, through the admin portal's API, one plan per simulated
user (its budget, model maas-sim) and one service key on it, and sends that
user's traffic with its key through the real gateway path. Session names are
unique, so counters start fresh. On exit the keys are revoked and the plans
deleted; the admin portal removes their limits from Limitador.

The load generator runs inside the maas-client pod (one `oc exec`, keys passed
on stdin); the admin API is reached with a port-forward.
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
import urllib.request

NS = sys.argv[1] if len(sys.argv) > 1 else "uat-project"
CLIENT = "maas-client"
WINDOW = 60  # seconds per budget window
TYPES = ["chat", "tool-call", "research"]
# Budget range per type, in tokens per window.
BUDGETS = {"chat": (5_000, 20_000), "tool-call": (40_000, 200_000), "research": (15_000, 50_000)}

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
if cfg["mode"] == "limits":
    emit(get(cfg["limitador"] + "/limits/maas")); sys.exit(0)
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
        emit({"t": "counters", "data": get(cfg["limitador"] + "/counters/maas")})
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
            "limitador": "http://maas-limitador:8080", "model": "maas-sim"}


def oc(*args, input=None):
    r = subprocess.run(["oc", "-n", NS, *args], input=input, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"oc {' '.join(args[:3])}: {r.stderr.strip()}")
    return r.stdout


GEN_CMD = ["oc", "-n", NS, "exec", "-i", CLIENT, "--", "python3", "-u", "-c", GENERATOR]


def run_gen(cfg):
    """Runs the generator once (for the "limits" mode) and returns its output."""
    r = subprocess.run(GEN_CMD, input=json.dumps(cfg) + "\n", capture_output=True, text=True)
    return r.stdout


class AdminAPI:
    """The admin portal's JSON API, through a port-forward held for the TUI's life."""

    def __init__(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.pf = subprocess.Popen(["oc", "-n", NS, "port-forward", "deploy/maas-admin", f"{self.port}:8080"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=2)
                return
            except Exception:
                time.sleep(0.5)
        self.close()
        raise RuntimeError("can't reach the admin portal through a port-forward")

    def call(self, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-MaaS-Admin": "1", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"admin API {path}: {e.code} {e.read().decode().strip()[:120]}")

    def close(self):
        if self.pf.poll() is None:
            self.pf.terminate()
            self.pf.wait()


def random_user(name, rng):
    t = rng.choice(TYPES)
    lo, hi = BUDGETS[t]
    return {"name": name, "type": t, "budget": rng.randrange(lo, hi + 1, 1000), "on": True}


def limit_plan(limit):
    for c in limit.get("conditions", []):
        if c.startswith("descriptors[0]['plan'] == '"):
            return c.split("'")[3]
    return None


class Session:
    """State for one run, filled in from the generator's output."""

    def __init__(self, users, duration, api):
        self.users = users
        self.duration = duration
        self.api = api
        self.sid = "%06x" % random.getrandbits(24)
        for u in users:
            u["plan"] = f"tui-{self.sid}-{u['name']}"
        self.plan_user = {u["plan"]: u["name"] for u in users}
        self.key_ids = []
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
                self.counters = {}
                for c in m["data"]:
                    user = self.plan_user.get(limit_plan(c["limit"]))
                    if user:
                        rem = c["remaining"]
                        if rem >= 2**63:  # Limitador reports overshoot as a wrapped u64
                            rem -= 2**64
                        self.counters[user] = (c["limit"]["max_value"] - rem, c["expires_in_seconds"])
            elif m["t"] == "done":
                self.done = True
                self.status = "finished"

    def cleanup(self):
        """Revoke this session's keys, then delete its plans (and so their limits)."""
        if self.cleaned:
            return
        self.cleaned = True
        for kid in self.key_ids:
            try:
                self.api.call("keys/revoke", {"id": kid})
            except Exception:
                pass
        for u in self.users:
            try:
                self.api.call("plans/delete", {"name": u["plan"]})
            except Exception:
                pass


def prepare(sess):
    """Create the session's plans and keys, wait for Limitador, then start traffic."""
    try:
        keys = {}
        for n, u in enumerate(sess.users, 1):
            sess.note(f"creating plan and key for {u['name']} ({n}/{len(sess.users)})")
            sess.api.call("plans", {"name": u["plan"], "tokens": u["budget"], "window": WINDOW,
                                    "days": 1, "models": [BASE_CFG["model"]]})
            r = sess.api.call("service-keys", {"owner": f"maas-tui-{sess.sid}-{u['name']}",
                                               "name": u["name"], "plan": u["plan"], "days": 1})
            sess.key_ids.append(r["id"])
            keys[u["name"]] = r["key"]
            if sess.cancelled:
                return
        want = {u["plan"] for u in sess.users}
        t0 = time.time()
        while True:
            loaded = json.loads(run_gen({**BASE_CFG, "mode": "limits"}) or "[]")
            if want <= {limit_plan(lim) for lim in loaded}:
                break
            if time.time() - t0 > 180:
                raise RuntimeError("Limitador did not load the session's limits within 180s")
            if sess.cancelled:
                return
            sess.note(f"waiting for Limitador to load the session's limits ({int(time.time() - t0)}s)")
            time.sleep(3)
        pods = json.loads(oc("get", "pods", "-l", "app=maas-sim", "-o", "json"))["items"]
        with sess.lock:
            sess.pods = {p["status"].get("podIP"): p["metadata"]["name"] for p in pods}
        if sess.cancelled:
            return
        sess.note(f"running session {sess.sid}")
        cfg = {**BASE_CFG, "mode": "run", "duration": sess.duration,
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
    n = min(width, int(round(min(frac, 1.0) * width)))
    return "█" * n + "·" * (width - n)


def setup_screen(scr, users, duration, rng):
    """Returns (users, duration) to run, or None to quit."""
    sel = 0
    while True:
        scr.erase()
        put(scr, 0, 0, "MaaS test · " + NS + " · pick users", curses.A_BOLD)
        put(scr, 1, 0, f"budget window {WINDOW}s · run for {duration}s")
        put(scr, 3, 0, f"   {'USER':<8}{'TYPE':<10}{'BUDGET/WINDOW':>14}", curses.A_UNDERLINE)
        for i, u in enumerate(users):
            mark = "[x]" if u["on"] else "[ ]"
            attr = curses.A_REVERSE if i == sel else 0
            put(scr, 4 + i, 0, f"{mark} {u['name']:<8}{u['type']:<10}{u['budget']:>12,}", attr)
        y = 5 + len(users)
        put(scr, y, 0, "↑↓ move  space on/off  t type  b budget  r reroll all  +/- users", curses.A_DIM)
        put(scr, y + 1, 0, "[ ] duration -/+30s  enter start  q quit", curses.A_DIM)
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
            u = users[sel]
            u["type"] = TYPES[(TYPES.index(u["type"]) + 1) % len(TYPES)]
            u["budget"] = rng.randrange(BUDGETS[u["type"]][0], BUDGETS[u["type"]][1] + 1, 1000)
        elif k == ord("b"):
            u = users[sel]
            u["budget"] = rng.randrange(BUDGETS[u["type"]][0], BUDGETS[u["type"]][1] + 1, 1000)
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
        put(scr, 0, 0, "revoking this session's keys and deleting its plans ...")
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
    put(scr, y + 3, 0, ("n new session  " if sess.done else "") + "q quit (keys revoked, plans deleted)", curses.A_DIM)


def main(scr):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_CYAN, -1)
    rng = random.Random()
    users = [random_user(f"user{i:02d}", rng) for i in range(1, 7)]
    duration = 120
    api = AdminAPI()
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
    # session's keys and delete its plans.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(1))
    try:
        curses.wrapper(main)
    except SystemExit:
        pass
    print("session keys revoked and session plans deleted")
