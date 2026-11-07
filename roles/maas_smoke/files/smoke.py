#!/usr/bin/env python3
"""Smoke-tests a deployed MaaS through its public gateway Route.

Run by playbooks/smoke-test.yml, which mints a short-lived service key on a
dedicated tiny-budget plan first; the key arrives in MOC_MAAS_KEY and is
never printed here. One line per check, nonzero exit if any failed.

    smoke.py <gateway base URL>
"""
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1].rstrip("/")
KEY = os.environ["MOC_MAAS_KEY"]
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
# Headers no client should ever see on a response from the public Route.
FORBIDDEN = ["x-maas-user", "x-maas-plan", "x-maas-models", "x-maas-user-type",
             "x-maas-key-id", "x-maas-window", "x-maas-upstream",
             "x-gateway-destination-endpoint", "x-maas-refusal"]
RESULTS = []


def request(path, body=None, headers=None, method=None, timeout=60):
    """Returns (status, headers, body bytes); HTTP errors are normal answers,
    and so is no answer at all: a check that never got a response fails
    instead of crashing the run."""
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


def chat(model="maas-sim", max_tokens=48, headers=None, **extra):
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": "smoke test"}], **extra}
    return request("/v1/chat/completions", body, headers={"Authorization": "Bearer " + KEY, **(headers or {})})


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(("ok  " if ok else "FAIL") + f"  {name}" + (f": {detail}" if detail and not ok else ""))


# 0. Only the API's paths and methods are passed on; everything else is 404.
# (With the key attached: the header key check runs on every path, so a
# keyless request is answered 401 whatever its path.) First, while the tiny
# budget is untouched.
auth = {"Authorization": "Bearer " + KEY}
status, _, _ = request("/v1/chat/completions", method="GET", headers=auth)
check("404 for a wrong method", status == 404, f"got {status}")
status, _, _ = request("/v1/definitely-not-an-api-path", {"model": "maas-sim"}, method="POST", headers=auth)
check("404 for a path outside the API", status == 404, f"got {status}")

# 1. A normal request is served, and the response carries none of the
#    internal headers (the picker's, the key check's, or the upstream address,
#    which only the in-cluster listener adds for maas-tui.py).
status, headers, body = chat()
try:
    answered = json.loads(body)
except ValueError:
    answered = {}
leaked = sorted(h for h in FORBIDDEN if headers.get(h) is not None)
check("200 for a valid key", status == 200, f"got {status} {body[:120]!r}")
check("no internal headers on the response", status == 200 and not leaked, f"leaked {leaked}")
check("the served model is the one asked for", status == 200 and answered.get("model") == "maas-sim",
      f"got {answered.get('model')!r}")

# 2. A key that isn't one is refused.
status, _, body = request("/v1/chat/completions", {"model": "maas-sim", "max_tokens": 8, "messages": []},
                          headers={"Authorization": "Bearer maas-not-a-key"})
check("401 for a bad key", status == 401, f"got {status}")

# 3. A model outside the plan is refused before the model server sees it.
status, _, body = chat(model="some-other-model")
check("403 for a model outside the plan", status == 403, f"got {status} {body[:120]!r}")

# 4. A stream that wouldn't be charged is refused with a clear message.
status, _, body = chat(stream=True)
check("400 for streaming without include_usage", status == 400 and b"include_usage" in body,
      f"got {status} {body[:120]!r}")

# 4b. Field names are read strictly, so another parser can't see a stream the
# key check didn't.
status, _, body = request("/v1/chat/completions",
                          {"model": "maas-sim", "STREAM": True, "messages": [{"role": "user", "content": "hi"}]},
                          headers={"Authorization": "Bearer " + KEY})
check("400 for an upper-case STREAM field", status == 400, f"got {status} {body[:120]!r}")

# 5. A model error keeps its body; the token report doesn't break the answer.
# The emulator doesn't cap max_tokens, but it does refuse a prompt longer
# than its context, with a 400 that explains why.
status, _, body = chat(max_tokens=8, messages=[{"role": "user", "content": "word " * 40000}])
check("the model's own error passes through with its body",
      status == 400 and b"error" in body, f"got {status} {body[:120]!r}")

# 6. A charged stream is served.
status, _, body = chat(stream=True, stream_options={"include_usage": True}, timeout=120)
check("200 for streaming with include_usage", status == 200 and b"usage" in body,
      f"got {status}")

# 7. The client can't rewrite the model the server receives.
status, _, body = chat(headers={"x-llm-d-model-name-rewrite": "some-other-model"})
try:
    answered = json.loads(body)
except ValueError:
    answered = {}
check("a model-rewrite header is ignored", status == 200 and answered.get("model") == "maas-sim",
      f"got {status} {answered.get('model')!r}")

# 9. A keyless request is answered at its headers: declare a large body, send
# only a first slice, and the refusal still comes back. A gateway that read
# the body first would hold the connection open instead.
def keyless_large():
    u = urllib.parse.urlparse(BASE)
    with socket.create_connection((u.hostname, u.port or 443), timeout=15) as s:
        with CTX.wrap_socket(s, server_hostname=u.hostname) as tls:
            start = time.monotonic()
            head = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: {u.hostname}\r\n"
                    f"Content-Type: application/json\r\nContent-Length: {16 * 1024 * 1024}\r\n"
                    f"\r\n").encode()
            tls.sendall(head + b"x" * 65536)
            first = tls.recv(4096)
            return int(first.split(b" ", 2)[1]), time.monotonic() - start

try:
    status, elapsed = keyless_large()
    why = ""
except Exception as e:
    status, elapsed, why = 0, float("inf"), repr(e)
check("a large keyless request is refused before its body is read",
      status == 401 and elapsed < 10,
      f"got {status} after {elapsed:.1f}s {why}".strip())

# 10. Once the budget is spent, the refusal says what it is and when to retry.
# The emulator answers with a random number of completion tokens, so keep
# asking until the tiny budget is spent rather than counting on a few large
# charges to do it.
status, headers, body = 0, None, b""
for _ in range(20):
    status, headers, body = chat(max_tokens=96)
    if status == 429:
        break
refusal = (headers.get("x-maas-refusal") or "") if headers else ""
check("429 with x-maas-refusal: budget and Retry-After once the budget is spent",
      status == 429 and refusal == "budget" and headers.get("Retry-After") is not None,
      f"got {status} refusal={refusal!r} body={body[:120]!r}")
try:
    refusal_type = json.loads(body)["error"]["type"]
except (ValueError, KeyError, TypeError):
    refusal_type = None
check("the budget refusal is a JSON error an OpenAI client can show",
      status == 429 and refusal_type == "budget_exceeded", f"got {body[:120]!r}")

print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
sys.exit(0 if all(RESULTS) else 1)
