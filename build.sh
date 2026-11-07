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

echo "== syntax"
cluster="$(ls clusters/*.yml | head -1)"
for playbook in playbooks/*.yml; do
  case "$playbook" in playbooks/_*) continue ;; esac
  ansible-playbook -i "$cluster" --syntax-check "$playbook" >/dev/null
  echo "  ok  $playbook"
done

echo "== manifests"
# The manifests are templates on the inventory vars; render them for a made-up
# namespace to catch broken Jinja or YAML before a deploy does.
"${python:-python3}" - <<'EOF'
import glob, sys, yaml
from jinja2 import Template

VARS = {"maas_namespace": "build-check", "maas_apps_domain": "apps.build-check.example",
        "maas_route_concurrent_tcp": 50, "maas_route_rate_http": 100,
        "maas_gateway_drain_seconds": 120, "maas_gateway_rate_per_second": 200, "maas_gateway_rate_burst": 400}
for path in sorted(glob.glob("manifests/*.yaml")):
    docs = [d for d in yaml.safe_load_all(Template(open(path).read()).render(**VARS)) if d]
    if not docs:
        sys.exit(f"{path}: renders to nothing")
    print(f"  ok  {path} ({len(docs)} objects)")
EOF

echo "== python files"
python3 -m py_compile roles/maas_portals/files/portal.py roles/maas_smoke/files/smoke.py \
  roles/maas_grafana/files/gen-dashboards.py roles/maas_admin/files/api.py maas-tui.py
echo "  ok"

echo "== unit tests"
out="$(python3 -m unittest discover -s tests 2>&1)" || { echo "$out" | grep -v '"POST /check' >&2; exit 1; }
echo "$out" | tail -1
find . -name __pycache__ -not -path './collections/*' -prune -exec rm -rf {} +
echo "  ok"

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

VARS = {"maas_namespace": "build-check", "maas_apps_domain": "apps.build-check.example",
        "maas_gateway_rate_per_second": 200, "maas_gateway_rate_burst": 400, "maas_gateway_drain_seconds": 120}
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
  for c in gateway sso; do
    podman run --rm --entrypoint envoy -v "$PWD/build:/cfg:Z" \
      docker.io/envoyproxy/envoy@sha256:45876da26cbb8ef73ef86e985586f363a619eef9c0081a9c3a781cb4b3b9255e \
      --mode validate -c /cfg/$c-envoy.yaml >/dev/null 2>build/$c-envoy.log ||
      { echo "  FAIL  $c (see build/$c-envoy.log)" >&2; exit 1; }
    echo "  ok  $c"
  done
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
