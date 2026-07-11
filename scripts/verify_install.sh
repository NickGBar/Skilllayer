#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

PYTHON="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  echo "Verification failed: ${PYTHON} is missing. Run scripts/install.sh first." >&2
  exit 1
fi

echo "SkillLayer install verification"
echo "  repo: ${REPO_ROOT}"
echo "  python: ${PYTHON}"

run_check() {
  local label="$1"
  shift
  echo
  echo "== ${label} =="
  "$@"
}

run_check "import path" "${PYTHON}" -c 'import skilllayer; print(skilllayer.__file__)'
run_check "workflows" "${PYTHON}" -m skilllayer workflows
run_check "doctor" "${PYTHON}" -m skilllayer doctor

echo
echo "Verification passed."
