#!/bin/bash
# Runs both database backups now, from their CronJobs, and waits for them.
# Dumps land on uat-project-storage under maas/backups and maas/sso-backups.
set -euo pipefail
NS=uat-project
stamp=$(date -u +%Y%m%d%H%M%S)
for cj in maas-db-backup maas-sso-db-backup; do
  job="$cj-$stamp"
  oc create job -n "$NS" "$job" --from="cronjob/$cj" >/dev/null
  oc wait -n "$NS" "job/$job" --for=condition=Complete --timeout=10m >/dev/null
  echo "$cj: $(oc logs -n "$NS" "job/$job" | tail -1)"
  oc delete job -n "$NS" "$job" >/dev/null
done
