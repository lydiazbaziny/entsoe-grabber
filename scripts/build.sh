#!/usr/bin/env bash
# Build the Lambda deployment package: runtime dependencies plus application
# source. Dependencies are packaged rather than taken from the runtime, so the
# function pins its own versions instead of drifting when AWS updates the
# runtime's bundled SDK.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT}/build"
FUNCTION_DIR="${BUILD_DIR}/function"

# Must match var.lambda_architecture: wheels are resolved for the target, not
# for the machine running this script.
ARCHITECTURE="${LAMBDA_ARCHITECTURE:-arm64}"
case "${ARCHITECTURE}" in
arm64) PLATFORM="aarch64-manylinux2014" ;;
x86_64) PLATFORM="x86_64-manylinux2014" ;;
*)
  echo "Unsupported LAMBDA_ARCHITECTURE: ${ARCHITECTURE}" >&2
  exit 1
  ;;
esac

# Must match the runtime in infra/app/lambda.tf.
PYTHON_VERSION="3.14"

rm -rf "${BUILD_DIR}"
mkdir -p "${FUNCTION_DIR}"

uv export --frozen --no-dev --no-emit-project \
  --format requirements.txt -o "${BUILD_DIR}/requirements.txt" --quiet

# --no-installer-metadata drops RECORD/INSTALLER files, which carry paths and
# would otherwise vary between builds.
uv pip install --target "${FUNCTION_DIR}" \
  --requirements "${BUILD_DIR}/requirements.txt" \
  --python-platform "${PLATFORM}" \
  --python-version "${PYTHON_VERSION}" \
  --no-installer-metadata --quiet

# Console scripts and uv's target lock are not importable code.
rm -rf "${FUNCTION_DIR:?}/bin" "${FUNCTION_DIR:?}/.lock"

cp -R "${ROOT}/src/entsoe_grabber" "${FUNCTION_DIR}/"
find "${FUNCTION_DIR}" -name '__pycache__' -type d -prune \
  -exec rm -rf {} + 2>/dev/null || true

# Fix mtimes and sort entries so unchanged source produces the same zip hash.
(
  cd "${FUNCTION_DIR}"
  find . -exec touch -t 200001010000.00 {} +
  find . -type f | sort | zip -q -X "${BUILD_DIR}/function.zip" -@
)

echo "Built ${BUILD_DIR}/function.zip ($(du -h "${BUILD_DIR}/function.zip" | cut -f1), ${ARCHITECTURE})"
