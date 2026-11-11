#!/usr/bin/env python3
# ruff: noqa: BLE001, S110, PLR0124, ISC004, EXE001
"""The MaaS usage page.

Users sign in through the MaaS's own Keycloak (OIDC, brokered to GitHub) and
see only their own usage: this window against each subscription's budget (live
from Limitador), the last 24 hours and the last 30 days (from Thanos Query).
Every query is built here and fixed to the signed-in user; nothing the browser
sends ends up in a query.

A second port, 9100, serves the storage volume's gauge metrics for Prometheus.
"""
import base64
import hashlib
import html
import http.server
import json
import os
import secrets
import signal
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

MODE = os.environ.get("MODE", "usage")
DATABASE_URL = os.environ["DATABASE_URL"]
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
try:
    with open(f"{SA_DIR}/namespace") as _f:
        NAMESPACE = _f.read().strip()
except OSError:
    NAMESPACE = "uat-project"
GOVERNANCE_FILE = os.environ.get("GOVERNANCE_FILE", "/governance/objects.json")
ROUTE_HOST = os.environ.get("ROUTE_HOST", "")
# The realm's public issuer, as browsers and tokens see it, and the same realm
# through the Service name, for the page's own (backchannel) requests.
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "")
OIDC_BACKCHANNEL = os.environ.get("OIDC_BACKCHANNEL", "")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "maas-usage")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
SERVICE_CA = f"{SA_DIR}/service-ca.crt"
THANOS_URL = os.environ.get("THANOS_URL", "http://maas-thanos-query:9090")
LIMITADOR_URL = os.environ.get("LIMITADOR_URL", "http://maas-limitador:8080")
MAASAPI_PASSWORD = os.environ.get("MAASAPI_PASSWORD", "")
SESSION_HOURS = 8
OIDC = {}

# The variable string the governance role writes into every token limit.
VARIABLE = 'descriptors[0]["auth.identity.userid"]'

MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS sessions (
      id_hash text PRIMARY KEY,
      username text NOT NULL,
      identity text,
      full_name text NOT NULL DEFAULT '',
      groups text[] NOT NULL,
      csrf text NOT NULL,
      expires_at timestamptz NOT NULL);
    CREATE TABLE IF NOT EXISTS oauth_pending (
      state text PRIMARY KEY,
      verifier text NOT NULL,
      nonce text,
      expires_at timestamptz NOT NULL);
    CREATE TABLE IF NOT EXISTS audit (
      id bigserial PRIMARY KEY,
      at timestamptz NOT NULL DEFAULT now(),
      actor text NOT NULL,
      action text NOT NULL,
      target text NOT NULL DEFAULT '',
      details jsonb NOT NULL DEFAULT '{}');
    """,
    "ALTER TABLE oauth_pending ADD COLUMN IF NOT EXISTS browser_hash text;",
]


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, file=sys.stderr, flush=True)


POOL = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, timeout=1,
                      kwargs={"autocommit": True}, open=False)


def db(sql, args=(), fetch=False):
    with POOL.connection() as conn:
        cur = conn.execute(sql, args)
        return cur.fetchall() if fetch else None


def audit(actor, action, target="", **details):
    db("INSERT INTO audit (actor, action, target, details) VALUES (%s, %s, %s, %s)",
       (actor, action, target, Jsonb(details)))


SCHEMA_LOCK = 0x6D616173  # 'maas': concurrent pods serialize on this


def wait_for_db():
    """Apply pending schema migrations, each exactly once."""
    while True:
        try:
            if db("SELECT to_regclass('schema_version')", fetch=True)[0][0] is None:
                db("CREATE TABLE schema_version (version integer PRIMARY KEY, "
                   "applied_at timestamptz NOT NULL DEFAULT now())")
            for version, sql in enumerate(MIGRATIONS):
                if db("SELECT 1 FROM schema_version WHERE version = %s", (version,), fetch=True):
                    continue
                with POOL.connection() as conn, conn.transaction():
                    conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
                    conn.execute("SET LOCAL lock_timeout = '1s'")
                    if not conn.execute("SELECT 1 FROM schema_version WHERE version = %s",
                                        (version,)).fetchone():
                        conn.execute(sql)
                        conn.execute("INSERT INTO schema_version (version) VALUES (%s)", (version,))
            return
        except Exception as e:  # database not up yet, or a lock timed out
            log("waiting for the database:", type(e).__name__, str(e).splitlines()[0][:120])
            time.sleep(3)


def hash_value(v):
    return hashlib.sha256(v.encode()).hexdigest()


def esc(v):
    return html.escape(str(v))


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>
body{{font:14px system-ui,sans-serif;margin:2em auto;max-width:64em;padding:0 1em;color:#222}}
table{{border-collapse:collapse;width:100%;margin:.5em 0 1.5em}}
th,td{{border-bottom:1px solid #ddd;padding:.35em .5em;text-align:left;vertical-align:top}}
th{{background:#f4f4f4}} form.inline{{display:inline}} input,select{{padding:.25em}}
.key{{font:13px monospace;background:#eef6ee;border:1px solid #9c9;padding:.6em;word-break:break-all}}
.muted{{color:#777}} .err{{color:#a00}} code{{font-size:12px}}
</style></head><body><h1>{title}</h1>{body}</body></html>"""


