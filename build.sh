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
  ansible-playbook -i "$cluster" --syntax-check "$playbook" >/dev/null
  echo "  ok  $playbook"
done
