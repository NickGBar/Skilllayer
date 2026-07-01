#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${REPO_ROOT}/.venv/bin/python"
else
  PYTHON="$(command -v python3 || command -v python)"
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

run_check "tester-check" "${PYTHON}" -m skilllayer tester-check
run_check "workflows" "${PYTHON}" -m skilllayer workflows
run_check "doctor" "${PYTHON}" -m skilllayer doctor

echo
echo "Verification passed."
