#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"
EXPLICIT_PYTHON=""
PREFLIGHT_ONLY=false
SELECTED_PYTHON=""
SELECTED_VERSION=""
INITIAL_ARTIFACTS=""
INSTALL_TMPDIR=""

usage() {
  cat <<'EOF'
Usage: scripts/install.sh [--python <executable>] [--preflight]

--python <executable>  Use this supported Python interpreter explicitly.
--preflight            Validate destination and interpreter selection without writing files.
EOF
}

fail() {
  echo "INSTALL FAILED" >&2
  echo "Reason: $1" >&2
  exit 1
}

is_supported_version() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

candidate_path() {
  local candidate="$1"
  if [[ "$candidate" == */* ]]; then
    [[ -f "$candidate" && -x "$candidate" ]] && printf '%s\n' "$candidate"
  else
    command -v "$candidate" 2>/dev/null || true
  fi
}

select_candidate() {
  local candidate="$1" path details
  path="$(candidate_path "$candidate")"
  [[ -n "$path" ]] || return 1
  is_supported_version "$path" || return 1
  details="$("$path" -c 'import os, sys; print(os.path.realpath(sys.executable)); print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')" || return 1
  SELECTED_PYTHON="$(printf '%s\n' "$details" | sed -n '1p')"
  SELECTED_VERSION="$(printf '%s\n' "$details" | sed -n '2p')"
  [[ -n "$SELECTED_PYTHON" && -n "$SELECTED_VERSION" ]]
}

select_python() {
  if [[ -n "$EXPLICIT_PYTHON" ]]; then
    select_candidate "$EXPLICIT_PYTHON" || fail "--python must name an executable Python 3.10 or newer: $EXPLICIT_PYTHON"
    return
  fi

  if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
    if select_candidate "${VIRTUAL_ENV}/bin/python"; then
      return
    fi
    echo "warning: active virtual environment Python is unsupported; searching for another supported Python." >&2
  fi

  local candidate
  for candidate in python3.13 python3.12 python3.11 python3.10; do
    if select_candidate "$candidate"; then
      return
    fi
  done
  if select_candidate python3; then
    return
  fi

  fail "No supported Python interpreter was found. SkillLayer requires Python 3.10 or newer. Re-run with --python /path/to/python after installing or locating one."
}

validate_destination() {
  case "$REPO_ROOT" in
    *:*)
      fail "Installation path contains ':' and is unsupported by the selected tooling: $REPO_ROOT. Move or clone SkillLayer to a path without ':' (for example, $HOME/SkillLayer), confirm that destination yourself, then rerun. No files were changed."
      ;;
    *$'\n'*|*$'\r'*)
      fail "Installation path contains a newline and is unsupported: $REPO_ROOT. Choose a normal path and rerun. No files were changed."
      ;;
  esac
}

artifact_paths() {
  local path
  for path in "$REPO_ROOT/build" "$REPO_ROOT/dist"; do
    [[ -e "$path" ]] && printf '%s\n' "$path"
  done
  if [[ -d "$REPO_ROOT/src" ]]; then
    find "$REPO_ROOT/src" -type d -name '*.egg-info' -print 2>/dev/null
  fi
}

cleanup_installer_artifacts() {
  local path
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    if ! grep -Fqx -- "$path" <<< "$INITIAL_ARTIFACTS"; then
      case "$path" in
        "$REPO_ROOT"/build|"$REPO_ROOT"/dist|"$REPO_ROOT"/src/*.egg-info)
          rm -rf -- "$path"
          echo "Removed installer-created artifact: ${path#"$REPO_ROOT"/}"
          ;;
      esac
    fi
  done < <(artifact_paths)
}

cleanup() {
  [[ -n "$INSTALL_TMPDIR" && -d "$INSTALL_TMPDIR" ]] && rm -rf -- "$INSTALL_TMPDIR"
  cleanup_installer_artifacts
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      [[ $# -ge 2 ]] || fail "--python requires an executable path."
      EXPLICIT_PYTHON="$2"
      shift 2
      ;;
    --preflight) PREFLIGHT_ONLY=true; shift ;;
    --help) usage; exit 0 ;;
    *) fail "Unknown option: $1. Run scripts/install.sh --help." ;;
  esac
done

cd "$REPO_ROOT"

echo "SkillLayer installer"
echo "  repo: ${REPO_ROOT}"
echo "  sudo: not required"

[[ -f "pyproject.toml" ]] || fail "pyproject.toml not found. Run this script from a SkillLayer checkout."

select_python
validate_destination

echo "  selected executable: ${SELECTED_PYTHON}"
echo "  resolved path: ${SELECTED_PYTHON}"
echo "  Python version: ${SELECTED_VERSION}"
echo "  installation target: ${VENV_DIR}"

if "$PREFLIGHT_ONLY"; then
  echo "Preflight passed. No files were created or modified."
  exit 0
fi

if [[ -e "$VENV_DIR" && ! -d "$VENV_DIR" ]]; then
  fail ".venv exists but is not a directory. Refusing to overwrite it."
fi

INITIAL_ARTIFACTS="$(artifact_paths || true)"
INSTALL_TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/skilllayer-install.XXXXXX")"
trap cleanup EXIT

if [[ ! -d "$VENV_DIR" ]]; then
  echo "Creating virtual environment at .venv"
  "$SELECTED_PYTHON" -m venv "$VENV_DIR"
else
  echo "Using existing virtual environment at .venv"
fi

PYTHON="$VENV_DIR/bin/python"
[[ -x "$PYTHON" ]] || fail "$PYTHON is not executable. Recreate .venv and try again."

echo "Upgrading pip, setuptools, and wheel inside .venv"
if ! TMPDIR="$INSTALL_TMPDIR" PIP_CACHE_DIR="$INSTALL_TMPDIR/pip-cache" "$PYTHON" -m pip install --upgrade pip setuptools wheel; then
  fail "failed to upgrade pip, setuptools, and wheel."
fi

echo "Installing SkillLayer with the required MCP runtime extra"
if ! TMPDIR="$INSTALL_TMPDIR" PIP_CACHE_DIR="$INSTALL_TMPDIR/pip-cache" "$PYTHON" -m pip install ".[mcp]" --no-build-isolation; then
  fail "failed to install the required MCP runtime extra."
fi

echo "Running installed-runtime verification"
if ! "$PYTHON" -m skilllayer doctor --json; then
  fail "doctor failed after installation."
fi

echo
echo "SkillLayer install validation completed."
echo "Next steps:"
echo "  ${PYTHON} -m skilllayer doctor --json"
echo "  ${PYTHON} -m skilllayer workflows --json"
echo "  ${PYTHON} -m skilllayer mcp-config --output skilllayer-mcp.json"
