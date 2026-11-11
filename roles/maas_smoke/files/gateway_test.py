#!/usr/bin/env python3
"""Checks the MaaS gateway's routes and guards through its public Route.

Run by playbooks/test-gateway.yml with a valid key in MOC_MAAS_KEY. One line
per check, nonzero exit if any failed.

    gateway_test.py <base URL> <publisher model id> <namespace>
"""
import json
import os
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1].rstrip("/")
MODEL = sys.argv[2]
NS = sys.argv[3]
KEY = os.environ["MOC_MAAS_KEY"]
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


def chat(headers=None, **extra):
    body = {"model": MODEL, "max_tokens": 16,
            "messages": [{"role": "user", "content": "gateway test"}], **extra}
    return request("/v1/chat/completions", body,
                   headers={"Authorization": "Bearer " + KEY, **(headers or {})})


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(("ok  " if ok else "FAIL") + f"  {name}" + (f": {detail}" if detail and not ok else ""))


def big_body():
    """Sends a 5 MiB request and returns the status."""
    u = urllib.parse.urlparse(BASE)
    size = 5 * 1024 * 1024
    with socket.create_connection((u.hostname, u.port or 443), timeout=15) as s:
        with CTX.wrap_socket(s, server_hostname=u.hostname) as tls:
            head = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: {u.hostname}\r\n"
                    f"Authorization: Bearer {KEY}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {size}\r\n\r\n").encode()
            tls.sendall(head + b"x" * size)
            first = tls.recv(4096)
            return int(first.split(b" ", 2)[1])


def main():
    auth = {"Authorization": "Bearer " + KEY}
    status, _, _ = request("/v1/models", headers=auth)
    check("/v1/models reaches maas-api", status == 200, f"got {status}")
    status, _, _ = request("/v1/subscriptions", headers=auth)
    check("/v1/subscriptions reaches maas-api", status == 200, f"got {status}")
    status, _, _ = request("/v1/api-keys/search", {"filters": {}}, method="POST", headers=auth)
    check("/v1/api-keys/search reaches maas-api", status == 200, f"got {status}")
    status, _, _ = request("/maas-api/v1/subscriptions", headers=auth)
    check("/maas-api/v1 is rewritten to /v1", status == 200, f"got {status}")
    status, _, _ = request("/maas-api/health")
    check("/maas-api/health answers without credentials", status == 200, f"got {status}")
    status, _, _ = request("/v1/responses", {"model": MODEL, "input": "hi"}, method="POST", headers=auth)
    check("/v1/responses is not routed", status == 404, f"got {status}")
    status, _, _ = request("/v1/messages", {"model": MODEL, "messages": []}, method="POST", headers=auth)
    check("/v1/messages is not routed", status == 404, f"got {status}")
    status, _, _ = request("/not-an-api", headers=auth)
    check("an unknown path is 404", status == 404, f"got {status}")

    try:
        status = big_body()
    except Exception as e:
        status = repr(e)
    check("a 5 MiB body is refused with 413", status == 413, f"got {status}")

    status, headers, _ = chat(headers={"x-gateway-destination-endpoint": "203.0.113.9:8000"})
    upstream = headers.get("x-maas-upstream", "") if headers else ""
    check("a client endpoint header doesn't steer the picker", status == 200 and "203.0.113.9" not in upstream,
          f"got {status} upstream={upstream!r}")
    status, _, _ = chat(headers={"x-gateway-model-name": "publishers/" + NS + "/models/other"})
    check("a client model header is replaced from the body", status == 200, f"got {status}")

    logs = subprocess.run(["oc", "-n", NS, "logs", "deploy/maas-sim", "--tail=2000"],
                          capture_output=True, text=True, check=False).stdout
    check("the key never reaches the model", KEY not in logs,
          "the key appears in the model's log")

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
