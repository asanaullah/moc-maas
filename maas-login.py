#!/usr/bin/env python3
# ruff: noqa: BLE001, S110
# Assisted by Claude Opus
"""Sign in to the MaaS, keep an API key, and use it.

    ./maas-login.py login            # browser sign-in; mints and stores a key
    ./maas-login.py chat "hello"     # sends to the first model in /v1/models
    ./maas-login.py key              # prints the stored key
    ./maas-login.py keys             # lists the user's keys
    ./maas-login.py revoke <id>      # revokes one of the user's keys
    ./maas-login.py metrics          # the user metrics the gateway exposes

The namespace is --namespace (default uat-project). With oc, the issuer and
gateway URL come from the cluster's Routes; users without oc pass --issuer,
--url and (when the CA is private) --ca, or set MAAS_ISSUER, MAAS_URL and
MAAS_CA. The login stores those settings with the key, access token and refresh
token in ~/.maas/credentials.json with mode 0600.

Sign-in uses the public Keycloak client maas-oidc (authorization code with
PKCE), so the password never passes through this script. The browser opens
the realm's login page, with GitHub as the broker.
"""
import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

CLIENT = "maas-oidc"
SCOPES = "openid groups"
CREDENTIALS = os.path.expanduser("~/.maas/credentials.json")
DEFAULT_NS = "uat-project"


def oc(*args):
    return subprocess.run(["oc", *args], check=True, capture_output=True, text=True).stdout


def discover(ns, args):
    issuer = args.issuer or os.environ.get("MAAS_ISSUER")
    url = args.url or os.environ.get("MAAS_URL")
    if not issuer or not url:
        try:
            if not issuer:
                host = oc("get", "route", "maas-sso", "-n", ns, "-o", "jsonpath={.spec.host}")
                issuer = f"https://{host}/realms/maas"
            if not url:
                host = oc("get", "route", "maas-gateway", "-n", ns, "-o", "jsonpath={.spec.host}")
                url = f"https://{host}"
        except (OSError, subprocess.CalledProcessError):
            sys.exit("cannot read the cluster Routes; pass --issuer and --url "
                     "(or set MAAS_ISSUER and MAAS_URL)")
    return issuer.rstrip("/"), url.rstrip("/")


def settings(ns, args, cfg):
    issuer, url = cfg.get("issuer"), cfg.get("url")
    if not issuer or not url:
        found_issuer, found_url = discover(ns, args)
        issuer, url = issuer or found_issuer, url or found_url
    return issuer, url


def ssl_context(ns, args, cfg=None):
    if args.insecure:
        return ssl._create_unverified_context()
    ca = args.ca or os.environ.get("MAAS_CA") or (cfg or {}).get("ca")
    if ca:
        try:
            return ssl.create_default_context(cafile=ca)
        except OSError as e:
            sys.exit(f"cannot read the CA file: {e}")
    try:
        data = json.loads(oc("get", "cm", "maas-ingress-ca", "-n", ns, "-o", "json"))
        pem = next(iter(data["data"].values()))
        return ssl.create_default_context(cadata=pem)
    except Exception:
        return ssl.create_default_context()


def request(url, data=None, headers=None, method=None, ctx=None, form=False):
    body, hdrs = None, dict(headers or {})
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach {url}: {e.reason}; pass --ca or --insecure "
                 "if the cluster certificate is not trusted")


def claims(token):
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def config_path(ns):
    return CREDENTIALS


def load_config(ns):
    try:
        with open(config_path(ns)) as f:
            cfg = json.load(f)
        if "key" in cfg and "api_key" not in cfg:
            cfg["api_key"] = cfg.pop("key")
        return cfg
    except (OSError, ValueError):
        return {}


def save_config(ns, cfg):
    os.makedirs(os.path.dirname(CREDENTIALS), mode=0o700, exist_ok=True)
    fd = os.open(config_path(ns), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)


def oidc(issuer, ctx):
    status, body = request(issuer + "/.well-known/openid-configuration", ctx=ctx)
    if status != 200:
        sys.exit(f"cannot read the realm's OIDC configuration: {status}")
    return json.loads(body)


