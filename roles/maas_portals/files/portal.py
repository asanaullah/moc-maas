#!/usr/bin/env python3
"""MaaS portals for a project namespace.

MODE=admin  plans, group-to-plan mappings, service keys, every key and the
            audit trail. Listens on the pod's localhost only, so it is reached
            with `oc port-forward` and nothing else; every project member who
            can port-forward is a MaaS admin of this namespace.
MODE=keys   users sign in through the MaaS's own Keycloak (OIDC, brokered to
            GitHub) and request or revoke their own keys. A second port
            answers the gateway's key checks and serves /metrics.
MODE=usage  the user dashboard: users sign in the same way (their own Keycloak
            client) and see only their own usage, from Thanos and Limitador.

The admin portal also publishes plans, group mappings and the key inventory as
metrics, on a metrics-only port (9100); its UI stays on localhost.

Access is by Keycloak group: a user may hold keys if any of their groups maps
to a plan. With several, the plan with the highest priority wins (then the
highest rate, then the name), and its budget, models and key lifetime apply. A user key records the groups it was issued under; each
sign-in refreshes that, and revokes the user's keys when no group qualifies.
Keys are random 256-bit values, stored only as SHA-256 hashes, and expire.
"""
import base64
import datetime
import hashlib
import html
import http.server
import json
import os
import re
import secrets
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

MODE = os.environ["MODE"]
DATABASE_URL = os.environ["DATABASE_URL"]
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc"
# The pod's own namespace, so the same code runs in any project.
try:
    NAMESPACE = open(f"{SA_DIR}/namespace").read().strip()
except OSError:
    NAMESPACE = "uat-project"
ROUTE_HOST = os.environ.get("ROUTE_HOST", "")
LIMITS_CONFIGMAP = os.environ.get("LIMITS_CONFIGMAP", "maas-limits")
# The realm's public issuer, as browsers and tokens see it, and the same realm
# through the Service name, for the portal's own (backchannel) requests.
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "")
OIDC_BACKCHANNEL = os.environ.get("OIDC_BACKCHANNEL", "")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "maas-portal")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
RECONCILER_CLIENT_SECRET = os.environ.get("RECONCILER_CLIENT_SECRET", "")
SERVICE_CA = f"{SA_DIR}/service-ca.crt"
THANOS_URL = os.environ.get("THANOS_URL", "http://maas-thanos-query:9090")
LIMITADOR_URL = os.environ.get("LIMITADOR_URL", "http://maas-limitador:8080")
SESSION_HOURS = 8
KEY_CACHE_SECONDS = 5  # fallback only; revocations clear caches at once via NOTIFY
STALE_SECONDS = 300  # how long the last known result serves while the database is down
PURGE_EXPIRED_KEYS_AFTER_DAYS = 90
NOTIFY_CHANNEL = "maas_keys"

