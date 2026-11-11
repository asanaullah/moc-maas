#!/bin/bash
# Assisted by Claude Opus
# Admin consoles on localhost until Ctrl-C:
#   8090  maas-api         (deploy/maas-gateway; listens on the pod's localhost)
#   16443 maas-kube        (deploy/maas-kube; use the maas-ops token from
#                          Secret maas-kube-token-ops in a kubeconfig)
#   9443  Keycloak admin   (deploy/maas-sso; its login is registered for
#                          localhost:9443 by deploy.yml)
#   3000  Grafana          (deploy/maas-grafana)
#   9090  Prometheus       (deploy/maas-prometheus)
# The namespace is the first argument (default uat-project).
set -euo pipefail
NS="${1:-uat-project}"

# local port, target, pod port, name, address, health URL. Keycloak's admin
# login is registered for localhost:9443, so keep that one. maas-kube answers
# 401 without a token, which is enough to prove the forward works.
forwards=(
  "8090 deploy/maas-gateway 8090 maas-api http://localhost:8090/health http://127.0.0.1:8090/health"
  "16443 deploy/maas-kube 6443 maas-kube https://localhost:16443/version https://127.0.0.1:16443/version"
  "9443 deploy/maas-sso 8443 keycloak https://localhost:9443/admin/ https://127.0.0.1:9443/realms/maas"
  "3000 deploy/maas-grafana 3000 grafana http://localhost:3000/ http://127.0.0.1:3000/api/health"
  "9090 deploy/maas-prometheus 9090 prometheus http://localhost:9090/graph http://127.0.0.1:9090/-/healthy"
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
  # Any HTTP answer (including 401 from maas-kube) means the forward works.
  for _ in $(seq 1 40); do curl -sk -o /dev/null "$health" && break; sleep 0.5; done
  if curl -sk -o /dev/null "$health"; then printf '%-13s %s\n' "$name" "$url"
  else printf '%-13s %s  (not answering yet)\n' "$name" "$url"; fi
done
echo "Ctrl-C to stop."
wait