def fresh_token(ns, cfg, ctx):
    if cfg.get("access_token") and time.time() < cfg.get("expires_at", 0) - 30:
        return cfg["access_token"]
    if not cfg.get("refresh_token"):
        return None
    meta = oidc(cfg["issuer"], ctx)
    status, body = request(meta["token_endpoint"],
                           {"grant_type": "refresh_token", "client_id": CLIENT,
                            "refresh_token": cfg["refresh_token"]}, ctx=ctx, form=True)
    if status != 200:
        return None
    tok = json.loads(body)
    cfg.update(access_token=tok["access_token"],
               refresh_token=tok.get("refresh_token", cfg["refresh_token"]),
               expires_at=time.time() + int(tok.get("expires_in", 300)))
    save_config(ns, cfg)
    return cfg["access_token"]


def login(ns, args):
    issuer, url = discover(ns, args)
    ctx = ssl_context(ns, args)
    meta = oidc(issuer, ctx)
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    result, done = {}, threading.Event()

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<p>Signed in. You can close this window.</p>")
            done.set()

        def log_message(self, *_):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Callback)
    port = server.server_address[1]
    redirect = f"http://127.0.0.1:{port}/callback"
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()
    link = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode({
        "client_id": CLIENT, "response_type": "code", "scope": SCOPES,
        "redirect_uri": redirect, "state": state, "nonce": secrets.token_urlsafe(24),
        "code_challenge": challenge, "code_challenge_method": "S256",
        "kc_idp_hint": "github"})
    print("Open this URL to sign in:\n  " + link, flush=True)
    try:
        webbrowser.open(link)
    except Exception:
        pass
    done.wait(300)
    server.shutdown()
    server.server_close()
    if result.get("error"):
        sys.exit("sign-in failed: " + result["error"])
    if result.get("state") != state or not result.get("code"):
        sys.exit("sign-in did not complete")
    status, body = request(meta["token_endpoint"], {
        "grant_type": "authorization_code", "client_id": CLIENT,
        "code": result["code"], "redirect_uri": redirect,
        "code_verifier": verifier}, ctx=ctx, form=True)
    if status != 200:
        sys.exit(f"token exchange failed: {status} {body[:200].decode(errors='replace')}")
    tok = json.loads(body)
    access = tok["access_token"]
    claim = claims(access)
    user = claim.get("preferred_username") or claim.get("sub", "")
    email = claim.get("email", "")
    status, body = request(url + "/maas-api/v1/api-keys",
                           {"name": "maas-login", "expiresIn": "720h"},
                           headers={"Authorization": "Bearer " + access}, ctx=ctx)
    if status not in (200, 201):
        text = body[:300].decode(errors="replace")
        if status in (400, 403) and "subscription" in text.lower():
            who = email or user
            sys.exit(f"Signed in as {who}, but no subscription is assigned yet. "
                     "Ask the admin to add you to the subscription's group, then run login again.")
        sys.exit(f"key creation failed: {status} {text}")
    created = json.loads(body)
    cfg = {"issuer": issuer, "url": url, "namespace": ns, "username": user,
           "api_key": created.get("key", ""), "key_id": created.get("id", ""),
           "subscription": created.get("subscription", ""),
           "access_token": access, "refresh_token": tok.get("refresh_token", ""),
           "expires_at": time.time() + int(tok.get("expires_in", 300)),
           "ca": args.ca or os.environ.get("MAAS_CA", "")}
    save_config(ns, cfg)
    print(f"Signed in as {user}. Key stored in {config_path(ns)}.")
    print("Key: " + cfg["api_key"])


def chat(ns, text, args):
    cfg = load_config(ns)
    if not cfg.get("api_key"):
        sys.exit("run ./maas-login.py login first")
    ctx = ssl_context(ns, args, cfg)
    _, url = settings(ns, args, cfg)
    key = cfg["api_key"]
    model = args.model
    if not model:
        status, body = request(url + "/v1/models",
                               headers={"Authorization": "Bearer " + key}, ctx=ctx)
        if status == 200:
            data = json.loads(body).get("data") or []
            if data:
                model = data[0].get("id")
    model = model or f"publishers/{cfg.get('namespace', ns)}/models/maas-sim"
    status, body = request(url + "/v1/chat/completions",
                           {"model": model, "max_tokens": args.max_tokens,
                            "messages": [{"role": "user", "content": text}]},
                           headers={"Authorization": "Bearer " + key}, ctx=ctx)
    if status != 200:
        sys.exit(f"chat failed: {status} {body[:300].decode(errors='replace')}")
    data = json.loads(body)
    print(data["choices"][0]["message"]["content"].strip())
    usage = data.get("usage") or {}
    if usage.get("total_tokens"):
        print(f"[{usage['total_tokens']} tokens]")


