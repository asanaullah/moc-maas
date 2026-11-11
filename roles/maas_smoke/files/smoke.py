#!/usr/bin/env python3
"""Smoke-tests a deployed MaaS through its public gateway Route.

Run by the maas_smoke role, which creates the Keycloak user and the
subscription first and removes them afterwards. The password arrives in the
environment and is never printed here. One line per check, nonzero exit if any
failed.

    smoke.py <gateway base URL> <issuer URL> <publisher model id>
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1].rstrip("/")
ISSUER = sys.argv[2].rstrip("/")
MODEL = sys.argv[3]
USER = os.environ["SMOKE_USER"]
PASSWORD = os.environ["SMOKE_PASSWORD"]
CLIENT = os.environ.get("SMOKE_CLIENT", "maas-oidc")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "60"))
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
RESULTS = []


def request(path, body=None, headers=None, method=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()
    except Exception as e:
        return 0, {}, repr(e).encode()


def bearer(value):
    return {"Authorization": "Bearer " + value}


def token():
    body = urllib.parse.urlencode({"grant_type": "password", "client_id": CLIENT, "username": USER,
                                   "password": PASSWORD, "scope": "openid"}).encode()
    req = urllib.request.Request(ISSUER + "/protocol/openid-connect/token", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, context=CTX, timeout=15) as r:
        return json.loads(r.read())["access_token"]


def chat(key, **extra):
    body = {"model": MODEL, "max_tokens": 32,
            "messages": [{"role": "user", "content": "smoke test"}], **extra}
    return request("/v1/chat/completions", body, headers=bearer(key))


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(("ok  " if ok else "FAIL") + f"  {name}" + (f": {detail}" if detail and not ok else ""))


def main():
    try:
        tok = token()
    except Exception as e:
        print("FAIL  sign in:", repr(e))
        return 1
    check("a token from " + CLIENT, bool(tok))

    status, _, body = request("/maas-api/v1/api-keys", {"name": "smoke", "expiresIn": "1h"}, headers=bearer(tok))
    try:
        created = json.loads(body)
    except ValueError:
        created = {}
    key, kid = created.get("key", ""), created.get("id", "")
    check("a key is minted", status == 201 and key.startswith("sk-oai-") and bool(kid),
          f"got {status} {body[:120]!r}")

    status, _, body = request("/v1/models", headers=bearer(key))
    check("the model list carries the publisher id", status == 200 and MODEL.encode() in body,
          f"got {status} {body[:160]!r}")

    status, _, body = chat(key)
    try:
        usage = json.loads(body).get("usage") or {}
    except ValueError:
        usage = {}
    check("a plain chat is served and counted", status == 200 and usage.get("total_tokens", 0) > 0,
          f"got {status} {body[:120]!r}")

    status, _, body = chat(key, stream=True, timeout=120)
    streamed = [line[6:] for line in body.decode(errors="replace").splitlines() if line.startswith("data: ")]
    with_usage = any('"usage"' in chunk and "total_tokens" in chunk for chunk in streamed if chunk != "[DONE]")
    check("a stream without stream_options reports its usage", status == 200 and with_usage,
          f"got {status}")

    refusal, attempts = None, 0
    for _ in range(100):
        attempts += 1
        status, _, body = chat(key)
        if status == 429:
            refusal = body
            break
    detail = f"after {attempts} requests: {refusal[:120]!r}" if refusal is not None else f"no 429 after {attempts} requests"
    check("the budget is enforced with 429 Too Many Requests",
          refusal is not None and b"Too Many Requests" in refusal, detail)

    status, _, _ = request(f"/v1/api-keys/{kid}", method="DELETE", headers=bearer(key))
    check("the key can be revoked", status == 200, f"got {status}")

    time.sleep(CACHE_TTL + 5)
    status, _, _ = request("/v1/models", headers=bearer(key))
    check("a revoked key is refused after the cache TTL", status in (401, 403), f"got {status}")

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
