#!/usr/bin/env bash
# Runs once, after the container is created.
set -euo pipefail

# The uv, Terraform and Claude Code directories are named volumes. Docker
# creates their mount points as root:root and Dev Containers does not chown
# named volumes, so without this the first `uv sync` fails with Permission
# denied -- and `claude` cannot write its credentials.
sudo chown -R "$(id -u):$(id -g)" \
  "$HOME/.cache/uv" "$HOME/.terraform.d" "$HOME/.claude" 2>/dev/null || true

# uv reads requires-python from pyproject.toml and downloads that exact
# interpreter if it is not already present. Nothing else pins the version.
echo "==> Installing the project's Python and dependencies with uv"
uv sync --all-extras

echo "==> Warming Terraform provider cache"
mkdir -p "${TF_PLUGIN_CACHE_DIR:-$HOME/.terraform.d/plugin-cache}"
if [ -d infra ]; then
  terraform -chdir=infra init -backend=false -input=false >/dev/null || \
    echo "    (terraform init skipped - run 'make tf-init' once credentials are set)"
fi

echo
echo "==> Toolchain"
printf '    python    : %s\n' "$(uv run python --version 2>&1)"
printf '    uv        : %s\n' "$(uv --version 2>&1)"
printf '    terraform : %s\n' "$(terraform version | head -1)"
printf '    aws       : %s\n' "$(aws --version 2>&1)"
printf '    node      : %s\n' "$(node --version 2>&1)"
printf '    claude    : %s\n' "$(claude --version 2>&1)"
echo
echo "Ready. Try: make test | make build | make plan"