class Base(http.server.BaseHTTPRequestHandler):
    server_version = "maas-usage"

    def log_message(self, fmt, *args):
        log(self.address_string(), fmt % args)

    def send(self, code, body, ctype="text/html; charset=utf-8", headers=()):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def page(self, title, body, code=200, headers=()):
        self.send(code, PAGE.format(title=esc(title), body=body), headers=headers)

    def redirect(self, location, headers=()):
        self.send_response(303)
        self.send_header("Location", location)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def form(self):
        try:
            n = min(self.content_length(), 64 * 1024)
        except ValueError:
            return {}
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}

    def content_length(self):
        n = self.headers.get("Content-Length")
        try:
            n = int(n or 0)
        except ValueError:
            raise ValueError("bad Content-Length")
        if n < 0:
            raise ValueError("bad Content-Length")
        return n

    def cookie_value(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def healthz(self):
        try:
            db("SELECT 1")
            self.send(200, "ok", "text/plain")
        except Exception as e:
            self.send(503, f"database: {type(e).__name__}", "text/plain")

    def livez(self):
        self.send(200, "ok", "text/plain")


def oidc_ctx():
    return ssl.create_default_context(cafile=SERVICE_CA)


def discover_oidc():
    """Endpoints from the realm's discovery document, fetched over the Service."""
    url = OIDC_BACKCHANNEL.rstrip("/") + "/.well-known/openid-configuration"
    with urllib.request.urlopen(url, context=oidc_ctx(), timeout=10) as r:
        OIDC.update(json.loads(r.read()))
    if OIDC["issuer"] != OIDC_ISSUER:
        raise RuntimeError(f"realm issuer is {OIDC['issuer']}, expected {OIDC_ISSUER}")
    log("OIDC issuer:", OIDC["issuer"])


def redirect_uri():
    return f"https://{ROUTE_HOST}/callback"


def b64url_json(part):
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def exchange_code(code, verifier, nonce):
    """Trade the code for tokens and return the ID token's claims."""
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
        "code_verifier": verifier}).encode()
    basic = base64.b64encode(f"{OIDC_CLIENT_ID}:{OIDC_CLIENT_SECRET}".encode()).decode()
    req = urllib.request.Request(OIDC["token_endpoint"], data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
        "Authorization": "Basic " + basic})
    with urllib.request.urlopen(req, context=oidc_ctx(), timeout=15) as r:
        tokens = json.loads(r.read())
    claims = b64url_json(tokens["id_token"].split(".")[1])
    aud = claims.get("aud")
    if claims.get("iss") != OIDC_ISSUER:
        raise ValueError("ID token from an unexpected issuer")
    if OIDC_CLIENT_ID not in (aud if isinstance(aud, list) else [aud]):
        raise ValueError("ID token not issued to this page")
    if claims.get("exp", 0) < time.time():
        raise ValueError("ID token expired")
    if not nonce or not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ValueError("ID token nonce doesn't match this sign-in")
    return claims


def username_from_claims(claims):
    """The user's identity everywhere: the Keycloak username."""
    username = claims.get("preferred_username")
    if not username:
        raise ValueError("the token has no preferred_username")
    return username


