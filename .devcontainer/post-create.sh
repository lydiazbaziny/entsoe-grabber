#!/usr/bin/env bash
# Runs once, after the container is created.
set -euo pipefail

# The uv, pre-commit, Terraform and Claude Code directories are named volumes.
# Docker creates their mount points as root:root and Dev Containers does not
# chown named volumes, so without this the first `uv sync` fails with
# Permission denied -- and `claude` cannot write its credentials.
sudo chown -R "$(id -u):$(id -g)" \
  "$HOME/.cache/uv" "$HOME/.cache/pre-commit" "$HOME/.terraform.d" "$HOME/.claude" 2>/dev/null || true

# $HOME/.cache is not itself a volume -- Docker creates it as root:root simply
# because two mounts below it need it to exist. Anything else that writes there
# then cannot create its own directory: the gitleaks hook's `go install` fails
# with "failed to initialize build cache at /home/vscode/.cache/go-build".
sudo chown "$(id -u):$(id -g)" "$HOME/.cache" 2>/dev/null || true

# uv reads requires-python from pyproject.toml and downloads that exact
# interpreter if it is not already present. Nothing else pins the version.
echo "==> Installing the project's Python and dependencies with uv"
uv sync --all-extras

# Hooks live in .git/hooks, which is not part of the image, so this runs per
# container rather than per build. --install-hooks builds the upstream hook
# environments now -- they are cached on a named volume -- instead of stalling
# the first commit for several minutes.
echo "==> Installing the pre-commit hook"
if [ -d .git ]; then
  uv run pre-commit install --install-hooks ||
    echo "    (hook envs not built - run 'uv run pre-commit install --install-hooks')"
else
  echo "    (no .git directory - skipped)"
fi

echo "==> Warming Terraform provider cache"
mkdir -p "${TF_PLUGIN_CACHE_DIR:-$HOME/.terraform.d/plugin-cache}"
if [ -d infra ]; then
  terraform -chdir=infra init -backend=false -input=false >/dev/null ||
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
printf '    pre-commit: %s\n' "$(uv run pre-commit --version 2>&1)"
echo
echo "Ready. Try: make test | make build | make plan"
