#!/bin/bash
# Step 1: creates the MaaS's generated secrets in uat-project, each only if it doesn't
# exist yet, so re-running is safe:
#   maas-db           PostgreSQL for the portals (POSTGRESQL_*, DATABASE_URL)
#   maas-sso-db       PostgreSQL for Keycloak (POSTGRESQL_*)
#   maas-sso-admin    Keycloak's bootstrap admin (KC_BOOTSTRAP_ADMIN_*)
#   maas-portal-oidc  the key portal's OIDC client secret (client-secret)
#   maas-usage-oidc   the user dashboard's OIDC client secret (client-secret)
# To rotate one, delete it, re-run this, and restart what uses it; the
# PostgreSQL image resets its user's password from the Secret on each start.
#
# Values are random (192 bits), never printed, and reach oc through pipes
# rather than its command line. Server-side apply keeps them out of a
# last-applied-configuration annotation. The GitHub client secret comes from
# outside; see 02-create-github-secret.sh.
set -euo pipefail
NS=uat-project

gen() { openssl rand -hex 24; }

exists() { oc get secret "$1" -n "$NS" >/dev/null 2>&1; }

apply() {
  oc label --local -f - app.kubernetes.io/part-of=maas-test -o yaml |
    oc apply --server-side --field-manager=maas-admin -f - >/dev/null
  echo "created $1"
}

if exists maas-db; then echo "maas-db exists, left as it is"; else
  p=$(gen)
  oc create secret generic maas-db -n "$NS" \
    --from-literal=POSTGRESQL_USER=maas \
    --from-literal=POSTGRESQL_DATABASE=maas \
    --from-file=POSTGRESQL_PASSWORD=<(printf '%s' "$p") \
    --from-file=DATABASE_URL=<(printf 'postgresql://maas:%s@maas-db:5432/maas' "$p") \
    --dry-run=client -o yaml | apply maas-db
  unset p
fi

if exists maas-sso-db; then echo "maas-sso-db exists, left as it is"; else
  oc create secret generic maas-sso-db -n "$NS" \
    --from-literal=POSTGRESQL_USER=keycloak \
    --from-literal=POSTGRESQL_DATABASE=keycloak \
    --from-file=POSTGRESQL_PASSWORD=<(gen | tr -d '\n') \
    --dry-run=client -o yaml | apply maas-sso-db
fi

if exists maas-sso-admin; then echo "maas-sso-admin exists, left as it is"; else
  oc create secret generic maas-sso-admin -n "$NS" \
    --from-literal=KC_BOOTSTRAP_ADMIN_USERNAME=maas-admin \
    --from-file=KC_BOOTSTRAP_ADMIN_PASSWORD=<(gen | tr -d '\n') \
    --dry-run=client -o yaml | apply maas-sso-admin
fi

if exists maas-portal-oidc; then echo "maas-portal-oidc exists, left as it is"; else
  oc create secret generic maas-portal-oidc -n "$NS" \
    --from-file=client-secret=<(gen | tr -d '\n') \
    --dry-run=client -o yaml | apply maas-portal-oidc
fi

if exists maas-usage-oidc; then echo "maas-usage-oidc exists, left as it is"; else
  oc create secret generic maas-usage-oidc -n "$NS" \
    --from-file=client-secret=<(gen | tr -d '\n') \
    --dry-run=client -o yaml | apply maas-usage-oidc
fi
