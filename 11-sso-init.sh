#!/bin/bash
# Step 11: one-time Keycloak setup after its first start (step 10). Points the
# master realm (the admin login) at the port-forward address, so the admin
# console works through
#   oc port-forward -n uat-project deploy/maas-sso 9443:8443
# while the public address keeps the master realm blocked. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"
./91-sso-admin.sh init
