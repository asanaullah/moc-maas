#!/bin/sh
# Assisted by Claude Opus
set -eu
k() { oc --kubeconfig "$KUBECONFIG" "$@"; }
until k get --raw /readyz >/dev/null 2>&1; do sleep 2; done
k apply --server-side --force-conflicts -f /crds/
k wait --for=condition=Established crd --all --timeout=180s
seed() {
  k apply --server-side --force-conflicts -f /base/base.yaml
  k create secret generic maas-db-config -n "$NS" --from-file=DB_CONNECTION_URL=/db/DB_CONNECTION_URL \
    --dry-run=client -o yaml | k apply --server-side --force-conflicts -f -
  k apply --server-side --force-conflicts -f /seed/objects.yaml -f /seed/authconfig.yaml
  for kind in maassubscriptions.maas.opendatahub.io maasauthpolicies.maas.opendatahub.io \
              maasmodelrefs.maas.opendatahub.io authconfigs.authorino.kuadrant.io; do
    for name in $(k get "$kind" -n "$NS" -l maas.moc/managed=true -o name); do
      grep -qx "$name" /seed/names.txt || k delete -n "$NS" "$name"
    done
  done
  # The ConfigMap value can lack its final newline (Ansible's file lookup
  # strips it), and read skips a last line without one.
  while read -r res name patch || [ -n "${res:-}" ]; do
    k patch "$res" "$name" -n "$NS" --subresource=status --type=merge -p "$patch"
  done < /seed/status.txt
  touch /tmp/seeded
}
fingerprint() { cat /base/base.yaml /seed/* /db/DB_CONNECTION_URL | sha256sum; }
seed
last=$(fingerprint)
while sleep 30; do
  now=$(fingerprint)
  if [ "$now" != "$last" ]; then seed && last=$now; fi
done