def groups_from_claims(claims):
    groups = claims.get("groups") or []
    return [groups] if isinstance(groups, str) else groups


class SignIn(Base):
    def session(self):
        sid = self.cookie_value("__Host-maas_session")
        if not sid:
            return None, None
        h = hash_value(sid)
        rows = db("SELECT username, identity, full_name, groups, csrf FROM sessions "
                  "WHERE id_hash = %s AND expires_at > now()", (h,), fetch=True)
        if not rows:
            return None, None
        u, ident, name, groups, csrf = rows[0]
        return h, {"username": u, "identity": ident, "name": name, "groups": groups, "csrf": csrf}

    def login(self):
        state, verifier, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(48), secrets.token_urlsafe(24)
        browser = secrets.token_urlsafe(24)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        db("INSERT INTO oauth_pending (state, verifier, nonce, browser_hash, expires_at) "
           "VALUES (%s, %s, %s, %s, now() + interval '10 minutes')", (state, verifier, nonce, hash_value(browser)))
        q = urllib.parse.urlencode({"client_id": OIDC_CLIENT_ID, "response_type": "code",
                                    "scope": "openid profile", "redirect_uri": redirect_uri(),
                                    "state": state, "nonce": nonce, "kc_idp_hint": "github",
                                    "code_challenge": challenge, "code_challenge_method": "S256"})
        self.redirect(OIDC["authorization_endpoint"] + "?" + q,
                      headers=[("Set-Cookie", f"__Host-maas_login={browser}; Path=/; Secure; HttpOnly; "
                                               "SameSite=Lax; Max-Age=600")])

    def callback(self, q):
        state, code = (q.get("state") or [""])[0], (q.get("code") or [""])[0]
        browser = self.cookie_value("__Host-maas_login") or ""
        rows = db("DELETE FROM oauth_pending WHERE state = %s AND browser_hash = %s AND expires_at > now() "
                  "RETURNING verifier, nonce", (state, hash_value(browser)), fetch=True)
        if q.get("error"):
            return self.page("Sign-in cancelled", f"<p class=err>{esc(q['error'][0])}</p>"
                             "<p><a href=/>start again</a></p>", 400)
        if not rows or not code:
            return self.page("Sign-in failed", "<p class=err>The sign-in expired or was not started here.</p>"
                             "<p><a href=/>start again</a></p>", 400)
        try:
            claims = exchange_code(code, rows[0][0], rows[0][1])
            username, identity = username_from_claims(claims), claims["sub"]
            name, groups = claims.get("name") or username, groups_from_claims(claims)
        except Exception as e:
            log("sign-in error:", repr(e))
            return self.page("Sign-in failed", f"<p class=err>{esc(e)}</p><p><a href=/>start again</a></p>", 502)
        sid = secrets.token_urlsafe(32)
        db("INSERT INTO sessions (id_hash, username, identity, full_name, groups, csrf, expires_at) "
           "VALUES (%s, %s, %s, %s, %s, %s, now() + make_interval(hours => %s))",
           (hash_value(sid), username, identity, name, groups, secrets.token_urlsafe(24), SESSION_HOURS))
        # Same username, different Keycloak account: the old sessions don't pass to the new one.
        db("DELETE FROM sessions WHERE username = %s AND identity IS DISTINCT FROM %s", (username, identity))
        log("signed in:", username)
        self.redirect("/", headers=[("Set-Cookie", "__Host-maas_login=; Path=/; Max-Age=0; Secure; HttpOnly"),
                                    ("Set-Cookie", f"__Host-maas_session={sid}; Path=/; Secure; HttpOnly; "
                                     f"SameSite=Lax; Max-Age={SESSION_HOURS * 3600}")])

    def do_POST(self):
        sid_hash, s = self.session()
        f = self.form()
        if not s or not secrets.compare_digest(f.get("csrf", ""), s["csrf"]):
            return self.send(403, "sign in again", "text/plain")
        if self.path == "/logout":
            db("DELETE FROM sessions WHERE id_hash = %s", (sid_hash,))
            end = OIDC.get("end_session_endpoint")
            target = (end + "?" + urllib.parse.urlencode({"client_id": OIDC_CLIENT_ID,
                                                          "post_logout_redirect_uri": f"https://{ROUTE_HOST}/"})
                      if end else "/")
            return self.redirect(target, headers=[("Set-Cookie", "__Host-maas_session=; Path=/; Max-Age=0; Secure; HttpOnly")])
        self.send(404, "not found", "text/plain")