def keys(ns, args):
    cfg = load_config(ns)
    if not cfg:
        sys.exit("run ./maas-login.py login first")
    issuer, url = settings(ns, args, cfg)
    cfg.update(issuer=issuer, url=url)
    ctx = ssl_context(ns, args, cfg)
    tok = fresh_token(ns, cfg, ctx)
    if not tok:
        sys.exit("sign in again: ./maas-login.py login")
    status, body = request(url + "/maas-api/v1/api-keys/search",
                           {"filters": {}}, headers={"Authorization": "Bearer " + tok}, ctx=ctx)
    if status != 200:
        sys.exit(f"key list failed: {status} {body[:200].decode(errors='replace')}")
    for k in json.loads(body).get("data") or []:
        print("\t".join(str(k.get(f, "")) for f in
                        ("id", "name", "subscription", "status", "expirationDate")))


def revoke(ns, key_id, args):
    cfg = load_config(ns)
    if not cfg:
        sys.exit("run ./maas-login.py login first")
    issuer, url = settings(ns, args, cfg)
    cfg.update(issuer=issuer, url=url)
    ctx = ssl_context(ns, args, cfg)
    tok = fresh_token(ns, cfg, ctx) or cfg.get("api_key", "")
    if not tok:
        sys.exit("sign in again: ./maas-login.py login")
    status, body = request(url + "/maas-api/v1/api-keys/" + urllib.parse.quote(key_id),
                           headers={"Authorization": "Bearer " + tok}, method="DELETE", ctx=ctx)
    if status != 200:
        sys.exit(f"revoke failed: {status} {body[:200].decode(errors='replace')}")
    if key_id == cfg.get("key_id"):
        cfg.update(api_key="", key_id="")
        save_config(ns, cfg)
    print("revoked", key_id)


def metrics(ns, args):
    cfg = load_config(ns)
    if not cfg.get("api_key"):
        sys.exit("run ./maas-login.py login first")
    ctx = ssl_context(ns, args, cfg)
    _, url = settings(ns, args, cfg)
    status, body = request(url + "/maas-usage/metrics",
                           headers={"Authorization": "Bearer " + cfg["api_key"]}, ctx=ctx)
    if status != 200:
        sys.exit(f"metrics failed: {status} {body[:200].decode(errors='replace')}")
    sys.stdout.write(body.decode())


def main():
    p = argparse.ArgumentParser(description="Sign in to the MaaS and use an API key.")
    p.add_argument("--namespace", default=DEFAULT_NS)
    p.add_argument("--issuer", help="override the Keycloak issuer URL")
    p.add_argument("--url", help="override the gateway URL")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification")
    p.add_argument("--ca", help="PEM file for the cluster's ingress CA")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("login")
    c = sub.add_parser("chat")
    c.add_argument("text")
    c.add_argument("--model")
    c.add_argument("--max-tokens", type=int, default=256)
    sub.add_parser("key")
    sub.add_parser("keys")
    r = sub.add_parser("revoke")
    r.add_argument("id")
    sub.add_parser("metrics")
    args = p.parse_args()
    if args.command == "login":
        login(args.namespace, args)
    elif args.command == "chat":
        chat(args.namespace, args.text, args)
    elif args.command == "key":
        cfg = load_config(args.namespace)
        if not cfg.get("api_key"):
            sys.exit("run ./maas-login.py login first")
        print(cfg["api_key"])
    elif args.command == "keys":
        keys(args.namespace, args)
    elif args.command == "revoke":
        revoke(args.namespace, args.id, args)
    elif args.command == "metrics":
        metrics(args.namespace, args)


if __name__ == "__main__":
    main()
