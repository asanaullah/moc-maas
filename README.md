# moc-maas: Models-as-a-Service in one OpenShift namespace

A Models-as-a-Service (MaaS) that runs entirely inside a single OpenShift project. It gives users an OpenAI-compatible model API behind API keys, plans and per-user token budgets, with sign-in, an admin console, usage dashboards and an audit trail.

Deployment and administration are Ansible playbooks. The only scripts are the two that are interactive by nature: `forward.sh` (admin consoles on localhost) and `maas-tui.py` (a live view of the running MaaS).

## Goals

We wanted a MaaS with these constraints:

- **Everything in one namespace.** Every component runs in the project, so the project's members can manage, change and replace any part of it themselves.
- **Namespaced permissions only.** No ClusterRoles, ClusterRoleBindings or components in platform namespaces.
- **No shared databases.** Keys, users and the audit trail are stored in databases inside the project.
- **No workload-specific platform changes.** Running the MaaS needs nothing from the platform beyond namespaced permissions in the project (see [Permissions](#permissions)).
- **Red Hat and upstream components** wherever they fit: Envoy, Kuadrant's Limitador and wasm-shim, llm-d's endpoint picker, Red Hat build of Keycloak, Red Hat's PostgreSQL image, and the Prometheus and Thanos images OpenShift's own monitoring uses.
- **Group-based access.** Users get a plan through their identity-provider groups. Project members are the MaaS admins.

## What you get

| | |
|---|---|
| Model API | `https://maas-gateway-<namespace>.<apps domain>/v1/...`, OpenAI-compatible |
| API keys | users create their own after signing in with GitHub; admins create service keys for applications |
| Plans | allowed models, a token budget per time window, and a maximum key lifetime, assigned through Keycloak groups |
| Token budgets | enforced per user by Limitador, counted from each response's `usage` |
| Dashboards | a Grafana for admins (port-forward only) and a usage page for each user |
| Audit trail | sign-ins, key changes, plan and group changes; daily database backups |

## Architecture

![Architecture](docs/architecture.svg)

| Group | Component | Image / version | Role |
|---|---|---|---|
| Model path | Gateway | Envoy v1.35.12, Kuadrant wasm-shim v0.15.0 | strips client identity headers, checks the key, checks the budget, picks a model pod |
| | Limitador | quay.io/kuadrant/limitador v2.5.0 | token counters per user and plan |
| | Redis | redis 7.4 | Limitador's counter store |
| | Endpoint picker | llm-d endpoint picker v1.0.0 | chooses a model pod per request (prefix cache, queue, KV cache), with flow control |
| | Model | llm-d inference simulator v1.0.0 | a vLLM-compatible emulator, 2 pods, no GPU; replace with vLLM for real models |
| Identity | Keycloak | Red Hat build of Keycloak 26.4 | realm `maas`, GitHub as the identity provider; a sidecar exposes only the realm's public paths |
| Keys and plans | Key portal | UBI 9 Python 3.12 (`roles/maas_portals/files/portal.py`) | user sign-in, self-service keys, and the gateway's key checks |
| | Admin portal | same program | plans, group mappings, service keys, key list, audit trail; writes Limitador's limits |
| | PostgreSQL ×2 | Red Hat PostgreSQL 16 (RHEL 9) | one for the portals, one for Keycloak; daily `pg_dump` backups |
| Metrics | Prometheus, Thanos | OpenShift 4.22 monitoring images | scrapes the MaaS components; Thanos keeps the history on the shared volume (retention set to 90 days) |
| | Grafana | grafana 11.6.0 | admin dashboards, localhost only, read-only |
| | User dashboard | same portal program | each user's own usage only |

The portals (`portal.py`) are the only custom code: one Python program that runs as the admin portal, the key portal or the user dashboard.

### One request

![Request flow](docs/request-flow.svg)

1. The gateway removes any `x-maas-*` identity headers the client sent.
2. The key portal checks the API key and the requested model, and returns the caller's user, plan and allowed models (or 401 / 403).
3. Limitador checks the user's token budget for the plan (or 429).
4. The endpoint picker chooses a model pod; the user is passed as the fairness ID, so capacity is shared between users under load.
5. After the response, the gateway reports `usage.total_tokens` to Limitador.

## Tested with

- OpenShift 4.22.14 (Kubernetes 1.35.6)
- Red Hat OpenShift AI 3.5.2, which installs the `InferencePool` API (`inference.networking.k8s.io`) the endpoint picker uses.

## Layout

```
playbooks/     the entry points: deploy, first-plan, admin, sso-admin, …
manifests/     the Kubernetes manifests, named for what they deploy
roles/         one Ansible role per component
  maas_common/                        shared tasks: apply manifests, wait for rollouts, restart, find pods
  maas_secrets/                       the generated and GitHub Secrets
  maas_sso_admin/files/kcadm.sh       Keycloak admin, run inside a Keycloak pod with its own credentials
  maas_portals/files/portal.py        the portal program (admin, keys, usage dashboards)
  maas_grafana/files/gen-dashboards.py  renders the Grafana dashboards
clusters/      one inventory per cluster: the namespace and the apps domain
docs/          architecture and request-flow diagrams
build.sh       installs the Ansible collections, syntax-checks the playbooks
forward.sh     admin consoles on localhost, until Ctrl-C
maas-tui.py    a live terminal view of the running MaaS
```

The manifests are static and named for what they deploy; the playbooks own the ordering. Two ConfigMaps are generated at deploy time instead of living in git: the portals' code (from `portal.py`) and Grafana's dashboards (from `gen-dashboards.py`). The playbooks read the namespace and the apps domain from the cluster inventory, so `clusters/<cluster>.yml` is the one place the deploy knows which cluster it is on.

## Prerequisites

- A namespace, with the permissions below granted to its members and service accounts.
- A ReadWriteMany PersistentVolumeClaim in the namespace, for database files, backups and metric blocks.
- Egress from the namespace to the container registries, to GitHub (the gateway downloads the pinned wasm-shim at start) and to PyPI (the portals install their PostgreSQL driver at start).
- A GitHub OAuth App, with:
  - Homepage URL: `https://maas-sso-<namespace>.<apps domain>`
  - Authorization callback URL: `https://maas-sso-<namespace>.<apps domain>/realms/maas/broker/github/endpoint`
- On the machine you deploy from: `ansible-core` 2.16 or newer, its Python able to import `kubernetes` (Fedora/RHEL: `python3-kubernetes`), and a kubeconfig for the cluster (an `oc login` gives you one). Run `./build.sh` once per checkout. `oc` itself is only needed by `forward.sh`; `maas-tui.py` uses the same login.

## Permissions

Everything is namespaced. A cluster admin creates these once, in the namespace.

**Project members** (the MaaS admins) need:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: maas-project-admin
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log"]
    verbs: ["create", "get", "list", "watch", "delete"]
  - apiGroups: [""]
    resources: ["pods/exec", "pods/portforward"]
    verbs: ["create", "get"]
  - apiGroups: [""]
    resources: ["services", "configmaps", "secrets"]
    verbs: ["create", "get", "list", "watch", "update", "patch", "delete"]
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["create", "get", "list", "watch", "update", "patch", "delete"]
  - apiGroups: ["apps"]
    resources: ["replicasets"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["create", "get", "list", "watch", "delete"]
  - apiGroups: ["batch"]
    resources: ["cronjobs"]
    verbs: ["create", "get", "list", "watch", "update", "patch", "delete"]
  - apiGroups: ["route.openshift.io"]
    resources: ["routes"]
    verbs: ["create", "get", "list", "watch", "update", "patch", "delete"]
  - apiGroups: ["inference.networking.k8s.io"]
    resources: ["inferencepools"]
    verbs: ["create", "get", "list", "watch", "update", "patch", "delete"]
```

`pods/exec` is how the playbooks run the Keycloak and admin-portal commands inside their pods (both portals' admin APIs stay on pod-localhost, so this adds no reach the old port-forward didn't have); `pods/portforward` is how admins reach the admin portal, the Keycloak console and Grafana.

**Service accounts** used by the manifests:

| Service account | Role | Rules |
|---|---|---|
| `uat-epp` | endpoint picker | `get`, `list`, `watch` on `inferencepools` and `pods`; `get`, `list`, `watch` on `inferenceobjectives` and `inferencemodelrewrites` (`llm-d.ai`); `create`, `patch` on `events` |
| `maas-admin` | admin portal | `get`, `update`, `patch` on the ConfigMap `maas-limits` only (`resourceNames`) |
| `maas-keys` | key portal, user dashboard | none |

Prometheus, Thanos and Grafana need no Kubernetes permissions: Prometheus finds its targets through headless Services. The endpoint picker runs without metrics authentication, because the token and access reviews it would need are cluster-scoped.

## Setup

```sh
./build.sh
MOC_MAAS_CLIENTID=… MOC_MAAS_SECRET=… \
  ansible-playbook -i clusters/oac-dev-workload0.yml playbooks/deploy.yml
ansible-playbook -i clusters/oac-dev-workload0.yml playbooks/first-plan.yml
```

`deploy.yml` is safe to re-run: it applies the same manifests, only creates Secrets that are missing, and restarts deployments to pick up changed code and config. The public Routes are left out unless you ask for them (`-e maas_routes=true`), so the MaaS can come up without public endpoints first.

The GitHub OAuth App's client ID and secret (`MOC_MAAS_CLIENTID` and `MOC_MAAS_SECRET` in the environment) are only needed while `maas-sso-github` doesn't exist yet — a re-deploy never touches an existing one, so stale environment values can't rotate it by accident. `playbooks/github-secret.yml` creates or rotates it on purpose. Secret values are never printed.

To redeploy from scratch over an earlier installation, run `playbooks/teardown.yml -e maas_wipe_data=true` first. The wipe exists for Keycloak: its database keeps the bootstrap-admin password it was first given and never resets it from the Secret on later starts, so a redeploy over an old database with a regenerated `maas-sso-admin` leaves the admin login broken. The PostgreSQL images, unlike Keycloak, reset their user's password from the Secret on every start.

`deploy.yml` runs one play per component, in dependency order:

| Play | Manifests | What it does |
|---|---|---|
| Secrets | — | the generated Secrets (only created when missing) and the GitHub client Secret |
| Model path | `emulator`, `redis`, `limitador`, `epp`, `client` | model emulator, Redis, Limitador, endpoint picker, test pod |
| Databases | `maas-db`, `maas-sso-db` | the two PostgreSQL databases, with backup CronJobs |
| Keycloak | `maas-sso` | Keycloak, realm `maas`; admin console login pointed at the port-forward |
| Portals | `maas-portals` (+ `portal.py`) | the portal code as a ConfigMap; the admin and key portals |
| Gateway | `gateway` | the Envoy gateway |
| Metrics | `metrics-store`, `prometheus`, `thanos` | Prometheus with a Thanos sidecar; Thanos Store, Query and Compactor |
| Grafana | `grafana` (+ `gen-dashboards.py`) | the admin Grafana and its dashboards |
| User dashboard | `maas-usage` | the user dashboard and its Keycloak client |
| Public addresses | `maas-keys-route`, `maas-sso-route`, `maas-gateway-route`, `maas-usage-route` | the public Routes — only with `-e maas_routes=true` |

`first-plan.yml` then creates a Keycloak group (`maas-standard`) and a plan (`standard`, 20000 tokens per 60s) and maps the group to it; every value can be overridden with `-e`.

## Running it

Run everything from the repository root. `<cluster>` below is your inventory file under `clusters/`.

| Command | What it does |
|---|---|
| `ansible-playbook -i clusters/<cluster>.yml playbooks/admin.yml -e maas_action=<action>` | plans, group mappings, service keys, revocation, audit (actions listed in the playbook) |
| `ansible-playbook -i clusters/<cluster>.yml playbooks/sso-admin.yml -e maas_action=<action>` | Keycloak groups and users |
| `MOC_MAAS_KEY=… ansible-playbook -i clusters/<cluster>.yml playbooks/test-gateway.yml` | one request through the public gateway; prints the response and the HTTP status |
| `ansible-playbook -i clusters/<cluster>.yml playbooks/backup-now.yml` | runs both database backups now, from their CronJobs |
| `ansible-playbook -i clusters/<cluster>.yml playbooks/github-secret.yml` | creates or rotates the GitHub Secret |
| `ansible-playbook -i clusters/<cluster>.yml playbooks/teardown.yml` | removes the MaaS from the namespace; database files, backups and metrics stay on the volume (`-e maas_wipe_data=true` also wipes the databases, for a fresh install) |
| `./forward.sh` | the admin portal, Keycloak console and Grafana on localhost until Ctrl-C |
| `./maas-tui.py` | a live terminal view of simulated users with real keys: budgets, 429s, which pod served each request |

Some examples:

```sh
ansible-playbook -i clusters/oac-dev-workload0.yml playbooks/admin.yml \
  -e maas_action=plan-set -e maas_plan=standard -e maas_tokens=20000 -e maas_models=maas-sim
ansible-playbook -i clusters/oac-dev-workload0.yml playbooks/admin.yml \
  -e maas_action=group-map -e maas_group=maas-standard -e maas_plan=standard
ansible-playbook -i clusters/oac-dev-workload0.yml playbooks/admin.yml \
  -e maas_action=service-key -e maas_owner=myapp -e maas_key_name=prod \
  -e maas_plan=standard -e maas_days=30 -e maas_secret_name=myapp-key
ansible-playbook -i clusters/oac-dev-workload0.yml playbooks/sso-admin.yml \
  -e maas_action=add -e maas_email=user@example.com -e maas_group=maas-standard
```

**Public addresses:** the sso Route has no landing page — the Envoy sidecar in front of Keycloak answers only `/realms/maas/` and `/resources/` and 404s everything else, including `/` and `/admin/`. That is deliberate: the public address can serve realm logins and GitHub callbacks, but never the admin console or the master realm. Users start at the key portal (`https://maas-keys-<namespace>.<apps domain>/`); the Keycloak admin console is reached with `./forward.sh` at `https://localhost:9443/admin/` (the certificate is for the Service name, so the browser warns).

**Adding a user:** the user signs in to the key portal with GitHub, then an admin adds them to a Keycloak group that is mapped to a plan (the `add` example above). After signing in again, the user can create keys.

**Setting limits:** define plans and map groups to them (the `plan-set` and `group-map` examples above). The admin portal writes Limitador's limits; don't edit the `maas-limits` ConfigMap by hand.

**Viewing usage:** admins use Grafana (dashboards "MaaS usage" and "MaaS serving"); users see their own usage on the user dashboard.

## Adapting to your cluster

The manifests are written for one deployment. Before applying them elsewhere, change:

- the namespace: `uat-project` in `manifests/` and in `forward.sh`, and `maas_namespace` in your cluster inventory — the playbooks, the generated ConfigMaps and the Keycloak wrapper read it from the inventory or the pod itself
- the Route hostnames and Keycloak issuer, which embed the namespace and the cluster's apps domain: `manifests/maas-sso.yaml`, `manifests/maas-portals.yaml`, `manifests/prometheus.yaml`, `manifests/maas-usage.yaml`, and `maas_apps_domain` in the cluster inventory
- the shared volume's claim name, `uat-project-storage`, in `manifests/maas-db.yaml`, `manifests/maas-sso-db.yaml`, `manifests/prometheus.yaml` and `manifests/thanos.yaml`, and as `maas_storage_claim` in `roles/maas_common/defaults/main.yml` (the teardown's wipe step reads it)
- the endpoint picker's service account name, `uat-epp`, in `manifests/epp.yaml`, if you name it differently

Copy `clusters/oac-dev-workload0.yml` to a new file per cluster; that is the inventory the playbooks take with `-i`.

## Current limitations

- The model is an emulator; serving a real model means replacing the model Deployment with vLLM on GPUs.
- One PostgreSQL instance each, with daily backups; restores haven't been tested.
- Rotating `maas-sso-admin` does not change the password already stored in Keycloak's database; wipe the databases (`teardown.yml -e maas_wipe_data=true`) and redeploy for a working admin login.
- Redis has no password and no persistence; restarting Redis resets the current budget windows.
- Metric history uses Thanos's filesystem store on the shared volume, which Thanos doesn't recommend for production; it can move to an object storage bucket by changing `manifests/metrics-store.yaml` and the mounts.
- The portals install their PostgreSQL driver from PyPI at start; a built image would remove that dependency.
- Before opening the Routes to real users, add router and Envoy limits (IP allowlists, connection and request-rate limits, request size limits).