def promql_str(v):
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def prom(path, **params):
    url = THANOS_URL.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=15) as r:
        d = json.loads(r.read())
    if d.get("status") != "success":
        raise RuntimeError(d.get("error", "query failed"))
    return d["data"]["result"]


def scalar(q):
    try:
        res = prom("/api/v1/query", query=q)
        return float(res[0]["value"][1]) if res else 0.0
    except Exception as e:
        log("usage query failed:", repr(e)[:160])
        return None


def counter_total(metric, username, window="1d"):
    """Change over the window, including a series' first sample when the
    series was created inside it; increase() alone misses that value."""
    qu = promql_str(username)
    sel = f"{metric}{{user={qu}}}"
    return (f"sum(increase({sel}[{window}]))"
            f" + sum(min_over_time({sel}[{window}]) unless {sel} offset {window})")


def bars_svg(points, width=640, height=160):
    """A small bar chart of [(unix time, value)], drawn server-side."""
    if not points:
        return "<p class=muted>No usage recorded yet.</p>"
    top = max(v for _, v in points) or 1
    bw = width / len(points)
    out = [f'<svg viewBox="0 0 {width} {height + 24}" width="100%" role="img" aria-label="tokens per day">']
    for i, (t, v) in enumerate(points):
        h = (v / top) * height
        out.append(f'<rect x="{i * bw + 1:.1f}" y="{height - h:.1f}" width="{max(bw - 2, 1):.1f}" height="{h:.1f}" '
                   f'fill="#5b7fb8"><title>{time.strftime("%Y-%m-%d", time.gmtime(t))}: {int(v):,} tokens</title></rect>')
    out.append(f'<text x="0" y="{height + 18}" font-size="11" fill="#777">'
               f'{time.strftime("%b %d", time.gmtime(points[0][0]))}</text>')
    out.append(f'<text x="{width}" y="{height + 18}" font-size="11" fill="#777" text-anchor="end">'
               f'{time.strftime("%b %d", time.gmtime(points[-1][0]))} · peak {int(top):,}</text></svg>')
    return "".join(out)


def volume_stats(root="/volumes"):
    """(labels, free bytes, size bytes) for each storage volume mounted under
    root (read-only, for these metrics only)."""
    out = []
    if os.path.isdir(root):
        for name in os.listdir(root):
            try:
                st = os.statvfs(f"{root}/{name}")
                out.append(({"volume": name}, st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize))
            except OSError:
                pass
    return out


def label(v):
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def exposition(families):
    """Prometheus text format from [(name, type, help, [(labels dict, value)])]."""
    out = []
    for name, typ, help_, samples in families:
        out += [f"# HELP {name} {help_}", f"# TYPE {name} {typ}"]
        for labels, value in samples:
            ls = ",".join(f'{k}="{label(v)}"' for k, v in labels.items())
            out.append(f"{name}{{{ls}}} {value}" if ls else f"{name} {value}")
    return "\n".join(out) + "\n"


def load_governance():
    try:
        with open(GOVERNANCE_FILE) as f:
            objects = json.load(f)
        return objects if isinstance(objects, list) else []
    except (OSError, ValueError) as e:
        log("governance:", repr(e)[:120])
        return []


def subscriptions(objects, groups, username):
    """The subscriptions a user owns, by group or by name, highest priority first."""
    out = []
    for obj in objects:
        if obj.get("kind") != "MaaSSubscription":
            continue
        owner = (obj.get("spec") or {}).get("owner") or {}
        if username in (owner.get("users") or []) or \
                any(g.get("name") in groups for g in owner.get("groups") or []):
            out.append(obj)
    out.sort(key=lambda o: (-(o.get("spec") or {}).get("priority", 0), o["metadata"]["name"]))
    return out


def window_seconds(window):
    unit = str(window)[-1:]
    try:
        value = int(str(window)[:-1])
    except ValueError:
        return None
    return value * {"s": 1, "m": 60, "h": 3600}.get(unit, 0) or None


