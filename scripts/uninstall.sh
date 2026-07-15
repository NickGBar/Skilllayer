#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOVE_VENV=false
REMOVE_PROJECT_STATE=false
REMOVE_USER_DATA=false
DRY_RUN=false
CONFIRM=false

for arg in "$@"; do
  case "$arg" in
    --remove-venv) REMOVE_VENV=true ;;
    --remove-project-state) REMOVE_PROJECT_STATE=true ;;
    --remove-user-data) REMOVE_USER_DATA=true ;;
    --dry-run) DRY_RUN=true ;;
    --confirm) CONFIRM=true ;;
    --help)
      echo "Usage: scripts/uninstall.sh [--dry-run] [--confirm] [--remove-venv] [--remove-project-state] [--remove-user-data]"
      echo "The default operation only reports that MCP integration must be disabled; project memory is preserved."
      exit 0 ;;
    *) echo "error: unknown option: $arg" >&2; exit 2 ;;
  esac
done

echo "Disable integration: remove only the SkillLayer MCP entry from your client configuration."
echo "Project .skilllayer/ state is retained by default."
if "$DRY_RUN"; then
  echo "Dry run: no files changed."
  "$REMOVE_VENV" && echo "Would remove: $ROOT/.venv (only with --confirm)"
  "$REMOVE_PROJECT_STATE" && echo "Would remove: $ROOT/.skilllayer (only with --confirm)"
  "$REMOVE_USER_DATA" && echo "Would remove user-level SkillLayer state (only with --confirm)"
  exit 0
fi
if { "$REMOVE_VENV" || "$REMOVE_PROJECT_STATE" || "$REMOVE_USER_DATA"; } && ! "$CONFIRM"; then
  echo "Refusing destructive removal without --confirm." >&2
  exit 2
fi
if "$REMOVE_VENV"; then
  [[ -d "$ROOT/.venv" && -f "$ROOT/.venv/pyvenv.cfg" ]] || { echo "Refusing: .venv is not a recognizable environment." >&2; exit 2; }
  rm -rf -- "$ROOT/.venv"; echo "Removed: $ROOT/.venv"
fi
if "$REMOVE_PROJECT_STATE"; then rm -rf -- "$ROOT/.skilllayer"; echo "Removed: $ROOT/.skilllayer"; fi
if "$REMOVE_USER_DATA"; then rm -rf -- "${XDG_STATE_HOME:-$HOME/.local/state}/skilllayer"; echo "Removed user-level SkillLayer state."; fi
