#!/bin/bash
# Step 12: regenerates 13-maas-portal-code.yaml from 12-portal.py; run after
# editing the portal code.
set -euo pipefail
cd "$(dirname "$0")"
{
  printf '%s\n' "# Step 13: the portals' code, as a ConfigMap generated from 12-portal.py. After" \
    "# editing 12-portal.py, regenerate this file:" \
    "#   ./12-gen-portal-code.sh" \
    "# 12-portal.py stays the source; don't edit the code below by hand."
  oc create configmap maas-portal-code -n uat-project --from-file=portal.py=12-portal.py --dry-run=client -o yaml |
    oc label --local -f - app.kubernetes.io/part-of=maas-test -o yaml |
    grep -v '^  creationTimestamp: null$'
} > 13-maas-portal-code.yaml
echo "wrote 13-maas-portal-code.yaml"
