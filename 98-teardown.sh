#!/bin/bash
# Removes the MaaS from uat-project: workloads, Services, Routes, ConfigMaps,
# CronJobs, the InferencePool, the client pod and the MaaS's Secrets. The
# service CA's maas-*-tls Secrets go with their Services. Database files,
# backups and metrics on uat-project-storage (maas/) are kept; delete them
# separately.
#
#   ./98-teardown.sh --yes
set -euo pipefail
NS=uat-project
[ "${1:-}" = "--yes" ] || { echo "This removes the MaaS from $NS. Re-run with --yes to proceed." >&2; exit 2; }
oc delete route,deploy,svc,configmap,cronjob,inferencepool -n "$NS" -l app.kubernetes.io/part-of=maas-test
oc delete pod maas-client -n "$NS" --ignore-not-found
oc delete secret -n "$NS" --ignore-not-found maas-db maas-sso-db maas-sso-admin maas-portal-oidc maas-sso-github maas-tui-key maas-usage-oidc