def limitador_counters(namespace):
    url = LIMITADOR_URL + "/counters/" + urllib.parse.quote(namespace, safe="")
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def api_dsn():
    u = urllib.parse.urlsplit(DATABASE_URL)
    user = "maasapi:" + urllib.parse.quote(MAASAPI_PASSWORD, safe="")
    return urllib.parse.urlunsplit((u.scheme, f"{user}@{u.hostname}:{u.port or 5432}",
                                    "/maasapi", u.query, ""))


def user_keys(username):
    """The user's key metadata from maas-api's database; no key material."""
    if not MAASAPI_PASSWORD:
        return None
    with psycopg.connect(api_dsn(), connect_timeout=3) as conn:
        return conn.execute(
            "SELECT name, status, created_at, expires_at, last_used_at, subscription "
            "FROM api_keys WHERE username = %s ORDER BY created_at DESC", (username,)).fetchall()




def counter_used(counters, limit_name, username, max_value, seconds):
    """Tokens used in this window, from the counter for this user and rate."""
    for c in counters or []:
        lim = c.get("limit") or {}
        if lim.get("name") != limit_name:
            continue
        if (c.get("set_variables") or {}).get(VARIABLE) != username:
            continue
        if lim.get("max_value") != max_value or lim.get("seconds") != seconds:
            continue
        rem = c.get("remaining", 0)
        if rem >= 2**63:
            rem -= 2**64
        return max_value - rem
    return None


class Usage(SignIn):
    """The dashboard. Every query is built here and fixed to the signed-in
    user; nothing the browser sends ends up in a query."""

    TITLE = "MaaS usage"


    def do_GET(self):
        path = urllib.parse.urlparse(self.path)
        if path.path == "/login":
            return self.login()
        if path.path == "/callback":
            return self.callback(urllib.parse.parse_qs(path.query))
        if path.path != "/":
            return self.send(404, "not found", "text/plain")
        _, s = self.session()
        if not s:
            return self.page(self.TITLE, "<p>Sign in to see your MaaS usage.</p>"
                             "<p><a href=/login><button>Sign in with GitHub</button></a></p>")
        self.home(s)

    def home(self, s):
        csrf = f"<input type=hidden name=csrf value='{s['csrf']}'>"
        out = (f"<p>Signed in as <b>{esc(s['username'])}</b> {esc(s['name'])} <form class=inline method=post "
               f"action=/logout>{csrf}<button>sign out</button></form></p>")
        subs = subscriptions(load_governance(), s["groups"], s["username"])
        if not subs:
            return self.page(self.TITLE, out + "<p class=err>None of your groups has a MaaS subscription.</p>", 403)
        out += self.window(s["username"], subs)
        day = {k: scalar(counter_total(m, s["username"])) for k, m in (
            ("tokens", "authorized_hits"),
            ("requests", "report_calls"),
            ("refused", "limited_calls"))}
        fmt = lambda v, f="{:,.0f}": "–" if v is None or v != v else f.format(v)
        out += ("<h2>Last 24 hours</h2><table><tr><th>Tokens</th><th>Requests</th><th>Refused (429)</th></tr>"
                f"<tr><td>{fmt(day['tokens'])}</td><td>{fmt(day['requests'])}</td><td>{fmt(day['refused'])}</td></tr></table>")
        try:
            end = int(time.time()) // 86400 * 86400 + 86400
            res = prom("/api/v1/query_range", query=counter_total("authorized_hits", s["username"]),
                       start=end - 30 * 86400, end=end, step=86400)
            pts = [(int(t), float(v)) for t, v in res[0]["values"]] if res else []
        except Exception as e:
            log("usage range query:", repr(e)[:160])
            pts = []
        out += "<h2>Tokens per day, last 30 days</h2>" + bars_svg(pts)
        out += "<h2>Your keys</h2>"
        try:
            keys = user_keys(s["username"])
        except Exception as e:
            log("api keys:", repr(e)[:120])
            keys = None
        if keys is None:
            out += "<p class=muted>Keys are unavailable right now.</p>"
        elif not keys:
            out += "<p class=muted>No keys yet; create one with maas-login.py.</p>"
        else:
            fmt = lambda t: t.strftime("%Y-%m-%d %H:%M") if t else "-"
            out += ("<table><tr><th>Name</th><th>Subscription</th><th>Created</th>"
                    "<th>Expires</th><th>Last used</th><th>Status</th></tr>")
            for name, status, created, expires, last_used, sub in keys:
                out += (f"<tr><td>{esc(name)}</td><td>{esc(sub)}</td><td>{fmt(created)}</td>"
                        f"<td>{fmt(expires)}</td><td>{fmt(last_used)}</td>"
                        f"<td>{esc(status)}</td></tr>")
            out += "</table><p class=muted>Create and revoke keys with maas-login.py.</p>"
        self.page(self.TITLE, out)

    def window(self, username, subs):
        out, cache = [], {}
        for sub in subs:
            spec = sub.get("spec") or {}
            name = sub["metadata"]["name"]
            out.append(f"<h2>Subscription <b>{esc(name)}</b></h2>"
                       f"<p class=muted>Priority {esc(spec.get('priority', 0))}.</p>")
            for ref in spec.get("modelRefs") or []:
                model = ref.get("name")
                model_ns = ref.get("namespace") or NAMESPACE
                namespace = f"{model_ns}/{model}"
                if namespace not in cache:
                    try:
                        cache[namespace] = limitador_counters(namespace)
                    except Exception as e:
                        log("limitador counters:", repr(e)[:120])
                        cache[namespace] = None
                limit_name = f"{model_ns}-{name}-{model}-tokens"
                for rate in ref.get("tokenRateLimits") or []:
                    max_value, seconds = rate.get("limit"), window_seconds(rate.get("window"))
                    used = counter_used(cache[namespace], limit_name, username, max_value, seconds)
                    out.append(f"<h3>Model <b>{esc(model)}</b></h3>")
                    if used is None:
                        out.append("<p class=muted>This window's counter is unavailable.</p>")
                        continue
                    frac = min(used / max_value, 1) if max_value else 0
                    out.append(
                        f'<div style="background:#eee;border-radius:4px;height:14px;width:100%;max-width:640px">'
                        f'<div style="background:{"#c0392b" if used >= max_value else "#5b7fb8"};height:14px;'
                        f'border-radius:4px;width:{frac * 100:.1f}%"></div></div>'
                        f"<p>{used:,} of {max_value:,} tokens used per {esc(rate.get('window'))}.</p>")
        return "".join(out)


