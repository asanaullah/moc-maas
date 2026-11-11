# Keycloak (realm "maas") admin, inside a maas-sso pod: runs Keycloak's own
# CLI signed in as the maas-kcadm client (or, until sync has created that
# client, the bootstrap admin) from that pod's environment, so no admin
# secret passes through the machine running the playbooks. The
# playbooks copy this file into the pod and run it with one argument:
#   playbooks/sso-admin.yml -e maas_action=init          once after install: admin console login via port-forward
#   playbooks/sso-admin.yml -e maas_action=groups
#   playbooks/sso-admin.yml -e maas_action=group-create -e maas_group=<group>
#   playbooks/sso-admin.yml -e maas_action=group-delete -e maas_group=<group>
#   playbooks/sso-admin.yml -e maas_action=group-members -e maas_group=<group>
#   playbooks/sso-admin.yml -e maas_action=user-create -e maas_user=<user> -e maas_group=<group> -e maas_password=<pw>
#   playbooks/sso-admin.yml -e maas_action=user-delete -e maas_user=<user>
#   playbooks/sso-admin.yml -e maas_action=users
#   playbooks/sso-admin.yml -e maas_action=user-groups -e maas_email=<email>
#   playbooks/sso-admin.yml -e maas_action=add -e maas_email=<email> -e maas_group=<group>
#   playbooks/sso-admin.yml -e maas_action=remove -e maas_email=<email> -e maas_group=<group>
#   playbooks/deploy.yml runs usage-client itself, with the dashboard's host,
#   and oidc-client without arguments.
set -euo pipefail
K=/opt/keycloak/bin/kcadm.sh; C="--config /tmp/kcadm-$$.config"; T=/tmp/kcadm-ca-$$.p12
# This pod's own namespace, so the wrapper works in any project.
NS=$(</var/run/secrets/kubernetes.io/serviceaccount/namespace)
trap 'rm -f /tmp/kcadm-$$.config $T' EXIT
keytool -importcert -noprompt -alias ca -storetype PKCS12 -storepass changeit \
  -file /var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt -keystore $T >/dev/null 2>&1
$K config truststore $C --trustpass changeit $T
# kcadm reads KC_CLI_CLIENT_SECRET and KC_CLI_PASSWORD from the environment
# when the secret and password options are left out, so neither is on a command line.
S="https://maas-sso.$NS.svc:8443"
if [ -n "${KCADM_CLIENT_SECRET:-}" ] && KC_CLI_CLIENT_SECRET="$KCADM_CLIENT_SECRET" \
     $K config credentials $C --server "$S" --realm master --client maas-kcadm >/dev/null 2>&1; then
  LOGIN=client
else
  KC_CLI_PASSWORD="$KC_BOOTSTRAP_ADMIN_PASSWORD" \
    $K config credentials $C --server "$S" --realm master --user "$KC_BOOTSTRAP_ADMIN_USERNAME" >/dev/null 2>&1
  LOGIN=bootstrap
