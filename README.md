# moc-maas: Models-as-a-Service in one OpenShift namespace

A Models-as-a-Service (MaaS) that runs entirely inside a single OpenShift project. It gives users an OpenAI-compatible model API behind API keys, plans and per-user token budgets, with sign-in, an admin console, usage dashboards and an audit trail.

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
| Keys and plans | Key portal | UBI 9 Python 3.12 (`12-portal.py`) | user sign-in, self-service keys, and the gateway's key checks |
| | Admin portal | same program | plans, group mappings, service keys, key list, audit trail; writes Limitador's limits |
| | PostgreSQL ×2 | Red Hat PostgreSQL 16 (RHEL 9) | one for the portals, one for Keycloak; daily `pg_dump` backups |
| Metrics | Prometheus, Thanos | OpenShift 4.22 monitoring images | scrapes the MaaS components; Thanos keeps the history on the shared volume (retention set to 90 days) |
| | Grafana | grafana 11.6.0 | admin dashboards, localhost only, read-only |
| | User dashboard | same portal program | each user's own usage only |

The portals (`12-portal.py`) are the only custom code: one Python program that runs as the admin portal, the key portal or the user dashboard.

### One request

![Request flow](docs/request-flow.svg)

1. The gateway removes any `x-maas-*` identity headers the client sent.
2. The key portal checks the API key and the requested model, and returns the caller's user, plan and allowed models (or 401 / 403).
3. Limitador checks the user's token budget for the plan (or 429).
4. The endpoint picker chooses a model pod; the user is passed as the fairness ID, so capacity is shared between users under load.
5. After the response, the gateway reports `usage.total_tokens` to Limitador.

## Tested with

- OpenShift 4.22.14 (Kubernetes 1.35.6)
- Red Hat OpenShift AI 3.5.2, which installs the `InferencePool` API (`inference.networking.k8s.io`) the endpoint picker uses. None of OpenShift AI's own MaaS components are used.

## Prerequisites

- A namespace, with the permissions below granted to its members and service accounts.
- A ReadWriteMany PersistentVolumeClaim in the namespace, for database files, backups and metric blocks.
- Egress from the namespace to the container registries, to GitHub (the gateway downloads the pinned wasm-shim at start) and to PyPI (the portals install their PostgreSQL driver at start).
- A GitHub OAuth App, with:
  - Homepage URL: `https://maas-sso-<namespace>.<apps domain>`
  - Authorization callback URL: `https://maas-sso-<namespace>.<apps domain>/realms/maas/broker/github/endpoint`
- `oc`, `openssl` and `python3` on the machine you deploy from.

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

`pods/exec` is used by the Keycloak admin tool and the test pod; `pods/portforward` is how admins reach the admin portal, the Keycloak console and Grafana.

**Service accounts** used by the manifests:

| Service account | Role | Rules |
|---|---|---|
| `uat-epp` | endpoint picker | `get`, `list`, `watch` on `inferencepools` and `pods`; `get`, `list`, `watch` on `inferenceobjectives` and `inferencemodelrewrites` (`llm-d.ai`); `create`, `patch` on `events` |
| `maas-admin` | admin portal | `get`, `update`, `patch` on the ConfigMap `maas-limits` only (`resourceNames`) |
| `maas-keys` | key portal, user dashboard | none |

Prometheus, Thanos and Grafana need no Kubernetes permissions: Prometheus finds its targets through headless Services. The endpoint picker runs without metrics authentication, because the token and access reviews it would need are cluster-scoped.

## Setup

Every step is a file, numbered in the order you use it: manifests to apply and scripts to run.

```sh
./99-setup-all.sh            # steps 1-18 and 20-28, without public Routes
./99-setup-all.sh --routes   # also creates the Routes (steps 16-18 and 28)
./19-first-plan.sh           # a Keycloak group and a plan for it
```

For the GitHub secret, set `MOC_MAAS_CLIENTID` and `MOC_MAAS_SECRET` on the first run, or run `02-create-github-secret.sh` first. The secret scripts never print values.