def user_metrics(username, groups):
    """Prometheus text for one user: 24-hour counters and this window."""
    out = []

    def family(name, help_, samples):
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} gauge")
        for labels_, value in samples:
            ls = ",".join(f'{k}="{label(v)}"' for k, v in labels_.items())
            out.append(f"{name}{{{ls}}} {value}")

    for suffix, metric, help_ in (
            ("tokens_24h", "authorized_hits", "Tokens counted in the last 24 hours."),
            ("requests_24h", "report_calls", "Requests counted in the last 24 hours."),
            ("refused_24h", "limited_calls", "Requests refused by budget in the last 24 hours.")):
        value = scalar(counter_total(metric, username))
        if value is not None:
            family(f"maas_user_{suffix}", help_, [({"user": username}, value)])
    used, limits, cache = [], [], {}
    for sub in subscriptions(load_governance(), groups or [], username):
        spec = sub.get("spec") or {}
        sub_name = sub["metadata"]["name"]
        for ref in spec.get("modelRefs") or []:
            model = ref.get("name")
            model_ns = ref.get("namespace") or NAMESPACE
            namespace = f"{model_ns}/{model}"
            if namespace not in cache:
                try:
                    cache[namespace] = limitador_counters(namespace)
                except Exception as e:
                    log("limitador counters:", repr(e)[:120])
                    cache[namespace] = None
            limit_name = f"{model_ns}-{sub_name}-{model}-tokens"
            for rate in ref.get("tokenRateLimits") or []:
                max_value = rate.get("limit")
                value = counter_used(cache[namespace], limit_name, username,
                                     max_value, window_seconds(rate.get("window")))
                if value is None:
                    continue
                labels_ = {"user": username, "subscription": sub_name, "model": model,
                           "window": str(rate.get("window"))}
                used.append((labels_, value))
                limits.append((labels_, max_value))
    family("maas_user_window_tokens", "Tokens used in the current Limitador window.", used)
    family("maas_user_window_limit", "The token limit for the current window.", limits)
    return "\n".join(out) + "\n"


