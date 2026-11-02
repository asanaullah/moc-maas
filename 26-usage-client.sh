#!/bin/bash
# Step 26: the user dashboard's OIDC client in Keycloak (realm maas), named
# maas-usage, redirecting to the dashboard's Route (step 28). Its secret is
# maas-usage-oidc, which Keycloak reads from its own environment. Safe to
# re-run: it updates the client if it exists.
set -euo pipefail
cd "$(dirname "$0")"
./91-sso-admin.sh usage-client maas-usage-uat-project.apps.oac-dev-workload0.hcp.oac.int.massopen.cloud
