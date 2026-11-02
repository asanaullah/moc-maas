#!/bin/bash
# Keycloak (realm "maas") admin from the command line. Runs Keycloak's own CLI
# inside a maas-sso pod, signed in as the bootstrap admin from that pod's
# environment, so no admin password passes through this machine.
#
#   ./91-sso-admin.sh init                    # once after install: admin console login via port-forward
#   ./91-sso-admin.sh groups
#   ./91-sso-admin.sh group-create <group>
#   ./91-sso-admin.sh users
#   ./91-sso-admin.sh user-groups <email>
#   ./91-sso-admin.sh add <email> <group>     # users exist after their first GitHub sign-in
#   ./91-sso-admin.sh remove <email> <group>
#   ./91-sso-admin.sh usage-client <host>  # create or update the user dashboard's client
set -euo pipefail
NS=uat-project
[ $# -ge 1 ] || { sed -n '2,14p' "$0"; exit 2; }

# The remote script logs in, runs one command, and cleans up. Arguments are
# passed after "--", never interpolated into the script.
oc exec -i -n "$NS" deploy/maas-sso -c keycloak -- bash -s -- "$@" <<'REMOTE'
set -euo pipefail
K=/opt/keycloak/bin/kcadm.sh; C="--config /tmp/kcadm-$$.config"; T=/tmp/kcadm-ca-$$.p12
trap 'rm -f /tmp/kcadm-$$.config $T' EXIT
keytool -importcert -noprompt -alias ca -storetype PKCS12 -storepass changeit \
  -file /var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt -keystore $T >/dev/null 2>&1
$K config truststore $C --trustpass changeit $T
$K config credentials $C --server https://maas-sso.uat-project.svc:8443 --realm master \
  --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null 2>&1
R="-r maas"
user_id() { $K get users $C $R -q email="$1" -q exact=true --fields id --format csv --noquotes | head -1; }
group_id() { $K get groups $C $R --fields id,name --format csv --noquotes | awk -F, -v g="$1" '$2==g {print $1}' | head -1; }
need() { [ -n "$1" ] || { echo "$2" >&2; exit 1; }; }
case "$1" in
  init)
    $K update realms/master $C -s attributes.frontendUrl=https://localhost:9443
    echo "master realm frontend URL set to https://localhost:9443" ;;
  groups) $K get groups $C $R --fields name --format csv --noquotes ;;
  group-create) $K create groups $C $R -s name="$2" >/dev/null && echo "created group $2" ;;
  users) $K get users $C $R --fields username,email,enabled --format csv --noquotes ;;
  user-groups)
    u=$(user_id "$2"); need "$u" "no user with email $2 (they must sign in once first)"
    $K get users/$u/groups $C $R --fields name --format csv --noquotes ;;
  add|remove)
    u=$(user_id "$2"); need "$u" "no user with email $2 (they must sign in once first)"
    g=$(group_id "$3"); need "$g" "no group $3"
    if [ "$1" = add ]; then
      $K update users/$u/groups/$g $C $R -s realm=maas -s userId=$u -s groupId=$g -n && echo "added $2 to $3"
    else
      $K delete users/$u/groups/$g $C $R && echo "removed $2 from $3"
    fi ;;
  usage-client)
    # Same settings as the key portal's client; the secret comes from this
    # pod's environment (maas-usage-oidc), so it isn't printed or passed in.
    need "${2:-}" "usage-client needs the dashboard's host"
    [ -n "${USAGE_CLIENT_SECRET:-}" ] || { echo "USAGE_CLIENT_SECRET isn't set in this pod; apply 10-maas-sso.yaml" >&2; exit 1; }
    body() {
      printf '{"clientId":"maas-usage","name":"MaaS user dashboard","protocol":"openid-connect","enabled":true,'
      printf '"publicClient":false,"clientAuthenticatorType":"client-secret","secret":"%s",' "$USAGE_CLIENT_SECRET"
      printf '"standardFlowEnabled":true,"implicitFlowEnabled":false,"directAccessGrantsEnabled":false,'
      printf '"serviceAccountsEnabled":false,"consentRequired":false,"fullScopeAllowed":false,"frontchannelLogout":false,'
      printf '"redirectUris":["https://%s/callback"],"webOrigins":[],' "$2"
      printf '"attributes":{"pkce.code.challenge.method":"S256","post.logout.redirect.uris":"https://%s/"}' "$2"
    }
    cid=$($K get clients $C $R -q clientId=maas-usage --fields id --format csv --noquotes | head -1)
    if [ -z "$cid" ]; then
      { body "$@"; printf ',"protocolMappers":[{"name":"groups","protocol":"openid-connect",'
        printf '"protocolMapper":"oidc-group-membership-mapper","config":{"claim.name":"groups","full.path":"false",'
        printf '"id.token.claim":"true","access.token.claim":"false","userinfo.token.claim":"true"}}]}'; } |
        $K create clients $C $R -f - >/dev/null && echo "created client maas-usage for https://$2"
    else
      { body "$@"; printf '}'; } | $K update clients/$cid $C $R -f - && echo "updated client maas-usage for https://$2"
    fi ;;
  *) echo "unknown command $1" >&2; exit 2 ;;
esac
REMOTE
