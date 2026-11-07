#!/usr/bin/env python3
"""Calls the admin portal's JSON API from inside its own pod. The admin
portal has no Service and listens on the pod's localhost only, so the
playbooks reach it with a pod exec (project members can already exec into
pods; this adds no reach beyond the old port-forward).

Usage: python3 api.py METHOD /api/path [json-body] [base64 actor]

The optional actor (base64 so any username survives the exec command line)
is sent as X-MaaS-Actor and lands in the audit trail.

Prints the response body; on an HTTP error prints the error body and exits
non-zero, so a failed call fails the playbook.
"""
import base64
import sys
import urllib.error
import urllib.request

method, path = sys.argv[1], sys.argv[2]
body = sys.argv[3] if len(sys.argv) > 3 else None
actor = base64.b64decode(sys.argv[4]).decode() if len(sys.argv) > 4 else None
headers = {"X-MaaS-Admin": "1", "Content-Type": "application/json"}
if actor:
    headers["X-MaaS-Actor"] = actor
request = urllib.request.Request(
    "http://127.0.0.1:8080" + path,
    data=body.encode() if body else None,
    method=method,
    headers=headers)
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        print(response.read().decode())
except urllib.error.HTTPError as error:
    print(error.read().decode(), file=sys.stderr)
    sys.exit(1)
except urllib.error.URLError as error:
    print(f"cannot reach the admin portal on the pod's localhost: {error.reason}", file=sys.stderr)
    sys.exit(1)
