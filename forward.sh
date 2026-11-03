#!/bin/bash
# Assisted by Claude Opus
# All admin consoles on localhost until Ctrl-C:
#   8081  admin portal    (deploy/maas-admin)
#   9443  Keycloak admin  (deploy/maas-sso; its login is registered for
#                          localhost:9443 — deploy.yml step 11)
#   3000  Grafana         (deploy/maas-grafana)
set -euo pipefail
NS=uat-project

# local port, target, pod port, name, address, health URL. Keycloak's admin
# login is registered for localhost:9443, so keep that one.
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
