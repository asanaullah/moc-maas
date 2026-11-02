#!/bin/bash
# Runs setup steps 1-18 and 20-28 in order, waiting for each. Step 0 (the chart)
# is an admin's job, and step 19 (first plan) takes your choices, so both stay
# separate.
# Safe to re-run: applies are idempotent and secrets are only created when
# missing. Prerequisites (see GUIDE.md): the uat-harness chart applied
# (step 0, 00-apply-chart.sh), an oc login with rights in uat-project, openssl, and a
# GitHub OAuth App.
#
#   ./99-setup-all.sh             everything inside the namespace, no public addresses
#   ./99-setup-all.sh --routes    also open the Routes (key portal, Keycloak, gateway, user dashboard)
#
# For the GitHub secret, have MOC_MAAS_CLIENTID and MOC_MAAS_SECRET set on the
# first run (or create maas-sso-github with 02-create-github-secret.sh first).
set -euo pipefail
cd "$(dirname "$0")"
NS=uat-project
routes=false
[ "${1:-}" = "--routes" ] && routes=true

step() { echo; echo "== $*"; }
wait_deploy() { for d in "$@"; do oc rollout status -n "$NS" "deploy/$d" --timeout=10m; done; }

step "secrets"
./01-create-secrets.sh
if oc get secret maas-sso-github -n "$NS" >/dev/null 2>&1; then
  echo "maas-sso-github exists, left as it is"
else
  ./02-create-github-secret.sh
fi

step "model path: emulator, Redis, Limitador, endpoint picker, client"
oc apply -f 03-emulator.yaml -f 04-redis.yaml -f 05-limitador.yaml -f 06-epp.yaml -f 07-client.yaml
wait_deploy maas-sim maas-redis maas-limitador maas-epp
oc wait -n "$NS" pod/maas-client --for=condition=Ready --timeout=5m

step "databases"
oc apply -f 08-maas-db.yaml -f 09-maas-sso-db.yaml
wait_deploy maas-db maas-sso-db

step "Keycloak"
oc apply -f 10-maas-sso.yaml
wait_deploy maas-sso
./11-sso-init.sh

step "portals (the admin portal writes Limitador's limits when it starts)"
./12-gen-portal-code.sh
oc apply -f 13-maas-portal-code.yaml -f 14-maas-portals.yaml
# Pick up new code or config even when the Deployments themselves didn't change.
oc rollout restart -n "$NS" deploy/maas-admin deploy/maas-keys
wait_deploy maas-admin maas-keys

step "gateway (Envoy reads its config only at startup)"
oc apply -f 15-gateway.yaml
oc rollout restart -n "$NS" deploy/maas-gateway
wait_deploy maas-gateway

step "metrics: Prometheus, Thanos"
oc apply -f 20-metrics-store.yaml -f 21-prometheus.yaml -f 22-thanos.yaml
wait_deploy maas-prometheus maas-thanos-store maas-thanos-query maas-thanos-compact

step "admin dashboard (Grafana)"
./23-gen-dashboards.py
oc apply -f 24-grafana-dashboards.yaml -f 25-grafana.yaml
oc rollout restart -n "$NS" deploy/maas-grafana
wait_deploy maas-grafana

step "user dashboard"
./26-usage-client.sh
oc apply -f 27-maas-usage.yaml
oc rollout restart -n "$NS" deploy/maas-usage
wait_deploy maas-usage

if $routes; then
  step "public addresses"
  oc apply -f 16-maas-keys-route.yaml -f 17-maas-sso-route.yaml -f 18-maas-gateway-route.yaml -f 28-maas-usage-route.yaml
  oc get routes -n "$NS" -o custom-columns=ROUTE:.metadata.name,HOST:.spec.host
fi

step "done"
echo "Next: ./19-first-plan.sh   (a Keycloak group and a plan for it), then add users:"
echo "  ./91-sso-admin.sh add <email> maas-standard   (after they sign in once)"
