#!/bin/bash
# Sends one chat completion through the MaaS gateway's public Route with the
# key in MOC_MAAS_KEY, and prints the response and the HTTP status.
#   ./92-test-gateway.sh [model] [prompt]
# 200 = served, 401 = bad, expired or revoked key, 403 = model not in your plan,
# 429 = token budget used up for this window.
set -euo pipefail
: "${MOC_MAAS_KEY:?set MOC_MAAS_KEY to your MaaS API key}"

URL=https://maas-gateway-uat-project.apps.oac-dev-workload0.hcp.oac.int.massopen.cloud/v1/chat/completions
MODEL=${1:-maas-sim}
PROMPT=${2:-hi}

# The key goes to curl through a header file, not its command line, so it
# doesn't show up in the process list.
curl -s -w '\nHTTP %{http_code}\n' "$URL" \
  -H @<(printf 'Authorization: Bearer %s\n' "$MOC_MAAS_KEY") \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"$PROMPT\"}]}"