class UsageMetrics(Base):
    """The page's metrics-only port: storage gauges and per-user metrics."""

    def do_GET(self):
        if self.path == "/healthz":
            return self.healthz()
        if self.path == "/user-metrics":
            user = self.headers.get("X-MaaS-Username", "")
            try:
                groups = json.loads(self.headers.get("X-MaaS-Group", "[]"))
            except ValueError:
                groups = []
            if not user:
                return self.send(401, "no identity", "text/plain")
            return self.send(200, user_metrics(user, groups), "text/plain; version=0.0.4")
        if self.path != "/metrics":
            return self.send(404, "not found", "text/plain")
        vols = volume_stats()
        body = exposition([
            ("maas_volume_free_bytes", "gauge", "Free space on the shared storage volume.",
             [(lbl, free) for lbl, free, _ in vols]),
            ("maas_volume_size_bytes", "gauge", "Size of the shared storage volume.",
             [(lbl, size) for lbl, _, size in vols]),
        ])
        self.send(200, body, "text/plain; version=0.0.4")


class Health(Base):
    def do_GET(self):
        if self.path == "/healthz":
            return self.healthz()
        if self.path == "/livez":
            return self.livez()
        self.send(404, "not found", "text/plain")


def housekeeping():
    while True:
        try:
            db("DELETE FROM sessions WHERE expires_at < now()")
            db("DELETE FROM oauth_pending WHERE expires_at < now()")
        except Exception as e:
            log("housekeeping:", repr(e))
        time.sleep(3600)


class Server(http.server.ThreadingHTTPServer):
    tls = None
    request_queue_size = 128

    def finish_request(self, request, client_address):
        if self.tls:
            request.settimeout(30)
            try:
                request = self.tls.wrap_socket(request, server_side=True)
            except (OSError, ssl.SSLError):
                return
        try:
            super().finish_request(request, client_address)
        finally:
            if self.tls:
                request.close()

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, ssl.SSLError)):
            return
        super().handle_error(request, client_address)


SERVERS = []


def serve(handler, host, port, tls=False):
    srv = Server((host, port), handler)
    if tls:
        srv.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        srv.tls.minimum_version = ssl.TLSVersion.TLSv1_2
        srv.tls.load_cert_chain("/etc/tls/tls.crt", "/etc/tls/tls.key")
    SERVERS.append(srv)
    log(f"{handler.__name__} listening on {host}:{port}{' (TLS)' if tls else ''}")
    return srv


def stop_servers():
    for srv in SERVERS:
        try:
            srv.shutdown()
        except Exception:
            pass


def run(srv):
    srv.serve_forever()
    time.sleep(3)
    for s in SERVERS:
        try:
            s.server_close()
        except Exception:
            pass


def on_sigterm(*_):
    if not SERVERS:
        sys.exit(0)
    threading.Thread(target=stop_servers, daemon=True).start()


def main():
    if MODE != "usage":
        sys.exit(f"unknown MODE {MODE}")
    POOL.open()
    signal.signal(signal.SIGTERM, on_sigterm)
    wait_for_db()
    missing = [n for n in ("ROUTE_HOST", "OIDC_ISSUER", "OIDC_BACKCHANNEL", "OIDC_CLIENT_SECRET")
               if not globals()[n]]
    if missing:
        sys.exit("required: " + ", ".join(missing))
    while True:  # Keycloak may still be starting
        try:
            discover_oidc()
            break
        except Exception as e:
            log("waiting for Keycloak:", repr(e)[:160])
            time.sleep(5)
    threading.Thread(target=housekeeping, daemon=True).start()
    threading.Thread(target=serve(Health, "0.0.0.0", 8081).serve_forever, daemon=True).start()
    threading.Thread(target=serve(UsageMetrics, "0.0.0.0", 9100).serve_forever, daemon=True).start()
    run(serve(Usage, "0.0.0.0", 8443, tls=True))


if __name__ == "__main__":
    main()
