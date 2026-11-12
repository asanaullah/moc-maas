#!/usr/bin/env bash
# Assisted by Claude Opus
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./build.sh

Prepares the playbooks to run:
  1. installs the Ansible collections in requirements.yml into ./collections
  2. checks that Ansible's Python can import the kubernetes library
  3. syntax-checks every playbook
  4. renders every manifest for a test namespace and parses the result
  5. compiles the Python files and runs the unit tests (tests/)
  6. with podman, validates both Envoy configurations with the pinned Envoy image,
     and the Prometheus alerting rules with promtool

Options:
  -h   show this help
EOF
}

while getopts "h" opt; do
  case "$opt" in
    h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")"

command -v ansible-playbook >/dev/null || { echo "ansible-playbook not found; install ansible-core 2.16 or newer" >&2; exit 1; }

echo "== collections"
ansible-galaxy collection install -r requirements.yml -p collections

echo "== python"
python="$(ansible --version | sed -n 's/^  python version = .*(\(.*\))$/\1/p')"
"${python:-python3}" -c 'import kubernetes; print("kubernetes", kubernetes.__version__, "for", __import__("sys").executable)' || {
  echo "Ansible's Python cannot import kubernetes; install it (Fedora/RHEL: python3-kubernetes, pip: kubernetes)" >&2
  exit 1
}

echo "== shell syntax"
while IFS= read -r script; do
  bash -n "$script"
  echo "  ok  $script"
done < <(git ls-files '*.sh' ':(exclude)collections/**')

