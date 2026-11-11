#!/usr/bin/env bash
# One command, end to end:  ./deploy.sh            (same as: ./deploy.sh deploy)
# Single stage:             ./deploy.sh preflight | dns | render | iso | install | dns-delete
# Extra flags pass through: ./deploy.sh deploy --dry-run
set -euo pipefail
cd "$(dirname "$0")"

[[ -f .env ]] || { echo "ERROR: .env missing - cp .env.example .env and fill it in" >&2; exit 1; }
[[ -f config/cluster.yaml ]] || { echo "ERROR: config/cluster.yaml missing - cp config/cluster.example.yaml config/cluster.yaml" >&2; exit 1; }

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

[[ $# -eq 0 ]] && set -- deploy
exec .venv/bin/python -m ocpdeploy "$@"
