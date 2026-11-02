#!/bin/bash
# Step 19: a first plan and the Keycloak group that grants it. Users added to
# the group (./91-sso-admin.sh add <email> <group>) can then create keys under
# the plan. Re-running updates the plan and keeps the group.
#
#   ./19-first-plan.sh [group=maas-standard] [plan=standard] [tokens=20000] [window-seconds=60] [max-key-days=90] [models=maas-sim]
set -euo pipefail
cd "$(dirname "$0")"
group=${1:-maas-standard} plan=${2:-standard} tokens=${3:-20000} window=${4:-60} days=${5:-90} models=${6:-maas-sim}

if ./91-sso-admin.sh groups | grep -qx "$group"; then
  echo "Keycloak group $group exists"
else
  ./91-sso-admin.sh group-create "$group"
fi
./90-maas-admin.sh plan-set "$plan" "$tokens" "$window" "$days" "$models"
./90-maas-admin.sh group-map "$group" "$plan"
echo "Members of $group get plan $plan: $tokens tokens per ${window}s, models $models, keys up to $days days."
