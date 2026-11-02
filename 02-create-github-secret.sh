#!/bin/bash
# Step 2: creates or updates the maas-sso-github Secret in uat-project from
# MOC_MAAS_CLIENTID and MOC_MAAS_SECRET in your environment. Keycloak reads it
# for the GitHub login. Re-run it to rotate the secret.
#
# The values are never printed, and the client secret reaches oc through a
# pipe, not its command line, so other users on this machine can't see it in
# the process list. Server-side apply keeps oc from copying the values into a
# last-applied-configuration annotation on the Secret.
set -euo pipefail

: "${MOC_MAAS_CLIENTID:?set MOC_MAAS_CLIENTID to the GitHub OAuth app client ID}"
: "${MOC_MAAS_SECRET:?set MOC_MAAS_SECRET to the GitHub OAuth app client secret}"

oc create secret generic maas-sso-github -n uat-project \
  --from-literal=client-id="$MOC_MAAS_CLIENTID" \
  --from-file=client-secret=<(printf '%s' "$MOC_MAAS_SECRET") \
  --dry-run=client -o yaml |
  oc label --local -f - app.kubernetes.io/part-of=maas-test -o yaml |
  oc apply --server-side --field-manager=maas-admin -f -

echo "maas-sso-github is in uat-project with keys: client-id, client-secret"
