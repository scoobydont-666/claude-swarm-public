#!/usr/bin/env bash
# render-swarm-config.sh — one-off dev-machine render of config/swarm.yaml.
#
# For fleet hosts, prefer the Ansible role:
#     ansible-playbook -i inventory site.yml --tags claude_swarm_config
#
# This script copies swarm.yaml.example to swarm.yaml and substitutes a minimal
# set of env-driven values. Use for local dev where Ansible is overkill.

set -euo pipefail

CS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${CS_ROOT}/config/swarm.yaml.example"
DST="${CS_ROOT}/config/swarm.yaml"

if [[ -f "${DST}" ]]; then
    echo "==> ${DST} exists — not overwriting. Backup + remove first if you want to re-render."
    exit 0
fi

if [[ ! -f "${SRC}" ]]; then
    echo "FATAL: ${SRC} not found" >&2
    exit 1
fi

# Load .env if present
if [[ -f "${CS_ROOT}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${CS_ROOT}/.env"
    set +a
fi

# Resolve fleet IPs from env (fall back to localhost for dev)
export FLEET_MINIBOSS_IP="${FLEET_MINIBOSS_IP:-127.0.0.1}"
export FLEET_GIGA_IP="${FLEET_GIGA_IP:-127.0.0.1}"
export FLEET_MECHA_IP="${FLEET_MECHA_IP:-127.0.0.1}"
export FLEET_MEGA_IP="${FLEET_MEGA_IP:-127.0.0.1}"
export FLEET_MONGO_IP="${FLEET_MONGO_IP:-127.0.0.1}"
export SWARM_EMAIL_ALERTS="${SWARM_EMAIL_ALERTS:-ops@example.com}"

cp "${SRC}" "${DST}"
chmod 0640 "${DST}"

echo "==> Rendered ${DST} from example."
echo "==> Fleet IPs fell back to 127.0.0.1. For prod, run the Ansible role instead."