MIGRATIONS = [
    # The base tables.
    """
    CREATE TABLE IF NOT EXISTS plans (
      name text PRIMARY KEY,
      models text[] NOT NULL DEFAULT '{}',
      tokens integer NOT NULL CHECK (tokens > 0),
      window_seconds integer NOT NULL DEFAULT 60 CHECK (window_seconds > 0),
      max_key_days integer NOT NULL DEFAULT 90 CHECK (max_key_days > 0));
    CREATE TABLE IF NOT EXISTS group_plans (
      group_name text PRIMARY KEY,
      plan text NOT NULL REFERENCES plans(name));
    CREATE TABLE IF NOT EXISTS api_keys (
      id uuid PRIMARY KEY,
      kind text NOT NULL CHECK (kind IN ('user', 'service')),
      owner text NOT NULL,
      identity text,
      name text NOT NULL,
      groups text[],
      plan text,
      key_hash text NOT NULL UNIQUE,
      created_at timestamptz NOT NULL DEFAULT now(),
      expires_at timestamptz NOT NULL,
      revoked_at timestamptz,
      revoked_reason text,
      CHECK ((kind = 'user' AND groups IS NOT NULL) OR (kind = 'service' AND plan IS NOT NULL)));
    CREATE INDEX IF NOT EXISTS api_keys_owner ON api_keys (owner);
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
    # A service key's plan is kept as history once the key is revoked or
    # expired, so it is not a foreign key; deleting a plan is refused while
    # active keys use it.
    "ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_plan_fkey;",
    "ALTER TABLE oauth_pending ADD COLUMN IF NOT EXISTS nonce text;",
    "ALTER TABLE oauth_pending ADD COLUMN IF NOT EXISTS browser_hash text;",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS last_used_at timestamptz;",
    # Append only: a migration's version is its position in this list.
    "ALTER TABLE plans ALTER COLUMN tokens TYPE bigint;",
    "ALTER TABLE plans ADD COLUMN IF NOT EXISTS priority integer NOT NULL DEFAULT 0;",
    "ALTER TABLE plans ADD COLUMN IF NOT EXISTS max_request_tokens integer;",
    "ALTER TABLE plans ADD COLUMN IF NOT EXISTS max_requests integer;",
    "CREATE INDEX IF NOT EXISTS audit_key_create ON audit ((details->>'owner'), at) WHERE action = 'key.create';",
    "ALTER TABLE plans ADD COLUMN IF NOT EXISTS hourly_tokens bigint;",
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


def notify(payload):
    db("SELECT pg_notify(%s, %s)", (NOTIFY_CHANNEL, payload))


SCHEMA_LOCK = 0x6D616173  # 'maas': concurrent portal pods serialize on this


def wait_for_db():
    """Apply pending schema migrations, each exactly once. A migration takes
    an advisory lock and a short lock_timeout, so a starting pod never queues
    key checks behind its DDL for long; on a normal start nothing runs."""
    while True:
        try:
            if db("SELECT to_regclass('schema_version')", fetch=True)[0][0] is None:
                db("CREATE TABLE schema_version (version integer PRIMARY KEY, "
                   "applied_at timestamptz NOT NULL DEFAULT now())")
            for version, sql in enumerate(MIGRATIONS):
                if db("SELECT 1 FROM schema_version WHERE version = %s", (version,), fetch=True):
                    continue
                with POOL.connection() as conn:
                    with conn.transaction():
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


def sa_token():
    with open(f"{SA_DIR}/token") as f:
        return f.read().strip()


def api_ctx():
    return ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")


def kube(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": "Bearer " + sa_token(), "Accept": "application/json",
        "Content-Type": "application/merge-patch+json"})
    with urllib.request.urlopen(req, context=api_ctx(), timeout=10) as r:
        return json.loads(r.read())


def hash_value(v):
    return hashlib.sha256(v.encode()).hexdigest()


def new_key():
    return "maas-" + secrets.token_urlsafe(32)


KEY_RE = re.compile(r"maas-[A-Za-z0-9_-]{43}")


KEY_LIMIT = 5
KEYS_PER_HOUR = 10


def esc(v):
    return html.escape(str(v))


def effective(groups):
    """The plan for a set of groups: the mapped plan with the highest
    priority, then the highest rate (tokens per second), then the name.
    Budget, models and key lifetime all come from that plan.
    Returns (plan, tokens, window, models, max key days, mapped groups) or None;
    an empty model list means every model."""
    rows = db("SELECT gp.group_name, p.name, p.tokens, p.window_seconds, p.models, p.max_key_days "
              "FROM group_plans gp JOIN plans p ON p.name = gp.plan WHERE gp.group_name = ANY(%s) "
              "ORDER BY p.priority DESC, p.tokens::float8 / p.window_seconds DESC, p.name",
              (list(groups),), fetch=True)
    if not rows:
        return None
    _, name, tokens, window, models, days = rows[0]
    # All mapped groups are kept on the key, so it falls back to the next plan
    # if the chosen plan's group is removed later.
    return (name, tokens, window, sorted(models or []), days, sorted({r[0] for r in rows}))


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
    server_version = "maas-portal"

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
            return {}  # no form token either, so the caller refuses it
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

    def from_loopback_name(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("localhost", "127.0.0.1", "::1")

    def healthz(self):
        try:
            db("SELECT 1")
            self.send(200, "ok", "text/plain")
        except Exception as e:
            self.send(503, f"database: {type(e).__name__}", "text/plain")

    def livez(self):
        self.send(200, "ok", "text/plain")


def key_rows(where="", args=(), after=None):
    if after:
        where = (where + " AND " if where else "WHERE ") + "created_at < %s"
        args = (*args, after)
    return db("SELECT id, kind, owner, name, groups, plan, created_at, expires_at, revoked_at, revoked_reason, "
              "last_used_at "
              f"FROM api_keys {where} ORDER BY created_at DESC LIMIT 500", args, fetch=True)


def key_status(expires, revoked, reason, now):
    if revoked:
        return f"revoked {revoked:%Y-%m-%d %H:%M}" + (f" ({esc(reason)})" if reason else "")
    if expires < now:
        return f"expired {expires:%Y-%m-%d}"
    return f"active until {expires:%Y-%m-%d}"


def revoke(key_ids_sql, args, actor, reason):
    """Revoke matching active keys, record it, and clear every replica's cache."""
    rows = db(f"UPDATE api_keys SET revoked_at = now(), revoked_reason = %s WHERE revoked_at IS NULL "
              f"AND {key_ids_sql} RETURNING id, owner, key_hash", (reason, *args), fetch=True)
    for kid, owner, key_hash in rows:
        audit(actor, "key.revoke", str(kid), owner=owner, reason=reason)
        notify(key_hash)
    return len(rows)


# ── admin portal ─────────────────────────────────────────────────────────────

ADMIN_CSRF = secrets.token_urlsafe(24)
ADMIN_ACTOR = "admin (port-forward)"
ADMIN_STATUS = {"limits": ""}


def delete_plan(name, actor):
    active = db("SELECT count(*) FROM api_keys WHERE kind = 'service' AND plan = %s "
                "AND revoked_at IS NULL AND expires_at > now()", (name,), fetch=True)[0][0]
    if active:
        raise ValueError(f"plan {name} still has {active} active service key(s); revoke them first")
    db("DELETE FROM plans WHERE name = %s", (name,))  # refused by group_plans while groups use it
    audit(actor, "plan.delete", name)
    notify("*")
    write_limits(actor)


LIMITS_LOCK = threading.Lock()


def write_limits(actor):
    """Make Limitador's limits match the plans. A plan without a limit there
    would have no token budget at all. Locked so two concurrent admin calls
    can't leave the older state in Limitador."""
    with LIMITS_LOCK:
        try:
            kube("PATCH", f"/api/v1/namespaces/{NAMESPACE}/configmaps/{LIMITS_CONFIGMAP}",
                 {"data": {"limits.yaml": limits_yaml()}})
            audit(actor, "limits.write", LIMITS_CONFIGMAP)
            ADMIN_STATUS["limits"] = time.strftime("written %H:%M:%S; Limitador reloads it within a minute")
        except Exception as e:
            log("writing limits failed:", repr(e))
            ADMIN_STATUS["limits"] = time.strftime("FAILED at %H:%M:%S: ") + str(e)
            raise


def hourly_cap(tokens, window, hourly_tokens):
    """The plan's hourly token cap, or None. Unset: for windows over an hour,
    twice the plan's average hourly rate, so a user can't spend two budgets
    around a window's boundary within minutes; 0: none; otherwise the value."""
    if hourly_tokens is None:
        return max(1, 2 * tokens * 3600 // window) if window > 3600 else None
    return hourly_tokens or None


def limits_yaml():
    """Per plan, in namespace maas: the token budget per window and the hourly
    cap (Limitador refuses when any matching limit is spent). For plans with a
    request limit, a request count per user in namespace maas-requests, which
    the gateway charges one per request."""
    plans = db("SELECT name, tokens, window_seconds, max_requests, hourly_tokens FROM plans ORDER BY name",
               fetch=True)
    out = []
    for name, tokens, window, max_requests, hourly_tokens in plans:
        cond, var = [f"descriptors[0]['plan'] == '{name}'"], ["descriptors[0]['user']"]
        out.append({"namespace": "maas", "max_value": tokens, "seconds": window,
                    "conditions": cond, "variables": var})
        hourly = hourly_cap(tokens, window, hourly_tokens)
        if hourly and hourly < tokens:
            out.append({"namespace": "maas", "max_value": hourly, "seconds": 3600,
                        "conditions": cond, "variables": var})
        if max_requests:
            out.append({"namespace": "maas-requests", "max_value": max_requests, "seconds": window,
                        "conditions": cond, "variables": var})
    return json.dumps(out, indent=1)


PLAN_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
GROUP_RE = re.compile(r"[A-Za-z0-9._-]{1,255}")
MODEL_RE = re.compile(r"[A-Za-z0-9._/:-]{1,255}")
OWNER_RE = re.compile(r"[a-z0-9][a-z0-9._@-]{0,62}")


def optional_int(v):
    """An optional whole number from a form or JSON value; empty means none."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        raise ValueError("expected a whole number")
    return int(v)


def set_plan(actor, name, models, tokens, window, days, priority=0, max_request_tokens=None, max_requests=None,
             hourly_tokens=None):
    """One place a plan is written: the checks and the audit entry are the
    same whether it came from the form or the JSON API."""
    if not PLAN_RE.fullmatch(name):
        raise ValueError("plan names are 1-63 characters, starting with a lowercase letter or digit")
    if not 1 <= tokens <= 10 ** 12:
        raise ValueError("tokens must be between 1 and 10^12")
    if not 1 <= window <= 31 * 86400:
        raise ValueError("the window must be between 1 second and 31 days")
    if not 1 <= days <= 3650:
        raise ValueError("the key lifetime must be between 1 and 3650 days")
    if not -1000 <= priority <= 1000:
        raise ValueError("priority must be between -1000 and 1000")
    if max_request_tokens is not None and not 1 <= max_request_tokens <= 10 ** 7:
        raise ValueError("the per-request token cap must be between 1 and 10^7, or empty")
    if max_requests is not None and not 1 <= max_requests <= 10 ** 9:
        raise ValueError("the request limit must be between 1 and 10^9 per window, or empty")
    if hourly_tokens is not None and not 0 <= hourly_tokens <= 10 ** 12:
        raise ValueError("the hourly cap must be between 0 (none) and 10^12, or empty for the default")
    models = sorted(set(models))
    for m in models:
        if not MODEL_RE.fullmatch(m):
            raise ValueError(f"model name {m!r} is not allowed")
    db("INSERT INTO plans (name, models, tokens, window_seconds, max_key_days, priority, max_request_tokens, "
       "max_requests, hourly_tokens) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
       "ON CONFLICT (name) DO UPDATE SET models = EXCLUDED.models, tokens = EXCLUDED.tokens, "
       "window_seconds = EXCLUDED.window_seconds, max_key_days = EXCLUDED.max_key_days, "
       "priority = EXCLUDED.priority, max_request_tokens = EXCLUDED.max_request_tokens, "
       "max_requests = EXCLUDED.max_requests, hourly_tokens = EXCLUDED.hourly_tokens",
       (name, models, tokens, window, days, priority, max_request_tokens, max_requests, hourly_tokens))
    audit(actor, "plan.set", name, models=models, tokens=tokens, window_seconds=window, max_key_days=days,
          priority=priority, max_request_tokens=max_request_tokens, max_requests=max_requests,
          hourly_tokens=hourly_tokens)
    notify("*")
    write_limits(actor)


def map_group(actor, group, plan):
    if not GROUP_RE.fullmatch(group):
        raise ValueError("group names are 1-255 characters: letters, digits, dot, underscore or dash")
    if not db("SELECT 1 FROM plans WHERE name = %s", (plan,), fetch=True):
        raise ValueError(f"plan {plan!r} doesn't exist")
    db("INSERT INTO group_plans (group_name, plan) VALUES (%s, %s) "
       "ON CONFLICT (group_name) DO UPDATE SET plan = EXCLUDED.plan", (group, plan))
    audit(actor, "group.map", group, plan=plan)
    notify("*")


def unmap_group(actor, group):
    db("DELETE FROM group_plans WHERE group_name = %s", (group,))
    audit(actor, "group.unmap", group)
    notify("*")


def create_key(actor, kid, kind, owner, identity, name, groups, plan, key, days, details):
    """Insert a key unless the owner already holds KEY_LIMIT active keys or
    created KEYS_PER_HOUR in the last hour. A per-owner advisory lock
    serialises concurrent creates, so the counts see each other's rows;
    the audit row is written in the same transaction for the same reason.
    Raises ValueError with the reason when refused."""
    with POOL.connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (owner,))
            active = conn.execute("SELECT count(*) FROM api_keys WHERE owner = %s AND revoked_at IS NULL "
                                  "AND expires_at > now()", (owner,)).fetchone()[0]
            if active >= KEY_LIMIT:
                raise ValueError(f"at most {KEY_LIMIT} active keys per owner")
            recent = conn.execute("SELECT count(*) FROM audit WHERE action = 'key.create' "
                                  "AND details->>'owner' = %s AND at > now() - interval '1 hour'",
                                  (owner,)).fetchone()[0]
            if recent >= KEYS_PER_HOUR:
                raise ValueError(f"at most {KEYS_PER_HOUR} new keys per hour")
            conn.execute("INSERT INTO api_keys (id, kind, owner, identity, name, groups, plan, key_hash, expires_at) "
                         "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now() + make_interval(days => %s))",
                         (kid, kind, owner, identity, name, groups, plan, hash_value(key), days))
            conn.execute("INSERT INTO audit (actor, action, target, details) VALUES (%s, 'key.create', %s, %s)",
                         (actor, str(kid), Jsonb({"owner": owner, "kind": kind, **details})))


def mint_service_key(actor, owner, name, plan, days):
    if not OWNER_RE.fullmatch(owner):
        raise ValueError("service owners are 1-63 characters, starting with a lowercase letter or digit")
    maxd = db("SELECT max_key_days FROM plans WHERE name = %s", (plan,), fetch=True)
    if not maxd or not 1 <= days <= maxd[0][0]:
        raise ValueError(f"plan {plan!r} doesn't exist or allows fewer than {days} days")
    key, kid, owner = new_key(), uuid.uuid4(), "service:" + owner
    create_key(actor, kid, "service", owner, None, str(name)[:64], None, plan, key, days,
               {"plan": plan, "days": days})
    return key, str(kid)


class Admin(Base):
    def do_GET(self):
        if not self.from_loopback_name():
            return self.send(403, "the admin portal answers only on localhost", "text/plain")
        if self.path == "/healthz":
            return self.healthz()
        if self.path == "/livez":
            return self.livez()
        if self.path.startswith("/api/"):
            return self.api_get()
        if self.path != "/":
            return self.send(404, "not found", "text/plain")
        self.dashboard()

    def dashboard(self, new_key=None):
        csrf = f'<input type="hidden" name="csrf" value="{ADMIN_CSRF}">'
        plans = db("SELECT name, models, tokens, window_seconds, max_key_days, priority, max_request_tokens, "
                   "max_requests, hourly_tokens FROM plans ORDER BY priority DESC, name", fetch=True)
        groups = db("SELECT group_name, plan FROM group_plans ORDER BY group_name", fetch=True)
        actor = urllib.parse.unquote(self.cookie_value("maas_actor") or "").strip()[:64]
        b = []
        if new_key:
            b.append("<p><b>New service key. Copy it now; it is not shown again.</b></p>"
                     f"<p class=key>{esc(new_key)}</p>")
        b.append(f"<p class=muted>Acting as <b>{esc(actor) or 'admin (port-forward)'}</b> "
                 f"<form class=inline method=post action=/actor>{csrf}"
                 "<input name=actor placeholder='your name, for the audit trail' size=24> "
                 "<button>set</button></form></p>")
        b.append("<h2>Plans</h2><table><tr><th>Plan</th><th>Priority</th><th>Models</th><th>Tokens per window</th>"
                 "<th>Window (s)</th><th>Max key life (days)</th><th>Max tokens per request</th>"
                 "<th>Requests per window</th><th>Tokens per hour</th><th></th></tr>")
        none = "<span class=muted>none</span>"
        for name, models, tokens, window, days, prio, mrt, mreq, hourly in plans:
            cap = hourly_cap(tokens, window, hourly)
            cap_text = (f"{cap:,}" if cap and cap < tokens else none) + \
                (" <span class=muted>(default)</span>" if hourly is None and cap and cap < tokens else "")
            b.append(f"<tr><td>{esc(name)}</td><td>{prio}</td>"
                     f"<td>{esc(', '.join(models)) or '<span class=muted>all</span>'}</td>"
                     f"<td>{tokens:,}</td><td>{window}</td><td>{days}</td>"
                     f"<td>{f'{mrt:,}' if mrt else none}</td><td>{f'{mreq:,}' if mreq else none}</td>"
                     f"<td>{cap_text}</td>"
                     "<td><form class=inline method=post "
                     f"action=/plans/delete>{csrf}<input type=hidden name=name value='{esc(name)}'>"
                     "<button>delete</button></form></td></tr>")
        b.append(f"</table><form method=post action=/plans>{csrf}"
                 "<input name=name placeholder='plan name' required pattern='[a-z0-9][a-z0-9._-]{0,62}'> "
                 "<input name=models placeholder='models, comma separated (empty = all)' size=30> "
                 "<input name=tokens type=number min=1 placeholder=tokens required> "
                 "<input name=window type=number min=1 value=60 required title='window seconds'> "
                 "<input name=days type=number min=1 value=90 required title='max key life, days'> "
                 "<input name=priority type=number min=-1000 max=1000 value=0 required title='priority'> "
                 "<input name=max_request_tokens type=number min=1 placeholder='max tokens/request' "
                 "title='per-request max_tokens cap (empty = none)'> "
                 "<input name=max_requests type=number min=1 placeholder='requests/window' "
                 "title='requests per window per user (empty = none)'> "
                 "<input name=hourly_tokens type=number min=0 placeholder='tokens/hour' "
                 "title='hourly token cap: empty = default (2x the average hourly rate for windows over an hour), "
                 "0 = none'> "
                 "<button>add or update plan</button></form>")
        b.append(f"<p><form method=post action=/limits>{csrf}<button>rewrite plan limits in Limitador</button> "
                 f"<span class=muted>{esc(ADMIN_STATUS['limits'])}</span></form></p>")
        opts = "".join(f"<option>{esc(p[0])}</option>" for p in plans)
        b.append("<h2>Groups</h2><p class=muted>Members of these Keycloak groups may hold keys. A user in "
                 "several gets the plan with the highest priority (then the highest tokens per second), "
                 "with that plan's budget, models and key lifetime.</p>"
                 "<table><tr><th>Keycloak group</th><th>Plan</th><th></th></tr>")
        for g, plan in groups:
            b.append(f"<tr><td>{esc(g)}</td><td>{esc(plan)}</td><td><form class=inline method=post "
                     f"action=/groups/delete>{csrf}<input type=hidden name=group value='{esc(g)}'>"
                     "<button>remove</button></form></td></tr>")
        b.append(f"</table><form method=post action=/groups>{csrf}<input name=group placeholder='group name' "
                 f"required pattern='[A-Za-z0-9._-]{1,255}'> <select name=plan required>{opts}</select> "
                 "<button>map group to plan</button></form>")
        b.append(f"<h2>Service keys</h2><p class=muted>For applications and pipelines, not people.</p>"
                 f"<form method=post action=/service-keys>{csrf}<input name=owner placeholder='service name' "
                 "required pattern='[a-z0-9][a-z0-9._-]{0,62}'> <input name=name placeholder='key name' required> "
                 f"<select name=plan required>{opts}</select> <input name=days type=number min=1 value=30 "
                 "required title='expires in days'> <button>mint service key</button></form>")
        b.append("<h2>Keys</h2><table><tr><th>Owner</th><th>Kind</th><th>Name</th><th>Groups / plan</th>"
                 "<th>Created</th><th>Last used</th><th>Status</th><th></th></tr>")
        now = datetime.datetime.now(datetime.timezone.utc)
        rows = key_rows()
        for kid, kind, owner, name, kgroups, plan, created, expires, revoked, reason, last_used in rows:
            basis = esc(", ".join(kgroups)) if kind == "user" else f"plan {esc(plan)}"
            used = f"{last_used:%Y-%m-%d %H:%M}" if last_used else "never"
            btn = "" if revoked else (f"<form class=inline method=post action=/keys/revoke>{csrf}"
                                      f"<input type=hidden name=id value='{kid}'><button>revoke</button></form>")
            b.append(f"<tr><td>{esc(owner)}</td><td>{kind}</td><td>{esc(name)}</td><td>{basis}</td>"
                     f"<td>{created:%Y-%m-%d %H:%M}</td><td>{used}</td>"
                     f"<td>{key_status(expires, revoked, reason, now)}</td><td>{btn}</td></tr>")
        b.append("</table>")
        if len(rows) == 500:
            b.append("<p class=muted>showing the newest 500 keys; older ones via the API's ?after= parameter</p>")
        b.append("<h2>Audit trail</h2><table><tr><th>When</th><th>Who</th><th>What</th>"
                 "<th>Target</th><th>Details</th></tr>")
        for at, actor, action, target, details in db(
                "SELECT at, actor, action, target, details FROM audit ORDER BY id DESC LIMIT 200", fetch=True):
            b.append(f"<tr><td>{at:%Y-%m-%d %H:%M:%S}</td><td>{esc(actor)}</td><td>{esc(action)}</td>"
                     f"<td>{esc(target)}</td><td><code>{esc(json.dumps(details))}</code></td></tr>")
        b.append("</table>")
        self.page("MaaS admin · " + NAMESPACE, "".join(b))

    def do_POST(self):
        if not self.from_loopback_name():
            return self.send(403, "the admin portal answers only on localhost", "text/plain")
        if self.path.startswith("/api/"):
            return self.api()
        f = self.form()
        if f.get("csrf") != ADMIN_CSRF:
            return self.send(403, "bad form token", "text/plain")
        a = urllib.parse.unquote(self.cookie_value("maas_actor") or "").strip()[:64] or ADMIN_ACTOR
        try:
            if self.path == "/actor":
                actor = urllib.parse.quote(f.get("actor", "").strip()[:64])
                return self.redirect("/", headers=[("Set-Cookie", f"maas_actor={actor}; Path=/; "
                                                                "Secure; HttpOnly; SameSite=Lax; Max-Age=28800")])
            if self.path == "/plans":
                set_plan(a, f["name"].strip(),
                         [m.strip() for m in f.get("models", "").split(",") if m.strip()],
                         int(f["tokens"]), int(f["window"]), int(f["days"]), int(f.get("priority") or 0),
                         optional_int(f.get("max_request_tokens")), optional_int(f.get("max_requests")),
                         optional_int(f.get("hourly_tokens")))
            elif self.path == "/plans/delete":
                delete_plan(f["name"], a)
            elif self.path == "/groups":
                map_group(a, f["group"].strip(), f["plan"])
            elif self.path == "/groups/delete":
                unmap_group(a, f["group"].strip())
            elif self.path == "/service-keys":
                key, _ = mint_service_key(a, f["owner"].strip(), f["name"].strip(), f["plan"], int(f["days"]))
                return self.dashboard(new_key=key)
            elif self.path == "/keys/revoke":
                revoke("id = %s", (f["id"],), a, "revoked by an admin")
            elif self.path == "/limits":
                write_limits(a)
            else:
                return self.send(404, "not found", "text/plain")
        except psycopg.errors.ForeignKeyViolation:
            return self.page("Not done", "<p class=err>That plan is still mapped to a group.</p>"
                             "<p><a href=/>back</a></p>", 409)
        except Exception as e:
            log("admin error:", repr(e))
            return self.page("Error", f"<p class=err>{esc(e)}</p><p><a href=/>back</a></p>", 400)
        self.redirect("/")


    def api_get(self):
        """Read-only JSON views for the admin playbooks (admin.yml)."""
        if self.headers.get("X-MaaS-Admin") != "1":
            return self.send(403, json.dumps({"error": "needs X-MaaS-Admin: 1"}), "application/json")
        path, _, query = self.path.partition("?")
        after = (urllib.parse.parse_qs(query).get("after") or [None])[0]
        if path == "/api/plans":
            out = [{"name": n, "models": m, "tokens": t, "window": w, "days": d, "priority": pr,
                    "max_request_tokens": mrt, "max_requests": mreq, "hourly_tokens": ht,
                    "hourly_cap": hourly_cap(t, w, ht)}
                   for n, m, t, w, d, pr, mrt, mreq, ht in db(
                       "SELECT name, models, tokens, window_seconds, max_key_days, priority, max_request_tokens, "
                       "max_requests, hourly_tokens FROM plans ORDER BY name", fetch=True)]
        elif path == "/api/groups":
            out = [{"group": g, "plan": p} for g, p in db(
                "SELECT group_name, plan FROM group_plans ORDER BY group_name", fetch=True)]
        elif path == "/api/keys":
            out = [{"id": str(k), "kind": kind, "owner": o, "name": n, "groups": g, "plan": p,
                    "created": c.isoformat(), "expires": e.isoformat(),
                    "revoked": r.isoformat() if r else None, "reason": why,
                    "last_used": u.isoformat() if u else None}
                   for k, kind, o, n, g, p, c, e, r, why, u in key_rows(after=after)]
        elif path == "/api/audit":
            out = [{"at": at.isoformat(), "actor": a, "action": act, "target": t, "details": d} for at, a, act, t, d in db(
                "SELECT at, actor, action, target, details FROM audit ORDER BY id DESC LIMIT 200", fetch=True)]
        else:
            return self.send(404, json.dumps({"error": "not found"}), "application/json")
        self.send(200, json.dumps(out), "application/json")

    def api(self):
        """JSON admin API for scripts. A browser can't send the custom header or a
        JSON body cross-site without a CORS preflight, which this server never
        allows, so no form token is needed."""
        if self.headers.get("X-MaaS-Admin") != "1" or "json" not in self.headers.get("Content-Type", ""):
            return self.send(403, json.dumps({"error": "needs X-MaaS-Admin: 1 and a JSON body"}), "application/json")
        try:
            n = min(self.content_length(), 64 * 1024)
        except ValueError:
            return self.send(400, json.dumps({"error": "bad Content-Length"}), "application/json")
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
            a = (self.headers.get("X-MaaS-Actor") or "").strip()[:64] or ADMIN_ACTOR + " api"
            if self.path == "/api/plans":
                set_plan(a, req["name"], req.get("models") or [],
                         int(req["tokens"]), int(req.get("window", 60)), int(req.get("days", 90)),
                         int(req.get("priority") or 0), optional_int(req.get("max_request_tokens")),
                         optional_int(req.get("max_requests")), optional_int(req.get("hourly_tokens")))
                out = {"plan": req["name"]}
            elif self.path == "/api/plans/delete":
                delete_plan(req["name"], a)
                out = {"deleted": req["name"]}
            elif self.path == "/api/plans/tui-cleanup":
                rows = db("SELECT p.name FROM plans p WHERE p.name LIKE 'tui-%%' AND NOT EXISTS "
                          "(SELECT 1 FROM api_keys k WHERE k.plan = p.name AND k.revoked_at IS NULL "
                          "AND k.expires_at > now())", fetch=True)
                for (name,) in rows:
                    delete_plan(name, a)
                out = {"deleted": [name for name, in rows]}
            elif self.path == "/api/groups":
                map_group(a, req["group"], req["plan"])
                out = {"group": req["group"], "plan": req["plan"]}
            elif self.path == "/api/groups/delete":
                unmap_group(a, req["group"])
                out = {"unmapped": req["group"]}
            elif self.path == "/api/limits":
                write_limits(a)
                out = {"limits": "written"}
            elif self.path == "/api/service-keys":
                key, kid = mint_service_key(a, req["owner"], str(req.get("name", "key")),
                                            req["plan"], int(req.get("days", 1)))
                out = {"id": kid, "key": key}
            elif self.path == "/api/keys/revoke":
                out = {"revoked": revoke("id = %s", (req["id"],), a, "revoked by an admin script")}
            elif self.path == "/api/users/reconcile":
                groups = req.get("groups") or []
                revoked = reconcile(req["owner"], groups, actor=a)
                db("DELETE FROM sessions WHERE username = %s", (req["owner"],))
                audit(a, "user.reconcile", req["owner"], groups=groups)
                out = {"revoked": revoked}
            elif self.path == "/api/users/revoke":
                out = {"revoked": revoke("owner = %s AND revoked_at IS NULL", (req["owner"],), a,
                                         "revoked by an admin script")}
                db("DELETE FROM sessions WHERE username = %s", (req["owner"],))
            else:
                return self.send(404, json.dumps({"error": "not found"}), "application/json")
        except psycopg.errors.ForeignKeyViolation:
            return self.send(409, json.dumps({"error": "plan still mapped to a group"}), "application/json")
        except Exception as e:
            log("admin api error:", repr(e))
            return self.send(400, json.dumps({"error": str(e)}), "application/json")
        self.send(200, json.dumps(out), "application/json")


# ── key portal ───────────────────────────────────────────────────────────────

OIDC = {}


def oidc_ctx():
    return ssl.create_default_context(cafile=SERVICE_CA)


def discover_oidc():
    """Endpoints from the realm's discovery document, fetched over the Service.
    Keycloak names the browser endpoints with its public hostname and the
    backchannel ones with the host it was asked on."""
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
    """Trade the code for tokens and return the ID token's claims.

    The ID token comes straight from the token endpoint over TLS, verified
    against the service CA, so its issuer is authenticated by TLS (OIDC Core
    3.1.3.7) and its signature need not be checked again; its issuer, audience,
    expiry and nonce are."""
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
        raise ValueError("ID token not issued to this portal")
    if claims.get("exp", 0) < time.time():
        raise ValueError("ID token expired")
    if not nonce or not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ValueError("ID token nonce doesn't match this sign-in")
    if not claims.get("email") or claims.get("email_verified") is not True:
        raise ValueError("your account has no verified email address")
    return claims


def reconcile(username, groups, actor=None):
    """At sign-in: keep the user's active keys in step with their current groups."""
    eff = effective(groups)
    if not eff:
        return revoke("kind = 'user' AND owner = %s", (username,), actor or username,
                      "no longer in an allowed group")
    # Only keys whose groups changed need their cached checks dropped.
    rows = db("UPDATE api_keys SET groups = %s WHERE kind = 'user' AND owner = %s AND revoked_at IS NULL "
              "AND groups IS DISTINCT FROM %s RETURNING key_hash", (eff[5], username, eff[5]), fetch=True)
    for (key_hash,) in rows:
        notify(key_hash)
    return 0


class Keys(Base):
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
            return self.page("MaaS keys", "<p>Sign in to manage your MaaS API keys.</p>"
                             "<p><a href=/login><button>Sign in with GitHub</button></a></p>")
        self.home(s)

    def home(self, s, new_key=None, error=None):
        csrf = f"<input type=hidden name=csrf value='{s['csrf']}'>"
        out = (f"<p>Signed in as <b>{esc(s['username'])}</b> {esc(s['name'])} <form class=inline method=post "
               f"action=/logout>{csrf}<button>sign out</button></form></p>")
        eff = effective(s["groups"])
        if not eff:
            return self.page("MaaS keys", out + "<p class=err>None of your groups has access to this MaaS. "
                             "Ask the namespace's members to map one of your groups to a plan.</p>", 403)
        plan, tokens, window, models, maxd, via = eff
        out += (f"<p>Through {esc(', '.join(via))}: plan <b>{esc(plan)}</b>, {tokens:,} tokens per {window}s, "
                f"models: {esc(', '.join(models)) or 'all'}. Keys last up to {maxd} days.</p>")
        if error:
            out += f"<p class=err>{esc(error)}</p>"
        if new_key:
            out += ("<p><b>Your new key. Copy it now; it is not shown again.</b></p>"
                    f"<p class=key>{esc(new_key)}</p>")
        out += "<h2>Your keys</h2><table><tr><th>Name</th><th>Created</th><th>Last used</th><th>Status</th><th></th></tr>"
        now = datetime.datetime.now(datetime.timezone.utc)
        for kid, _, _, name, _, _, created, expires, revoked, reason, last_used in key_rows(
                "WHERE kind = 'user' AND owner = %s", (s["username"],)):
            used = f"{last_used:%Y-%m-%d %H:%M}" if last_used else "never"
            btn = "" if revoked else (f"<form class=inline method=post action=/keys/revoke>{csrf}"
                                      f"<input type=hidden name=id value='{kid}'><button>revoke</button></form>")
            out += (f"<tr><td>{esc(name)}</td><td>{created:%Y-%m-%d %H:%M}</td><td>{used}</td>"
                    f"<td>{key_status(expires, revoked, reason, now)}</td><td>{btn}</td></tr>")
        out += (f"</table><form method=post action=/keys>{csrf}<input name=name placeholder='key name, e.g. "
                f"laptop' required> expires in <input name=days type=number min=1 max={maxd} value={min(30, maxd)} "
                "required> days <button>create key</button></form>")
        self.page("MaaS keys", out)

    def login(self):
        state, verifier, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(48), secrets.token_urlsafe(24)
        browser = secrets.token_urlsafe(24)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        db("INSERT INTO oauth_pending (state, verifier, nonce, browser_hash, expires_at) "
           "VALUES (%s, %s, %s, %s, now() + interval '10 minutes')", (state, verifier, nonce, hash_value(browser)))
        # kc_idp_hint sends users straight to GitHub, past Keycloak's login form.
        q = urllib.parse.urlencode({"client_id": OIDC_CLIENT_ID, "response_type": "code",
                                    "scope": "openid email profile", "redirect_uri": redirect_uri(),
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
            username, identity = claims["email"].lower(), claims["sub"]
            name, groups = claims.get("name") or claims.get("preferred_username") or "", claims.get("groups") or []
        except Exception as e:
            log("sign-in error:", repr(e))
            return self.page("Sign-in failed", f"<p class=err>{esc(e)}</p><p><a href=/>start again</a></p>", 502)
        sid = secrets.token_urlsafe(32)
        db("INSERT INTO sessions (id_hash, username, identity, full_name, groups, csrf, expires_at) "
           "VALUES (%s, %s, %s, %s, %s, %s, now() + make_interval(hours => %s))",
           (hash_value(sid), username, identity, name, groups, secrets.token_urlsafe(24), SESSION_HOURS))
        # Same email, different Keycloak account (the old user was deleted):
        # the old account's keys and sessions don't pass to the new one.
        revoke("kind = 'user' AND owner = %s AND identity IS DISTINCT FROM %s", (username, identity),
               username, "a different Keycloak account signed in with this email")
        db("DELETE FROM sessions WHERE username = %s AND identity IS DISTINCT FROM %s", (username, identity))
        revoked = reconcile(username, groups)
        audit(username, "login", username, identity=identity, groups=groups, keys_revoked=revoked)
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
            audit(s["username"], "logout", s["username"])
            # Also end the Keycloak session, then come back here.
            end = OIDC.get("end_session_endpoint")
            target = (end + "?" + urllib.parse.urlencode({"client_id": OIDC_CLIENT_ID,
                                                          "post_logout_redirect_uri": f"https://{ROUTE_HOST}/"})
                      if end else "/")
            return self.redirect(target, headers=[("Set-Cookie", "__Host-maas_session=; Path=/; Max-Age=0; Secure; HttpOnly")])
        eff = effective(s["groups"])
        if not eff:
            return self.send(403, "none of your groups has access to this MaaS", "text/plain")
        if self.path == "/keys":
            try:
                days = int(f.get("days", "0"))
            except ValueError:
                days = 0
            if not 1 <= days <= eff[4]:
                return self.home(s, error=f"Keys may last 1 to {eff[4]} days.")
            key, kid = new_key(), uuid.uuid4()
            try:
                create_key(s["username"], kid, "user", s["username"], s["identity"],
                           (f.get("name") or "key").strip()[:64], eff[5], None, key, days,
                           {"groups": eff[5], "days": days})
            except ValueError as e:
                return self.home(s, error=f"Not created: {e}.")
            return self.home(s, new_key=key)
        if self.path == "/keys/revoke":
            revoke("id = %s AND kind = 'user' AND owner = %s", (f.get("id"), s["username"]),
                   s["username"], "revoked by its owner")
            return self.redirect("/")
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


CHECKS = {}  # (owner, plan, result) -> count, for /metrics
CHECKS_LOCK = threading.Lock()


def volume_stats():
    """(labels, free bytes, size bytes) for each storage volume mounted under
    /volumes (read-only, for these metrics only)."""
    out = []
    if os.path.isdir("/volumes"):
        for name in os.listdir("/volumes"):
            try:
                st = os.statvfs(f"/volumes/{name}")
                out.append(({"volume": name}, st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize))
            except OSError:
                pass
    return out


def limitador_plans():
    """The plan names Limitador has a limit for, or None when that cannot be
    confirmed: a plan without a loaded limit has no budget at all."""
    now = time.time()
    fetched, names = _LIMITS_CACHE
    if names is not None and now - fetched < 10:
        return names
    try:
        with urllib.request.urlopen(LIMITADOR_URL + "/limits/maas", timeout=1) as r:
            limits = json.load(r)
        names = {c.split("'")[3] for lim in limits for c in lim.get("conditions", [])
                 if c.startswith("descriptors[0]['plan'] == '")}
        _LIMITS_CACHE[0] = now
        _LIMITS_CACHE[1] = names
        return names
    except Exception:
        if names is not None and now - fetched < 300:
            return names
        return None


_LIMITS_CACHE = [0.0, None]

# The key check reads a request body only for the model, the streaming fields
# and the token caps. Bodies are counted first, so a body made of millions of
# tiny values is refused before json.loads builds them, and only a fixed
# number of bodies are held and parsed at once.
BODY_MAX_BYTES = 4 * 1024 * 1024     # the gateway's max_request_bytes
BODY_MAX_STRINGS = 200_000
BODY_MAX_CONTAINERS = 100_000
BODY_MAX_COMMAS = 1_000_000          # an upper bound on array elements and object members
BODY_WAIT_SECONDS = 1.0              # under the gateway's 2 s key-check timeout
# Two lanes, so ordinary requests never queue behind large ones. Memory held
# by bodies is then at most 32 x 256 KiB + 2 x 4 MiB before parsing.
SMALL_BODY_BYTES = 256 * 1024
SMALL_BODY_SLOTS = threading.BoundedSemaphore(32)
LARGE_BODY_SLOTS = threading.BoundedSemaphore(2)
TOKEN_RE = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"|[{\[]')
TOP_FIELDS = ("model", "stream", "stream_options", "max_tokens", "max_completion_tokens")


def within_budget(text):
    """Whether json.loads may build this body: counts strings and containers
    and stops at the first limit."""
    if text.count(",") > BODY_MAX_COMMAS:
        return False
    strings = containers = 0
    for m in TOKEN_RE.finditer(text):
        if text[m.start()] == '"':
            strings += 1
            if strings > BODY_MAX_STRINGS:
                return False
        else:
            containers += 1
            if containers > BODY_MAX_CONTAINERS:
                return False
    return True


class BodyError(ValueError):
    """A body the key check refuses; the message goes back to the client."""


class _Obj(dict):
    """A parsed JSON object that remembers whether its include_usage key was
    repeated or differently capitalised."""
    ambiguous_usage = False


def _int_field(name, v):
    # bool is an int in Python, but not a token count.
    if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
        raise BodyError(f"{name} must be an integer")
    return v


def request_fields(text):
    """The fields the key check reads, read strictly: one JSON object; model,
    stream, stream_options, max_tokens, max_completion_tokens and
    stream_options.include_usage each at most once and in lower case (Go's
    decoder matches keys case-insensitively, Python keeps the last repeat);
    model a string, stream and include_usage booleans, the caps integers.
    Returns a dict with model, stream, include_usage, max_tokens and
    max_completion_tokens."""
    if not within_budget(text):
        raise BodyError("request body has too many JSON values")
    last = [None]  # pairs of the object closed last: the top level, once parsing ends

    def pairs(items):
        obj = _Obj(items)
        last[0] = items
        usage = [k for k, _ in items if k.lower() == "include_usage"]
        if usage and (len(usage) > 1 or usage[0] != "include_usage"):
            obj.ambiguous_usage = True
        return obj

    try:
        doc = json.loads(text, object_pairs_hook=pairs)
    except (ValueError, RecursionError):
        raise BodyError("request body is not valid JSON") from None
    if not isinstance(doc, dict):
        raise BodyError("request body is not a JSON object")
    names = [k for k, _ in last[0] if k.lower() in TOP_FIELDS]
    if len(names) != len(set(names)) or any(k not in TOP_FIELDS for k in names):
        raise BodyError(", ".join(TOP_FIELDS) + " must each appear at most once, in lower case")
    model, stream, opts = doc.get("model"), doc.get("stream"), doc.get("stream_options")
    if model is not None and not isinstance(model, str):
        raise BodyError("model must be a string")
    if stream is not None and not isinstance(stream, bool):
        raise BodyError("stream must be true or false")
    if opts is not None and not isinstance(opts, dict):
        raise BodyError("stream_options must be an object")
    if opts is not None and opts.ambiguous_usage:
        raise BodyError("stream_options.include_usage must appear at most once, in lower case")
    include_usage = (opts or {}).get("include_usage")
    if include_usage is not None and not isinstance(include_usage, bool):
        raise BodyError("stream_options.include_usage must be true or false")
    return {"model": model, "stream": stream is True, "include_usage": include_usage is True,
            "max_tokens": _int_field("max_tokens", doc.get("max_tokens")),
            "max_completion_tokens": _int_field("max_completion_tokens", doc.get("max_completion_tokens"))}


def count_check(result, headers=None):
    h = dict(headers or [])
    k = (h.get("x-maas-user", ""), h.get("x-maas-plan", ""), result)
    with CHECKS_LOCK:
        CHECKS[k] = CHECKS.get(k, 0) + 1


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


class Usage(Keys):
    """The user dashboard. Every query is built here and fixed to the signed-in
    user; nothing the browser sends ends up in a query."""

    TITLE = "MaaS usage"

    def do_GET(self):
        path = urllib.parse.urlparse(self.path)
        if path.path in ("/login", "/callback"):
            return Keys.do_GET(self)
        if path.path != "/":
            return self.send(404, "not found", "text/plain")
        _, s = self.session()
        if not s:
            return self.page(self.TITLE, "<p>Sign in to see your MaaS usage.</p>"
                             "<p><a href=/login><button>Sign in with GitHub</button></a></p>")
        self.home(s)

    def home(self, s, new_key=None, error=None):
        csrf = f"<input type=hidden name=csrf value='{s['csrf']}'>"
        out = (f"<p>Signed in as <b>{esc(s['username'])}</b> {esc(s['name'])} <form class=inline method=post "
               f"action=/logout>{csrf}<button>sign out</button></form></p>")
        eff = effective(s["groups"])
        if not eff:
            return self.page(self.TITLE, out + "<p class=err>None of your groups has access to this MaaS.</p>", 403)
        plan, tokens, window, models, maxd, via = eff
        u = promql_str(s["username"])
        # This window, live from Limitador's own counter for this user and plan.
        used, left_s = 0, None
        try:
            for c in json.loads(urllib.request.urlopen(LIMITADOR_URL + "/counters/maas", timeout=5).read()):
                # The window's counter, not the plan's hourly cap.
                if (c.get("set_variables") or {}).get("descriptors[0]['user']") == s["username"] and \
                        f"descriptors[0]['plan'] == '{plan}'" in c["limit"].get("conditions", []) and \
                        c["limit"].get("seconds") == window:
                    rem = c["remaining"]
                    rem = rem - 2**64 if rem >= 2**63 else rem
                    used, left_s = c["limit"]["max_value"] - rem, c.get("expires_in_seconds")
        except Exception as e:
            log("limitador counters:", repr(e)[:120])
        frac = min(used / tokens, 1) if tokens else 0
        out += (f"<h2>This window</h2><p>Plan <b>{esc(plan)}</b>: {tokens:,} tokens per {window}s, models "
                f"{esc(', '.join(models)) or 'all'}.</p>"
                f'<div style="background:#eee;border-radius:4px;height:14px;width:100%;max-width:640px">'
                f'<div style="background:{"#c0392b" if used >= tokens else "#5b7fb8"};height:14px;border-radius:4px;'
                f'width:{frac * 100:.1f}%"></div></div>'
                f"<p>{used:,} of {tokens:,} tokens used" + (f"; window resets in {left_s}s" if left_s else "") + ".</p>")
        day = {k: scalar(q) for k, q in (
            ("tokens", f"sum(increase(authorized_hits{{user={u}}}[1d]))"),
            ("requests", f"sum(increase(report_calls{{user={u}}}[1d]))"),
            ("refused", f"sum(increase(limited_calls{{user={u}}}[1d]))"),
            ("ttft", f"histogram_quantile(0.5, sum by (le) (rate(llm_d_epp_request_ttft_seconds_bucket{{fairness_id={u}}}[1d])))"))}
        fmt = lambda v, f="{:,.0f}": "–" if v is None or v != v else f.format(v)
        out += ("<h2>Last 24 hours</h2><table><tr><th>Tokens</th><th>Requests</th><th>Refused (429)</th>"
                "<th>Median time to first token</th></tr>"
                f"<tr><td>{fmt(day['tokens'])}</td><td>{fmt(day['requests'])}</td><td>{fmt(day['refused'])}</td>"
                f"<td>{fmt(day['ttft'], '{:.2f} s')}</td></tr></table>")
        try:
            end = int(time.time()) // 86400 * 86400 + 86400
            res = prom("/api/v1/query_range", query=f"sum(increase(authorized_hits{{user={u}}}[1d]))",
                       start=end - 30 * 86400, end=end, step=86400)
            pts = [(int(t), float(v)) for t, v in res[0]["values"]] if res else []
        except Exception as e:
            log("usage range query:", repr(e)[:160])
            pts = []
        out += "<h2>Tokens per day, last 30 days</h2>" + bars_svg(pts)
        out += "<h2>Your keys</h2><table><tr><th>Name</th><th>Created</th><th>Last used</th><th>Status</th></tr>"
        now = datetime.datetime.now(datetime.timezone.utc)
        for kid, _, _, name, _, _, created, expires, revoked, reason, last_used in key_rows(
                "WHERE kind = 'user' AND owner = %s", (s["username"],)):
            used = f"{last_used:%Y-%m-%d %H:%M}" if last_used else "never"
            out += (f"<tr><td>{esc(name)}</td><td>{created:%Y-%m-%d %H:%M}</td><td>{used}</td>"
                    f"<td>{key_status(expires, revoked, reason, now)}</td></tr>")
        out += "</table><p class=muted>Manage keys in the key portal.</p>"
        self.page(self.TITLE, out)

    def do_POST(self):
        if self.path != "/logout":
            return self.send(404, "not found", "text/plain")
        return Keys.do_POST(self)


CACHE = OrderedDict()   # key hash -> (expires, headers), bounded
CACHE_MAX = 50_000
MISSES = OrderedDict()  # key hash -> (expires, None), bounded, so a flood of
MISSES_MAX = 5_000      # bad keys can't push the good ones out
CACHE_LOCK = threading.Lock()

LAST_USED = {}  # key hash -> when its last_used_at was last written
LAST_USED_LOCK = threading.Lock()

_LAST_DB_LOG = [0.0]


def log_db_error(e):
    """One line per outage, not a traceback per request."""
    if time.time() - _LAST_DB_LOG[0] > 10:
        _LAST_DB_LOG[0] = time.time()
        log("key check database:", repr(e)[:160])


def listen_for_revocations():
    """Drop cached key checks the moment a key is revoked or plans change."""
    while True:
        try:
            with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
                conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
                log("listening for key changes")
                for n in conn.notifies():
                    with CACHE_LOCK:
                        if n.payload == "*":
                            CACHE.clear()
                        else:
                            CACHE.pop(n.payload, None)
        except Exception as e:
            log("revocation listener:", repr(e), "- reconnecting")
            # The cache stays: entries refresh from lookups within seconds of
            # the database returning, and clearing would leave nothing to
            # serve while it is down.
            time.sleep(3)


def lookup(key_hash):
    """(expires_at, headers for the gateway, limits the key check applies
    itself), or None for an unknown, revoked or expired key."""
    rows = db("SELECT id, kind, owner, groups, plan, expires_at FROM api_keys WHERE key_hash = %s "
              "AND revoked_at IS NULL AND expires_at > now()", (key_hash,), fetch=True)
    if not rows:
        return None
    kid, kind, owner, groups, plan, expires_at = rows[0]
    if kind == "user":
        eff = effective(groups)
        if not eff:
            return None
        plan = eff[0]
    p = db("SELECT models, window_seconds, max_request_tokens FROM plans WHERE name = %s", (plan,), fetch=True)
    if not p:
        return None
    models, window, max_request_tokens = p[0]
    # The picker labels its metrics, and shares capacity, by this ID; the key's
    # id goes to the gateway's access log, and the plan's window becomes the
    # budget refusal's Retry-After.
    return (expires_at, [("x-maas-user", owner), ("x-maas-plan", plan), ("x-maas-models", ",".join(models or [])),
                         ("x-maas-key-id", str(kid)), ("x-maas-window", str(window)),
                         ("x-llm-d-inference-fairness-id", owner)],
            {"max_request_tokens": max_request_tokens})


class Validate(Base):
    """Answers the gateway's ext_authz checks under /check: 200 with identity
    headers, 401 for a bad key, 403 for a model outside the key's plan. The
    gateway prefixes every client path with /check, so a client can't reach
    /healthz through it. HTTP/1.1 so Envoy reuses the connection."""
    protocol_version = "HTTP/1.1"

    def check(self):
        if self.path == "/healthz" and self.command == "GET":
            return self.healthz()
        if self.path == "/livez" and self.command == "GET":
            return self.livez()
        if self.path == "/metrics" and self.command == "GET":
            with CHECKS_LOCK:
                samples = [({"owner": o, "plan": p, "result": r}, n) for (o, p, r), n in sorted(CHECKS.items())]
            return self.send(200, exposition([("maas_key_checks_total", "counter",
                                                "Key checks answered for the gateway, by key owner, plan and result.",
                                                samples)]), "text/plain; version=0.0.4")
        if not (self.path == "/check" or self.path.startswith("/check/")):
            return self.send(404, "not found", "text/plain")
        try:
            n = self.content_length()
        except ValueError:
            return self.send(400, "bad Content-Length", "text/plain")
        if n > BODY_MAX_BYTES:
            return self.send(413, "request body too large", "text/plain", headers=[("Connection", "close")])
        if not n:
            return self.check_key(b"")
        # The body stays unread until a slot is free, so a queue of large bodies
        # can't fill the pod's memory; a check that can't get a slot in time is
        # answered 503 and its connection closed (its body is unread).
        slots = SMALL_BODY_SLOTS if n <= SMALL_BODY_BYTES else LARGE_BODY_SLOTS
        if not slots.acquire(timeout=BODY_WAIT_SECONDS):
            return self.send(503, "key check busy, retry shortly", "text/plain", headers=[("Connection", "close")])
        try:
            return self.check_key(self.rfile.read(n))
        finally:
            slots.release()

    def check_key(self, body):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer maas-"):
            count_check("missing")
            return self.send(401, "missing key", "text/plain")
        key = auth[7:].strip()
        if not KEY_RE.fullmatch(key):
            count_check("invalid")
            return self.send(401, "invalid, expired or revoked key", "text/plain")
        h = hash_value(key)
        now = time.time()
        with CACHE_LOCK:
            hit = CACHE.get(h) or MISSES.get(h)
        value = hit[1] if hit else None
        if not hit or hit[0] < now:
            try:
                value = lookup(h)
            except Exception as e:
                # The database is down: keep serving the last known result for
                # a bounded time, so an outage doesn't take the API down too.
                log_db_error(e)
                if (hit and hit[1] and hit[1][1]
                        and hit[1][0] > datetime.datetime.now(datetime.timezone.utc)
                        and now < hit[0] + STALE_SECONDS):
                    value = hit[1]
                else:
                    return self.send(503, "key check unavailable, retry shortly", "text/plain")
            else:
                hit = (now + KEY_CACHE_SECONDS, value)
                with CACHE_LOCK:
                    store, limit = (CACHE, CACHE_MAX) if value else (MISSES, MISSES_MAX)
                    store[h] = hit
                    store.move_to_end(h)
                    while len(store) > limit:
                        store.popitem(last=False)
                if value:
                    with LAST_USED_LOCK:
                        due = now - LAST_USED.get(h, 0) >= 60
                        if due:
                            LAST_USED[h] = now
                    if due:
                        try:
                            db("UPDATE api_keys SET last_used_at = now() WHERE key_hash = %s", (h,))
                        except Exception as e:
                            log_db_error(e)
                            with LAST_USED_LOCK:
                                LAST_USED.pop(h, None)  # try again on the next check
        if not value or not value[1]:
            count_check("invalid")
            return self.send(401, "invalid, expired or revoked key", "text/plain")
        _, headers, limits = value
        plan, models = dict(headers)["x-maas-plan"], dict(headers)["x-maas-models"]
        loaded = limitador_plans()
        if loaded is None:
            count_check("limits_unknown", headers)
            return self.send(503, "cannot confirm the plan is active, retry shortly", "text/plain")
        if plan not in loaded:
            count_check("plan_inactive", headers)
            return self.send(403, "plan not active yet", "text/plain")
        if body:
            try:
                fields = request_fields(body.decode("utf-8", "replace"))
            except BodyError as e:
                return self.send(400, str(e), "text/plain")
            if fields["stream"] and not fields["include_usage"]:
                return self.send(400, "streaming requests must set stream_options.include_usage to true",
                                 "text/plain")
            cap = limits.get("max_request_tokens")
            if cap:
                asked = [v for v in (fields["max_tokens"], fields["max_completion_tokens"]) if v is not None]
                if not asked:
                    return self.send(400, f"your plan needs max_tokens (at most {cap}) on every request",
                                     "text/plain")
                if max(asked) > cap:
                    return self.send(400, f"max_tokens is above your plan's cap of {cap}", "text/plain")
            model = fields["model"]
            if models and model not in models.split(","):
                count_check("model_denied", headers)
                return self.send(403, f"model {model!r} is not in your plan", "text/plain")
        if not self.path.startswith("/check/body"):
            count_check("ok", headers)
        self.send(200, "ok", "text/plain", headers=headers)

    do_GET = do_POST = check


class AdminMetrics(Base):
    """The admin portal's metrics-only port: plans, group mappings and the key
    inventory, read from the database on each scrape. Nothing else is served."""

    def do_GET(self):
        if self.path == "/healthz":
            return self.healthz()
        if self.path != "/metrics":
            return self.send(404, "not found", "text/plain")
        plans = db("SELECT name, tokens, window_seconds, max_key_days, models FROM plans ORDER BY name", fetch=True)
        groups = db("SELECT group_name, plan FROM group_plans ORDER BY group_name", fetch=True)
        keys = db("SELECT id, kind, owner, name, groups, plan, extract(epoch FROM expires_at), "
                  "revoked_at IS NOT NULL, expires_at < now() FROM api_keys ORDER BY created_at", fetch=True)
        loaded = limitador_plans()
        vols = volume_stats()
        eff_cache = {}
        info, expiry, counts = [], [], {}
        for kid, kind, owner, name, kgroups, plan, exp, revoked, expired in keys:
            if kind == "user":
                gk = tuple(sorted(kgroups or []))
                if gk not in eff_cache:
                    eff_cache[gk] = effective(kgroups or [])
                eff = eff_cache[gk]
                plan = eff[0] if eff else ""
            status = "revoked" if revoked else "expired" if expired else "active"
            counts[(kind, status)] = counts.get((kind, status), 0) + 1
            if status != "active":
                continue  # one series per active key; maas_keys counts the rest
            info.append(({"key_id": kid, "kind": kind, "owner": owner, "name": name, "plan": plan or "",
                          "status": status}, 1))
            expiry.append(({"key_id": kid, "owner": owner}, int(exp)))
        db_used, db_max = db("SELECT (SELECT count(*) FROM pg_stat_activity), "
                             "current_setting('max_connections')::int", fetch=True)[0]
        body = exposition([
            ("maas_plan_budget_tokens", "gauge", "Each plan's token budget per window, per user.",
             [({"plan": n}, t) for n, t, w, d, m in plans]),
            ("maas_plan_window_seconds", "gauge", "Each plan's budget window.",
             [({"plan": n}, w) for n, t, w, d, m in plans]),
            ("maas_plan_max_key_days", "gauge", "Each plan's longest allowed key life.",
             [({"plan": n}, d) for n, t, w, d, m in plans]),
            ("maas_plan_models", "gauge", "Models each plan allows (empty: all).",
             [({"plan": n, "models": ",".join(m)}, 1) for n, t, w, d, m in plans]),
            ("maas_plan_limit_loaded", "gauge", "1 when Limitador has a limit loaded for the plan.",
             [({"plan": n}, 1 if loaded and n in loaded else 0) for n, t, w, d, m in plans]),
            ("maas_group_plan", "gauge", "Keycloak groups mapped to plans.",
             [({"group": g, "plan": p}, 1) for g, p in groups]),
            ("maas_key_info", "gauge", "Every active key: owner, kind (user or service), plan now in force.", info),
            ("maas_key_expiry_timestamp_seconds", "gauge", "When each active key expires.", expiry),
            ("maas_keys", "gauge", "Keys by kind and status.",
             [({"kind": k, "status": st}, n) for (k, st), n in sorted(counts.items())]),
            ("maas_volume_free_bytes", "gauge", "Free space on the shared storage volume.",
             [(lbl, free) for lbl, free, _ in vols]),
            ("maas_volume_size_bytes", "gauge", "Size of the shared storage volume.",
             [(lbl, size) for lbl, _, size in vols]),
            ("maas_db_connections", "gauge", "Connections open on the portal database.", [({}, db_used)]),
            ("maas_db_max_connections", "gauge", "The portal database's max_connections.", [({}, db_max)]),
            ("maas_reconcile_refused_users", "gauge",
             "Users the last reconciler pass would have revoked but refused to (0: none).",
             [({}, RECONCILER["refused"])]),
            ("maas_reconcile_last_success_timestamp_seconds", "gauge",
             "When the background user reconciliation last finished (0: never).",
             [({}, RECONCILER["last_success"])]),
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
            n = db("DELETE FROM api_keys WHERE expires_at < now() - make_interval(days => %s) RETURNING id",
                   (PURGE_EXPIRED_KEYS_AFTER_DAYS,), fetch=True)
            if n:
                audit("housekeeping", "key.purge", "", count=len(n))
        except Exception as e:
            log("housekeeping:", repr(e))
        time.sleep(3600)


RECONCILER_INTERVAL = 600
RECONCILER = {"last_success": 0.0, "users": 0, "refused": 0}
# Keycloak's admin REST API shares the backchannel's host and realm.
ADMIN_API = OIDC_BACKCHANNEL.replace("/realms/", "/admin/realms/", 1)


def kc_token():
    """A client-credentials token for the maas-reconciler service account,
    which kcadm.sh's sync step grants view-users to."""
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": "maas-reconciler",
        "client_secret": RECONCILER_CLIENT_SECRET}).encode()
    with urllib.request.urlopen(OIDC_BACKCHANNEL.rstrip("/") + "/protocol/openid-connect/token",
                                data=body, context=oidc_ctx(), timeout=15) as r:
        return json.loads(r.read())["access_token"]


def kc_get(token, path):
    """One Keycloak admin REST call; None means the user is gone."""
    req = urllib.request.Request(ADMIN_API + path, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, context=oidc_ctx(), timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def reconciler():
    """Without waiting for a sign-in, keep users' keys in step with Keycloak:
    a user who was deleted or disabled loses their keys and sessions, and
    everyone else's key groups refresh, exactly as at sign-in."""
    while True:
        try:
            rows = db("SELECT DISTINCT identity, owner FROM api_keys "
                      "WHERE kind = 'user' AND revoked_at IS NULL AND expires_at > now() "
                      "AND identity IS NOT NULL", fetch=True)
            token = kc_token()
            users = {i: kc_get(token, f"/users/{i}") for i, _ in rows}
            gone = [(i, o) for i, o in rows if users[i] is None or not users[i].get("enabled")]
            # A re-imported realm or a wrong admin URL makes every user look
            # gone; refuse to revoke most users' keys in one pass.
            if len(gone) > max(3, len(rows) // 2):
                log("reconciler: refusing to revoke", len(gone), "of", len(rows), "users in one pass; check Keycloak")
                RECONCILER["refused"] = len(gone)
            else:
                RECONCILER["refused"] = 0
                for identity, owner in gone:
                    n = revoke("kind = 'user' AND identity = %s AND revoked_at IS NULL", (identity,),
                               "reconciler", "user is gone or disabled in Keycloak")
                    db("DELETE FROM sessions WHERE username = %s", (owner,))
                    if n:
                        log("reconciler: revoked", n, "key(s) of", owner)
            gone_ids = {i for i, _ in gone}
            for identity, owner in rows:
                if identity in gone_ids:
                    continue
                groups = [g["name"] for g in kc_get(token, f"/users/{identity}/groups") or []]
                if reconcile(owner, groups, actor="reconciler"):
                    db("DELETE FROM sessions WHERE username = %s", (owner,))
                    log("reconciler: revoked the keys of", owner)
                else:
                    # Open sessions pick up the narrowed groups too.
                    db("UPDATE sessions SET groups = %s WHERE username = %s AND identity = %s",
                       (groups, owner, identity))
            RECONCILER["last_success"] = time.time()
            RECONCILER["users"] = len(rows)
            log("reconciler: pass complete,", len(rows), "user(s)")
        except Exception as e:
            log("reconciler:", repr(e)[:160])
        time.sleep(RECONCILER_INTERVAL)


class Server(http.server.ThreadingHTTPServer):
    tls = None
    # Bursts of checks arrive faster than they are answered; the kernel holds
    # the rest here instead of resetting them.
    request_queue_size = 1024

    def finish_request(self, request, client_address):
        # The TLS handshake runs in the connection's own thread, with a
        # timeout, so a stalled client can't hold up everyone else.
        if self.tls:
            request.settimeout(30)
            try:
                request = self.tls.wrap_socket(request, server_side=True)
            except (OSError, ssl.SSLError):
                return  # a health check or stray client that hung up
        try:
            super().finish_request(request, client_address)
        finally:
            if self.tls:
                request.close()

    def handle_error(self, request, client_address):
        # A client leaving mid-request isn't an error worth a traceback.
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
    """On SIGTERM: stop accepting connections. run() then gives in-flight
    requests a moment and closes the sockets."""
    for srv in SERVERS:
        try:
            srv.shutdown()
        except Exception:
            pass


def run(srv):
    """Serve in the main thread until SIGTERM, then let in-flight requests
    finish before the process exits."""
    srv.serve_forever()
    time.sleep(3)
    for s in SERVERS:
        try:
            s.server_close()
        except Exception:
            pass


def on_sigterm(*_):
    if not SERVERS:  # still starting: nothing in flight to drain
        sys.exit(0)  # SystemExit isn't an Exception, so the wait loops don't catch it
    threading.Thread(target=stop_servers, daemon=True).start()


def main():
    POOL.open()
    signal.signal(signal.SIGTERM, on_sigterm)
    wait_for_db()
    if MODE == "admin":
        try:
            write_limits("admin startup")
        except Exception:
            log("limits not written at startup; the admin portal shows the error")
        if RECONCILER_CLIENT_SECRET and OIDC_BACKCHANNEL:
            threading.Thread(target=reconciler, daemon=True).start()
        else:
            log("reconciler: needs RECONCILER_CLIENT_SECRET and OIDC_BACKCHANNEL; not started")
        threading.Thread(target=serve(AdminMetrics, "0.0.0.0", 9100).serve_forever, daemon=True).start()
        run(serve(Admin, "127.0.0.1", 8080))
    elif MODE in ("keys", "usage"):
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
        if MODE == "usage":
            threading.Thread(target=serve(Health, "0.0.0.0", 8081).serve_forever, daemon=True).start()
            run(serve(Usage, "0.0.0.0", 8443, tls=True))
            return
        for target in (listen_for_revocations, housekeeping):
            threading.Thread(target=target, daemon=True).start()
        # The check port speaks TLS with the service CA certificate (the same
        # one the UI on 8443 uses), so keys never cross the pod network in
        # plain HTTP.
        threading.Thread(target=serve(Validate, "0.0.0.0", 8081, tls=True).serve_forever, daemon=True).start()
        run(serve(Keys, "0.0.0.0", 8443, tls=True))
    else:
        sys.exit(f"unknown MODE {MODE}")


if __name__ == "__main__":
    main()
