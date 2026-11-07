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
| API keys | users create their own after signing in with GitHub; admins create service keys for applications; at most five active keys per owner |
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

The manifests are named for what they deploy and rendered as templates on the cluster inventory — the namespace and the apps domain; the playbooks own the ordering. Two ConfigMaps are generated at deploy time instead of living in git: the portals' code (from `portal.py`) and Grafana's dashboards (from `gen-dashboards.py`). So `clusters/<cluster>.yml` is the one place the deploy knows which cluster and namespace it is on.

## Prerequisites

- A namespace, with the permissions below granted to its members and service accounts.
- A ReadWriteMany PersistentVolumeClaim named `<namespace>-storage` in the namespace, for database files, backups and metric blocks.
- Egress from the namespace to the container registries, to GitHub (the gateway downloads the pinned wasm-shim at start) and to PyPI (the portals install their PostgreSQL driver at start).
- A GitHub OAuth App, with:
  - Homepage URL: `https://maas-sso-<namespace>.<apps domain>`
  - Authorization callback URL: `https://maas-sso-<namespace>.<apps domain>/realms/maas/broker/github/endpoint`
- On the machine you deploy from: `ansible-core` 2.16 or newer, its Python able to import `kubernetes` (Fedora/RHEL: `python3-kubernetes`), and a kubeconfig for the cluster (an `oc login` gives you one). Run `./build.sh` once per checkout. `oc` itself is only needed by `forward.sh`; `maas-tui.py` uses the same login.

## Permissions

Everything is namespaced. A cluster admin creates these once, in the namespace.

The Role for project members (the MaaS admins), the three service accounts and their Roles are in `manifests/prereqs/`. `deploy.yml` doesn't apply them; a cluster admin applies them once and binds the members' Role to the project's members group:

```sh
oc apply -n <namespace> -f manifests/prereqs/
oc create rolebinding maas-project-admin -n <namespace> --role=maas-project-admin --group=<members group>
```

On oac-dev-workload0 the uat-harness chart (in `oac-apps-fork`) applies this RBAC, and the members group for `uat-project` is `project_uat`.

`pods/exec` is how the playbooks run the Keycloak and admin-portal commands inside their pods (both portals' admin APIs stay on pod-localhost, so this adds no reach the old port-forward didn't have); `pods/portforward` is how admins reach the admin portal, the Keycloak console and Grafana. The COSI rules (create and get on `bucketclaims` and `bucketaccesses`) are for the metrics bucket.

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

