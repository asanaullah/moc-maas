#!/bin/bash
# MaaS admin from the command line, through the admin portal's JSON API. Opens
# its own port-forward to deploy/maas-admin for each call.
#
#   ./90-maas-admin.sh plans
#   ./90-maas-admin.sh plan-set <name> <tokens> [window-seconds=60] [max-key-days=90] [models=all, comma-separated]
#   ./90-maas-admin.sh plan-delete <name>
#   ./90-maas-admin.sh groups
#   ./90-maas-admin.sh group-map <keycloak-group> <plan>
#   ./90-maas-admin.sh group-unmap <keycloak-group>
#   ./90-maas-admin.sh limits                      # rewrite Limitador's limits from the plans
#   ./90-maas-admin.sh keys
#   ./90-maas-admin.sh service-key <service> <key-name> <plan> <days> [--secret <name>]
#       prints the key once, or with --secret stores it in that Secret in uat-project
#       (keys: key, id) and prints only the key's id
#   ./90-maas-admin.sh revoke <key-id>
#   ./90-maas-admin.sh audit
#   ./90-maas-admin.sh forward                     # all admin consoles on localhost until Ctrl-C
set -euo pipefail
NS=uat-project

if [ "${1:-}" = "forward" ]; then
  # local port, target, pod port, name, address, health URL. Keycloak's admin
  # login is registered for localhost:9443 (11-sso-init.sh), so keep that one.
  forwards=(
    "8081 deploy/maas-admin 8080 admin-portal http://localhost:8081/ http://127.0.0.1:8081/healthz"
    "9443 deploy/maas-sso 8443 keycloak https://localhost:9443/admin/ https://127.0.0.1:9443/realms/maas"
    "3000 deploy/maas-grafana 3000 grafana http://localhost:3000/ http://127.0.0.1:3000/api/health"
  )
  for f in "${forwards[@]}"; do
    read -r lp _ _ name _ _ <<<"$f"
    python3 -c 'import socket,sys; socket.socket().bind(("127.0.0.1", int(sys.argv[1])))' "$lp" 2>/dev/null ||
      { echo "localhost:$lp ($name) is in use; stop whatever holds it first" >&2; exit 1; }
  done
  # Ctrl-C stops the loops and their port-forwards.
  trap 'trap - EXIT INT TERM; kill 0 2>/dev/null' EXIT INT TERM
  for f in "${forwards[@]}"; do
    read -r lp target rp _ _ _ <<<"$f"
    # Reconnects when the pod is replaced, for example after a rollout.
    ( while :; do oc port-forward -n "$NS" "$target" "$lp:$rp" >/dev/null 2>&1; sleep 1; done ) &
  done
  for f in "${forwards[@]}"; do
    read -r _ _ _ name url health <<<"$f"
    for _ in $(seq 1 40); do curl -skf -o /dev/null "$health" && break; sleep 0.5; done
    if curl -skf -o /dev/null "$health"; then printf '%-13s %s\n' "$name" "$url"
    else printf '%-13s %s  (not answering yet)\n' "$name" "$url"; fi
  done
  echo "Ctrl-C to stop."
  wait
fi

port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
oc port-forward -n "$NS" deploy/maas-admin "$port:8080" >/dev/null 2>&1 &
pf=$!
trap 'kill $pf 2>/dev/null; wait $pf 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do curl -sf -o /dev/null "http://127.0.0.1:$port/healthz" && break; sleep 0.5; done
B="http://127.0.0.1:$port/api"

get() { curl -sf -H 'X-MaaS-Admin: 1' "$B/$1"; }
post() { curl -s -H 'X-MaaS-Admin: 1' -H 'Content-Type: application/json' -d "$2" "$B/$1"; echo; }
table() { python3 -c 'import json,sys; rows=json.load(sys.stdin); [print("  ".join(f"{k}={v}" for k,v in r.items())) for r in rows] or print("(none)")'; }
json() { python3 -c 'import json,sys; print(json.dumps(dict(a.split("=",1) for a in sys.argv[1:])))' "$@"; }

cmd=${1:-}; shift || true
case "$cmd" in
  plans|groups|keys|audit) get "$cmd" | table ;;
  plan-set)
    [ $# -ge 2 ] || { echo "plan-set <name> <tokens> [window] [days] [models]" >&2; exit 2; }
    models=$(python3 -c 'import json,sys; print(json.dumps([m for m in sys.argv[1].split(",") if m]))' "${5:-}")
    post plans "{\"name\":\"$1\",\"tokens\":$2,\"window\":${3:-60},\"days\":${4:-90},\"models\":$models}" ;;
  plan-delete) post plans/delete "$(json name="$1")" ;;
  group-map) post groups "$(json group="$1" plan="$2")" ;;
  group-unmap) post groups/delete "$(json group="$1")" ;;
  limits) post limits '{}' ;;
  service-key)
    [ $# -eq 4 ] || [ $# -eq 6 -a "${5:-}" = "--secret" ] || { echo "service-key <service> <key-name> <plan> <days> [--secret <name>]" >&2; exit 2; }
    body="{\"owner\":\"$1\",\"name\":\"$2\",\"plan\":\"$3\",\"days\":$4}"
    if [ $# -eq 4 ]; then post service-keys "$body"; exit; fi
    # The key goes from the API response into the Secret through pipes only.
    resp=$(curl -s -H 'X-MaaS-Admin: 1' -H 'Content-Type: application/json' -d "$body" "$B/service-keys")
    id=$(printf '%s' "$resp" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("id") or ""); sys.exit(0 if d.get("id") else 1)') ||
      { printf '%s' "$resp" | python3 -c 'import json,sys; print("error:", json.load(sys.stdin).get("error"))' >&2; exit 1; }
    oc create secret generic "$6" -n "$NS" \
      --from-file=key=<(printf '%s' "$resp" | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["key"])') \
      --from-literal=id="$id" --dry-run=client -o yaml |
      oc label --local -f - app.kubernetes.io/part-of=maas-test -o yaml |
      oc apply --server-side --field-manager=maas-admin -f - >/dev/null
    unset resp
    echo "service key $id stored in Secret $6 (keys: key, id)" ;;
  revoke) post keys/revoke "$(json id="$1")" ;;
  *) sed -n '2,18p' "$0"; exit 2 ;;
esac