fi
R="-r maas"
user_id() { $K get users $C $R -q email="$1" -q exact=true --fields id --format csv --noquotes | head -1; }
username_id() {
  local id name
  while IFS=, read -r id name; do
    if [ "$name" = "$1" ]; then printf '%s\n' "$id"; return; fi
  done < <($K get users $C $R --fields id,username --format csv --noquotes)
}
# Pure bash, no awk: the Keycloak image ships without it.
group_id() {
  local id name
  while IFS=, read -r id name; do
    if [ "$name" = "$1" ]; then printf '%s\n' "$id"; return; fi
  done < <($K get groups $C $R --fields id,name --format csv --noquotes)
}
scope_id() {
  local id name
  while IFS=, read -r id name; do
    if [ "$name" = "$1" ]; then printf '%s\n' "$id"; return; fi
  done < <($K get client-scopes $C $R --fields id,name --format csv --noquotes)
}
need() { [ -n "$1" ] || { echo "$2" >&2; exit 1; }; }
case "$1" in
  init)
    $K update realms/master $C -s attributes.frontendUrl=https://localhost:9443
    echo "master realm frontend URL set to https://localhost:9443" ;;
  groups) $K get groups $C $R --fields name --format csv --noquotes ;;
  group-create) $K create groups $C $R -s name="$2" >/dev/null && echo "created group $2" ;;
  group-delete)
    g=$(group_id "$2"); need "$g" "no group $2"
    $K delete groups/$g $C $R && echo "deleted group $2" ;;
  group-members)
    g=$(group_id "$2"); need "$g" "no group $2"
    $K get groups/$g/members $C $R --fields username,email --format csv --noquotes ;;
  user-create)
    need "${2:-}" "user-create needs a username"
    need "${3:-}" "user-create needs a group"
    need "${SMOKE_PASSWORD:-}" "SMOKE_PASSWORD isn't set"
    $K create users $C $R -s username="$2" -s enabled=true -s email="$2@example.invalid" \
      -s firstName=MaaS -s lastName=Smoke -s emailVerified=true >/dev/null
    u=$(username_id "$2"); need "$u" "user $2 wasn't created"
    KC_CLI_PASSWORD="$SMOKE_PASSWORD" $K set-password $C $R --userid "$u" --new-password "$SMOKE_PASSWORD" >/dev/null
    g=$(group_id "$3"); need "$g" "no group $3"
    $K update users/$u/groups/$g $C $R -s realm=maas -s userId=$u -s groupId=$g -n >/dev/null
    echo "created user $2 in group $3" ;;
  user-delete)
    u=$(username_id "$2"); need "$u" "no user $2"
    $K delete users/$u $C $R && echo "deleted user $2" ;;
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
    [ -n "${USAGE_CLIENT_SECRET:-}" ] || { echo "USAGE_CLIENT_SECRET isn't set in this pod; apply manifests/maas-sso.yaml" >&2; exit 1; }
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
  oidc-client)
    # The guide's public client for users' sign-in: PKCE S256, localhost
    # redirects, and the groups claim; it holds no secret.
    gs=$(scope_id groups)
    if [ -z "$gs" ]; then
      printf '{"name":"groups","protocol":"openid-connect","attributes":{"include.in.token.scope":"true"}}' |
        $K create client-scopes $C $R -f - >/dev/null
      gs=$(scope_id groups)
      printf '{"name":"groups","protocol":"openid-connect","protocolMapper":"oidc-group-membership-mapper","consentRequired":false,"config":{"claim.name":"groups","full.path":"false","id.token.claim":"true","access.token.claim":"true","introspection.token.claim":"true","userinfo.token.claim":"true"}}' |
        $K create client-scopes/$gs/protocol-mappers/models $C $R -f - >/dev/null
      echo "created client scope groups"
    fi
    obody() {
      printf '{"clientId":"maas-oidc","name":"MaaS OIDC Client","enabled":true,"publicClient":true,'
      printf '"directAccessGrantsEnabled":true,"standardFlowEnabled":true,"implicitFlowEnabled":false,'
      printf '"protocol":"openid-connect","redirectUris":["http://localhost:*","http://127.0.0.1:*","https://localhost:*"],'
      printf '"webOrigins":["*"],"attributes":{"pkce.code.challenge.method":"S256"},'
      printf '"defaultClientScopes":["openid","groups"],"protocolMappers":['
      printf '{"name":"preferred_username","protocol":"openid-connect","protocolMapper":"oidc-usermodel-attribute-mapper","consentRequired":false,'
      printf '"config":{"user.attribute":"username","claim.name":"preferred_username","jsonType.label":"String",'
      printf '"id.token.claim":"true","access.token.claim":"true","introspection.token.claim":"true","userinfo.token.claim":"true"}},'
      printf '{"name":"sub","protocol":"openid-connect","protocolMapper":"oidc-sub-mapper","consentRequired":false,'
      printf '"config":{"introspection.token.claim":"true","access.token.claim":"true"}}]}'
    }
    ocid=$($K get clients $C $R -q clientId=maas-oidc --fields id --format csv --noquotes | head -1)
    if [ -z "$ocid" ]; then
      obody | $K create clients $C $R -f - >/dev/null && echo "created client maas-oidc"
    else
      obody | $K update clients/$ocid $C $R -f - >/dev/null && echo "updated client maas-oidc"
    fi ;;
  sync)
    # Keep the realm in step with this pod's environment: the GitHub OAuth
    # app's credentials, mounted from a Secret that can change after the
    # realm was first imported, and remove the old portals' clients.
    # First, as the bootstrap admin only: the kcadm client this script signs
    # in as from then on. The roles for managing realm "maas" live on the
    # master realm's client "maas-realm"; "master-realm" holds those for the
    # master realm itself (init's frontend URL).
    if [ "$LOGIN" = bootstrap ] && [ -n "${KCADM_CLIENT_SECRET:-}" ]; then
      kbody() { printf '{"clientId":"maas-kcadm","publicClient":false,"serviceAccountsEnabled":true,"standardFlowEnabled":false,"directAccessGrantsEnabled":false,"secret":"%s"}' "$KCADM_CLIENT_SECRET"; }
      kid=$($K get clients $C -r master -q clientId=maas-kcadm --fields id --format csv --noquotes | head -1)
      if [ -z "$kid" ]; then kbody | $K create clients $C -r master -f - >/dev/null
      else kbody | $K update clients/$kid $C -r master --merge -f - >/dev/null; fi
      $K add-roles $C -r master --uusername service-account-maas-kcadm --cclientid maas-realm \
        --rolename view-realm --rolename view-users --rolename query-users --rolename query-groups \
        --rolename manage-users --rolename view-clients --rolename manage-clients --rolename manage-identity-providers
      $K add-roles $C -r master --uusername service-account-maas-kcadm --cclientid master-realm \
        --rolename view-realm --rolename manage-realm
      echo "created or updated the kcadm client"
    fi
    # Values go on standard input (printf is a builtin), not on a command
    # line; --merge keeps the rest of each object. The secrets are hex and
    # GitHub's IDs and secrets alphanumeric, so they need no JSON escaping.
    printf '{"config":{"clientId":"%s","clientSecret":"%s"}}' "$GITHUB_CLIENT_ID" "$GITHUB_CLIENT_SECRET" |
      $K update identity-provider/instances/github $C $R --merge -f - >/dev/null
    for old in maas-portal maas-reconciler; do
      oid=$($K get clients $C $R -q clientId="$old" --fields id --format csv --noquotes | head -1)
      if [ -n "$oid" ]; then $K delete clients/$oid $C $R && echo "deleted client $old"; fi
    done
    echo "synced the GitHub identity provider" ;;
  *) echo "unknown command $1" >&2; exit 2 ;;
esac