`deploy.yml` is safe to re-run: it applies the same manifests, only creates Secrets that are missing, and restarts deployments to pick up changed code and config. The public Routes are left out unless you ask for them (`-e maas_routes=true`), so the MaaS can come up without public endpoints first. To deploy into another namespace of the same cluster, pass `-e maas_namespace=<name>`; every manifest renders for it, and the namespace must already meet the [prerequisites](#prerequisites).

The GitHub OAuth App's client ID and secret (`MOC_MAAS_CLIENTID` and `MOC_MAAS_SECRET` in the environment) are only needed while `maas-sso-github` doesn't exist yet — a re-deploy never touches an existing one, so stale environment values can't rotate it by accident. `playbooks/github-secret.yml` creates or rotates it on purpose. Secret values are never printed.

A teardown without wipe keeps the databases and the generated Secrets they need, so a later `deploy.yml` matches them. To redeploy from scratch, run `playbooks/teardown.yml -e maas_wipe_data=true` first: it wipes the databases and deletes their Secrets together. (Keycloak's database keeps the bootstrap-admin password it was first given and never resets it from the Secret.) The playbooks sign in to Keycloak as their own `maas-kcadm` client once the first deploy has created it, so they don't depend on the bootstrap admin after that.

`deploy.yml` runs one play per component, in dependency order:

| Play | Manifests | What it does |
|---|---|---|
| Secrets | — | the generated Secrets (only created when missing) and the GitHub client Secret |
| Model path | `emulator`, `redis`, `limitador`, `epp`, `client` | model emulator, Redis, Limitador, endpoint picker, test pod |
| Databases | `maas-db`, `maas-sso-db` | the two PostgreSQL databases, with backup CronJobs |
| Keycloak | `maas-sso` | Keycloak, realm `maas`; admin console login pointed at the port-forward |
| Portals | `maas-portals` (+ `portal.py`) | the portal code as a ConfigMap; the admin and key portals |
| Gateway | `gateway` | the Envoy gateway |
| Metrics | `prometheus`, `thanos` | the metrics bucket (a COSI BucketClaim and BucketAccess), Prometheus with a Thanos sidecar; Thanos Store, Query and Compactor |
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
| `MOC_MAAS_KEY=… ansible-playbook -i clusters/<cluster>.yml playbooks/test-gateway.yml` | one request through the public gateway; prints the response and the HTTP status (streaming and `include_usage` with `-e maas_stream=true -e maas_include_usage=true`) |
| `ansible-playbook -i clusters/<cluster>.yml playbooks/backup-now.yml` | runs both database backups now, from their CronJobs |
| `ansible-playbook -i clusters/<cluster>.yml playbooks/github-secret.yml` | creates or rotates the GitHub Secret |
| `ansible-playbook -i clusters/<cluster>.yml playbooks/restore-test.yml` | restores the newest dumps into throwaway databases and prints their counts next to the live ones |
| `ansible-playbook -i clusters/<cluster>.yml playbooks/teardown.yml` | removes the MaaS from the namespace; database files and backups stay on the volume with the Secrets they need, and the metrics bucket is left alone (`-e maas_wipe_data=true` also wipes the databases and their Secrets, for a fresh install) |
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

**Adding a user:** the user signs in to the key portal with GitHub, then an admin adds them to a Keycloak group that is mapped to a plan (the `add` example above). After signing in again, the user can create keys. Keep MaaS groups top-level; a subgroup's name isn't distinguished from a top-level group's.

**Users in several groups** get one plan: the mapped plan with the highest priority (`-e maas_priority=`, default 0), then the highest rate (tokens per second), then the name. Its budget, models and key lifetime all apply. Each user holds at most five active keys and creates at most ten per hour.

**How budgets behave:** tokens are counted from each response's `usage` *after* the response is served, so the budget is a soft limit — a user at the edge of their budget can overshoot by roughly concurrency × the request's size. A request that names no `max_tokens` is bounded only by the model's context length. Two optional plan settings bound the overshoot: `max_request_tokens` (`-e maas_max_request_tokens=`) refuses requests whose `max_tokens` or `max_completion_tokens` is above it, or that name neither; `max_requests` (`-e maas_max_requests=`) caps each user's requests per window (429 with `x-maas-refusal: requests` and a JSON error body, like budget refusals). With both, the overshoot is at most `max_requests × max_request_tokens`. A budget window starts at the user's first request after the previous window expired (Limitador sets a counter's expiry on first use and doesn't move it), so a user can spend one budget at the end of a window and another at the start of the next. For plans with windows over an hour, an hourly cap bounds that: by default twice the plan's average hourly rate (a daily plan of 24,000 tokens allows 2,000 per hour), or the plan's own `hourly_tokens` (`-e maas_hourly_tokens=`, 0 for none). Streaming requests must set `stream_options.include_usage: true` (the gateway refuses them otherwise), so every accepted stream is charged.

**Restoring a backup:** the daily `pg_dump`s land on the storage volume under `maas/backups` (portals) and `maas/sso-backups` (Keycloak). To restore: stop the portals (`oc scale deploy/maas-admin deploy/maas-keys deploy/maas-usage --replicas=0`), point the database at the dump (`pg_restore -U maas -d maas /backup/<file>` inside a pod that mounts the volume, after clearing or recreating the database), scale the portals back up, and run `admin.yml -e maas_action=limits` so Limitador's limits match the restored plans. `playbooks/restore-test.yml` restores the latest dumps into throwaway databases and prints sanity counts, so the dumps are known-restorable without touching the live ones; run it after any schema change and regularly.

**A dead node:** if a node is down and a database pod shows `Terminating` or `Unknown`, first confirm with the cluster admin that the node is really off. Only then run `oc delete pod -n <namespace> <pod> --force --grace-period=0`. Never force-delete while the node might still be running: two database servers on the shared volume corrupt it.

**Setting limits:** define plans and map groups to them (the `plan-set` and `group-map` examples above). The admin portal writes Limitador's limits; don't edit the `maas-limits` ConfigMap by hand.

**Viewing usage:** admins use Grafana (dashboards "MaaS usage" and "MaaS serving"); users see their own usage on the user dashboard.

**Alerts:** the MaaS Prometheus evaluates the alerting rules in `manifests/prometheus.yaml` (`alerts.yml`): unreachable targets, a stopped or refusing reconciler, plans without a loaded limit, Limitador's limits unreadable, key-check connection failures, a filling volume, database connections near the limit, failed metric uploads and a halted compactor. Firing alerts show in the "Health" row of the "MaaS serving" dashboard and at Prometheus's `/alerts`. They aren't sent anywhere yet.

## Adapting to your cluster

The namespace is a variable throughout: the manifests render for it, the playbooks and generated ConfigMaps read it from the inventory, the portals read it from their own service account, and the shared volume is expected as `<namespace>-storage`. Per cluster, copy `clusters/oac-dev-workload0.yml` and set:

- `maas_namespace` — the namespace to deploy into by default (any other with `-e maas_namespace=<name>`)
- `maas_apps_domain` — the cluster's apps domain, which the Route hostnames and the Keycloak issuer embed
- `maas_s3_endpoint` — the object-storage endpoint the metrics bucket is reached through, and the COSI class names in `roles/maas_metrics/defaults/main.yml` (`fb-bucket` and `fb-access-rw` here)
- `maas_s3_ca` — the PEM of the CA that signs the S3 endpoint's certificate; leave it out to use the cluster's ingress CA (`openshift-config-managed/default-ingress-cert`), if your user can read it
- `maas_route_concurrent_tcp` and `maas_route_rate_http` — the router's per-address limits on the public Routes, and `maas_gateway_rate_per_second` and `maas_gateway_rate_burst` — each gateway pod's request-rate cap (defaults 50 connections and 100 requests per address, 200 per second with bursts of 400 per pod, in `roles/maas_common/defaults/main.yml`). These protect the key check before the caller is known, so they can't be per plan; a plan's own request limit is `max_requests`.
- `maas_gateway_drain_seconds` — how long a stopping gateway pod keeps serving, so rollouts and node drains don't cut generations (default 120); set it above your longest generation
- the endpoint picker's service account name, `uat-epp`, in `manifests/epp.yaml`, if you name it differently

## Current limitations

- The model is an emulator; serving a real model means replacing the model Deployment with vLLM on GPUs, with the same `--ssl-certfile` and `--ssl-keyfile` arguments and the `maas-sim-tls` certificate (or one for the new Service's name, which the gateway's `original_destination_cluster` checks). The model pods serve TLS; the gateway checks their certificate, while the endpoint picker's metric scrape is encrypted but unverified (v1.0.0 can't set the name to check against a pod IP).
- One PostgreSQL instance each, with daily backups on the same volume as the data; `restore-test.yml` checks the dumps restore.
- Rotating `maas-sso-admin` does not change the password already stored in Keycloak's database; the playbooks use their own `maas-kcadm` client, so only the console login is affected.
- Redis has no password and no persistence; restarting Redis resets the current budget windows.
- Metric history is in an S3 bucket provisioned through COSI; its endpoint's CA comes from `maas_s3_ca` or the cluster's ingress CA.
- The portals install their PostgreSQL driver from PyPI at start; a built image would remove that dependency.
- The Routes and the gateway have connection and request-rate limits (per client address at the router, per pod in Envoy) and a 4 MiB request size limit; there are no IP allowlists.