| Steps | Files | What they do |
|---|---|---|
| 1–2 | `01-create-secrets.sh`, `02-create-github-secret.sh` | generated secrets; the GitHub client ID and secret |
| 3–7 | `03-emulator.yaml` … `07-client.yaml` | model emulator, Redis, Limitador, endpoint picker, test pod |
| 8–9 | `08-maas-db.yaml`, `09-maas-sso-db.yaml` | the two PostgreSQL databases, with backup CronJobs |
| 10–11 | `10-maas-sso.yaml`, `11-sso-init.sh` | Keycloak, realm `maas`; admin console login through port-forward |
| 12–14 | `12-portal.py`, `12-gen-portal-code.sh`, `13-maas-portal-code.yaml`, `14-maas-portals.yaml` | the portal code, packed into a ConfigMap; admin and key portals |
| 15 | `15-gateway.yaml` | the Envoy gateway |
| 16–18 | `16-…`, `17-…`, `18-maas-gateway-route.yaml` | Routes for the key portal, Keycloak's login pages and the model API |
| 19 | `19-first-plan.sh` | a first plan and the Keycloak group that grants it |
| 20–22 | `20-metrics-store.yaml`, `21-prometheus.yaml`, `22-thanos.yaml` | Prometheus with a Thanos sidecar; Thanos Store, Query and Compactor |
| 23–25 | `23-gen-dashboards.py`, `24-grafana-dashboards.yaml`, `25-grafana.yaml` | the admin Grafana and its dashboards |
| 26–28 | `26-usage-client.sh`, `27-maas-usage.yaml`, `28-maas-usage-route.yaml` | the user dashboard, its Keycloak client and Route |

## Running it

| Tool | What it does |
|---|---|
| `90-maas-admin.sh` | plans, group mappings, service keys, revocation, audit; `forward` opens the admin portal, Keycloak console and Grafana on localhost |
| `91-sso-admin.sh` | Keycloak groups and users |
| `92-test-gateway.sh` | one request through the public gateway with the key in `MOC_MAAS_KEY` |
| `93-backup-now.sh` | runs both database backups now |
| `94-maas-tui.py` | a live terminal view of simulated users with real keys: budgets, 429s, which pod served each request |
| `98-teardown.sh` | removes the MaaS from the namespace; database files, backups and metrics stay on the volume |

**Adding a user:** the user signs in to the key portal with GitHub, then an admin adds them to a Keycloak group that is mapped to a plan (`./91-sso-admin.sh add <email> <group>`). After signing in again, the user can create keys.

**Setting limits:** define plans and map groups to them (`./90-maas-admin.sh plan-set …`, `group-map …`). The admin portal writes Limitador's limits; don't edit the `maas-limits` ConfigMap by hand.

**Viewing usage:** admins use Grafana (dashboards "MaaS usage" and "MaaS serving"); users see their own usage on the user dashboard.

## Adapting to your cluster

The files are written for one deployment. Before applying them elsewhere, change:

- the namespace: `uat-project` throughout the manifests and scripts
- the Route hostnames and Keycloak issuer, which include the namespace and the cluster's apps domain: `10-maas-sso.yaml`, `14-maas-portals.yaml`, `21-prometheus.yaml`, `26-usage-client.sh`, `27-maas-usage.yaml`, `92-test-gateway.sh`
- the shared volume's claim name, `uat-project-storage`, in `08-maas-db.yaml`, `09-maas-sso-db.yaml`, `21-prometheus.yaml` and `22-thanos.yaml` (it also appears in comments in `20`, `93` and `98`)
- the endpoint picker's service account name, `uat-epp`, in `06-epp.yaml`, if you name it differently

## Current limitations

- The model is an emulator; serving a real model means replacing the model Deployment with vLLM on GPUs.
- One PostgreSQL instance each, with daily backups; restores haven't been tested.
- Redis has no password and no persistence; restarting Redis resets the current budget windows.
- Metric history uses Thanos's filesystem store on the shared volume, which Thanos doesn't recommend for production; it can move to an object storage bucket by changing step 20 and the mounts.
- The portals install their PostgreSQL driver from PyPI at start; a built image would remove that dependency.
- Before opening the Routes to real users, add router and Envoy limits (IP allowlists, connection and request-rate limits, request size limits).