echo "== syntax"
cluster="$(ls clusters/*.yml | head -1)"
for playbook in playbooks/*.yml; do
  case "$playbook" in playbooks/_*) continue ;; esac
  ansible-playbook -i "$cluster" --syntax-check "$playbook" >/dev/null
  echo "  ok  $playbook"
done

echo "== manifests"
# The manifests are templates on the inventory vars; render them for a made-up
# namespace to catch broken Jinja or YAML before a deploy does. The same pass
# enforces the public surface and the pod security baseline.
"${python:-python3}" - <<'EOF'
import glob, re, sys, yaml
from jinja2 import Template

VARS = yaml.safe_load(open("roles/maas_common/defaults/main.yml")) or {}
VARS.update({"maas_namespace": "build-check", "maas_apps_domain": "apps.build-check.example",
        "maas_route_concurrent_tcp": 50, "maas_route_rate_http": 100,
        "maas_gateway_drain_seconds": 120,
        "maas_kube_crd_configmaps": ["maas-kube-crd-build-check"], "maas_kube_crd_hash": "build-check",
        "maas_governance_models": [{"name": "maas-sim", "publisher_id": "publishers/build-check/models/maas-sim"}],
        "maas_wasm_config_json": '{"services":{},"actionSets":[]}'})

EXPECTED_ROUTES = ["maas-gateway", "maas-sso", "maas-usage"]
INTERNAL = {"maas-db", "maas-sso-db", "maas-kube", "maas-redis", "maas-limitador",
            "maas-epp", "maas-ipp-pre", "maas-ipp-post", "maas-prometheus",
            "maas-thanos-query", "maas-thanos-store"}
# Pods that need the Kubernetes API token, with the reason.
AUTOMOUNT = {"maas-epp": "leader election and InferencePool discovery"}
# Extra capabilities, keyed by (workload, container, capability), with the reason.
ADD_CAPS = {}
# Jinja markers, not Go templates: the Prometheus alert annotations keep
# {{ $labels.job }} on purpose.
JINJA = re.compile(r"\{\{\s*[A-Za-z_][A-Za-z0-9_.]*\s*[}|.]|\{%")


def fail(message):
    sys.exit("FAIL  " + message)


def pod_spec(doc):
    kind = doc.get("kind")
    if kind == "Pod":
        return doc.get("spec") or {}
    if kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"):
        return ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}
    if kind == "CronJob":
        job = ((doc.get("spec") or {}).get("jobTemplate") or {}).get("spec") or {}
        return (job.get("template") or {}).get("spec") or {}
    return None


routes, services = [], []
for path in sorted(glob.glob("manifests/*.yaml")):
    text = Template(open(path).read()).render(**VARS)
    if JINJA.search(text):
        fail(f"{path}: rendered output still contains a Jinja marker")
    docs = [d for d in yaml.safe_load_all(text) if d]
    if not docs:
        fail(f"{path}: renders to nothing")
    for doc in docs:
        kind = doc.get("kind")
        name = (doc.get("metadata") or {}).get("name", "?")
        if kind == "Route":
            routes.append(name)
            target = ((doc.get("spec") or {}).get("to") or {}).get("name")
            if target in INTERNAL:
                fail(f"{path}: Route {name} targets internal service {target}")
        if kind == "Service":
            stype = (doc.get("spec") or {}).get("type", "ClusterIP")
            services.append((name, stype))
            if stype in ("LoadBalancer", "NodePort"):
                fail(f"{path}: Service {name} is {stype}")
        spec = pod_spec(doc)
        if spec is None:
            continue
        if spec.get("hostNetwork"):
            fail(f"{path}: {kind} {name} uses hostNetwork")
        for volume in spec.get("volumes") or []:
            if "hostPath" in volume:
                fail(f"{path}: {kind} {name} mounts hostPath {volume['hostPath']}")
        if spec.get("automountServiceAccountToken") is not False and name not in AUTOMOUNT:
            fail(f"{path}: {kind} {name} does not set automountServiceAccountToken: false")
        for container in (spec.get("containers") or []) + (spec.get("initContainers") or []):
            cname = container.get("name", "?")
            image = container.get("image") or ""
            if image and "@sha256:" not in image:
                fail(f"{path}: {kind} {name} container {cname} image is not digest-pinned: {image}")
            sc = container.get("securityContext") or {}
            if sc.get("privileged"):
                fail(f"{path}: {kind} {name} container {cname} is privileged")
            if sc:
                if sc.get("allowPrivilegeEscalation") is not False:
                    fail(f"{path}: {kind} {name} container {cname} does not set allowPrivilegeEscalation: false")
                if (sc.get("seccompProfile") or {}).get("type") != "RuntimeDefault":
                    fail(f"{path}: {kind} {name} container {cname} does not set seccompProfile.type: RuntimeDefault")
                caps = sc.get("capabilities") or {}
                if "ALL" not in {str(v).upper() for v in caps.get("drop") or []}:
                    fail(f"{path}: {kind} {name} container {cname} does not drop ALL capabilities")
                for cap in caps.get("add") or []:
                    if (name, cname, str(cap)) not in ADD_CAPS:
                        fail(f"{path}: {kind} {name} container {cname} adds capability {cap} without an allowlist reason")
    print(f"  ok  {path} ({len(docs)} objects)")

if sorted(routes) != EXPECTED_ROUTES:
    fail(f"Routes {sorted(routes)} do not match {EXPECTED_ROUTES}")
if {stype for _, stype in services} != {"ClusterIP"}:
    fail("not every Service is ClusterIP")
print(f"  ok  {len(routes)} Routes, {len(services)} ClusterIP Services, security baseline")
EOF

echo "== python files"
mapfile -t python_files < <(git ls-files '*.py' ':(exclude)collections/**')
python3 -m py_compile "${python_files[@]}"
echo "  ok  ${#python_files[@]} files"

echo "== unit tests"
out="$(python3 -m unittest discover -s tests 2>&1)" || { echo "$out" | grep -v '"POST /check' >&2; exit 1; }
echo "$out" | tail -1
find . -name __pycache__ -not -path './collections/*' -prune -exec rm -rf {} +
echo "  ok"

echo "== governance"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
ansible-playbook -i "$cluster" tests/governance-render.yml -e out="$tmp" >/dev/null
diff -ru tests/golden "$tmp"
echo "  ok"

echo "== governance checks"
for f in tests/fixtures/bad-*.yaml; do
  n="$(basename "$f" | cut -d- -f2)"
  if ansible-playbook -i "$cluster" tests/governance-render.yml -e out="$tmp" -e maas_governance_fixture="$PWD/$f" >"$tmp/out" 2>"$tmp/err"; then
    echo "  FAIL  $f rendered without an error" >&2; exit 1
  fi
  if ! grep -q "rule $n:" "$tmp/err" "$tmp/out"; then
    echo "  FAIL  $f: expected the rule $n message" >&2
    cat "$tmp/err" "$tmp/out" >&2
    exit 1
  fi
  echo "  ok  $(basename "$f")"
done

echo "== deploy guard"
for case in mismatch protected noconfirm; do
  if ansible-playbook -i "$cluster" tests/preflight-guard.yml -e "guard_case=$case" >"$tmp/guard-out" 2>&1; then
    echo "  FAIL  guard case $case was accepted" >&2
    exit 1
  fi
  case "$case" in
    mismatch) pattern="expected https://other.example:6443" ;;
    protected) pattern="is the protected server" ;;
    noconfirm) pattern="maas_confirm_deploy=true" ;;
  esac
  if ! grep -qF "$pattern" "$tmp/guard-out"; then
    echo "  FAIL  guard case $case did not report: $pattern" >&2
    cat "$tmp/guard-out" >&2
    exit 1
  fi
  echo "  ok  $case"
done
for playbook in playbooks/deploy.yml playbooks/teardown.yml playbooks/admin.yml \
                playbooks/sso-admin.yml playbooks/sync.yml playbooks/backup-now.yml \
                playbooks/restore-test.yml playbooks/first-plan.yml \
                playbooks/github-secret.yml playbooks/smoke-test.yml \
                playbooks/test-gateway.yml; do
  grep -q "tasks_from: preflight" "$playbook" || {
    echo "  FAIL  $playbook does not include the preflight" >&2
    exit 1
  }
  echo "  ok  $(basename "$playbook")"
done

echo "== envoy"
# A mistyped field otherwise shows up only as gateway pods that crash at start.
# Paths that exist only in the pod point at stand-in files under build/; the
# wasm filter is left out, because Envoy would try to load the module.
if command -v podman >/dev/null; then
  mkdir -p build
  openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=build-check \
    -keyout build/tls.key -out build/tls.crt >/dev/null 2>&1
  python3 - <<'PYEOF'
import yaml
from jinja2 import Template

VARS = yaml.safe_load(open("roles/maas_common/defaults/main.yml")) or {}
VARS.update({"maas_namespace": "build-check", "maas_apps_domain": "apps.build-check.example",
        "maas_gateway_drain_seconds": 120,
        "maas_governance_models": [{"name": "maas-sim", "publisher_id": "publishers/build-check/models/maas-sim"}],
        "maas_wasm_config_json": '{"services":{},"actionSets":[]}'})
PATHS = {"/etc/tls/tls.crt": "/cfg/tls.crt", "/etc/tls/tls.key": "/cfg/tls.key",
         "/etc/maas-ca/service-ca.crt": "/cfg/tls.crt",
         "/var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt": "/cfg/tls.crt"}

def strip_wasm(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "http_filters" and isinstance(v, list):
                node[k] = [f for f in v if f.get("name") != "envoy.filters.http.wasm"]
            strip_wasm(node[k])
    elif isinstance(node, list):
        for v in node:
            strip_wasm(v)

for manifest, cm, out in (("gateway.yaml", "maas-envoy-config", "gateway"), ("maas-sso.yaml", "maas-sso-proxy", "sso")):
    docs = yaml.safe_load_all(Template(open(f"manifests/{manifest}").read()).render(**VARS))
    data = next(d for d in docs if d and d.get("kind") == "ConfigMap" and d["metadata"]["name"] == cm)["data"]
    text = next(v for k, v in data.items() if k.endswith(".yaml"))
    for a, b in PATHS.items():
        text = text.replace(a, b)
    cfg = yaml.safe_load(text)
    strip_wasm(cfg)
    with open(f"build/{out}-envoy.yaml", "w") as f:
        yaml.safe_dump(cfg, f)
PYEOF
  sm_image="$(python3 -c 'import yaml; print(yaml.safe_load(open("roles/maas_common/defaults/main.yml"))["maas_image_envoy"])')"
  community="docker.io/envoyproxy/envoy@sha256:45876da26cbb8ef73ef86e985586f363a619eef9c0081a9c3a781cb4b3b9255e"
  envoy_image=""
  if podman pull -q "$sm_image" >/dev/null 2>&1; then
    envoy_image="$sm_image"; envoy_entry=/usr/local/bin/envoy
  elif podman pull -q "$community" >/dev/null 2>&1; then
    envoy_image="$community"; envoy_entry=envoy
    echo "  note   validating with the community Envoy build (no registry login)"
  fi
  if [ -n "$envoy_image" ]; then
    for c in gateway sso; do
      podman run --rm --entrypoint "$envoy_entry" -v "$PWD/build:/cfg:Z" \
        "$envoy_image" \
        --mode validate -c /cfg/$c-envoy.yaml >/dev/null 2>build/$c-envoy.log ||
        { echo "  FAIL  $c (see build/$c-envoy.log)" >&2; exit 1; }
      echo "  ok  $c"
    done
  else
    echo "  skipped: neither Envoy image can be pulled"
  fi
else
  echo "  skipped: podman not found"
fi

echo "== alert rules"
# The MaaS Prometheus's alerting rules, checked with promtool from the
# upstream image (pinned by its multi-arch digest).
if command -v podman >/dev/null; then
  mkdir -p build
  python3 - <<'PYEOF'
import yaml
from jinja2 import Template
docs = yaml.safe_load_all(Template(open("manifests/prometheus.yaml").read()).render(maas_namespace="build-check"))
cm = next(d for d in docs if d and d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "maas-prometheus-config")
open("build/alerts.yml", "w").write(cm["data"]["alerts.yml"])
PYEOF
  podman run --rm --entrypoint promtool -v "$PWD/build:/r:Z" \
    quay.io/prometheus/prometheus@sha256:63805ebb8d2b3920190daf1cb14a60871b16fd38bed42b857a3182bc621f4996 \
    check rules /r/alerts.yml >build/alerts.log 2>&1 ||
    { cat build/alerts.log >&2; exit 1; }
  echo "  ok  $(grep -o '[0-9]* rules found' build/alerts.log)"
else
  echo "  skipped: podman not found"
fi
