#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOVE_VENV=false
REMOVE_PROJECT_STATE=false
REMOVE_USER_DATA=false

for arg in "$@"; do
  case "$arg" in
    --remove-venv) REMOVE_VENV=true ;;
    --remove-project-state) REMOVE_PROJECT_STATE=true ;;
    --remove-user-data) REMOVE_USER_DATA=true ;;
    --help)
      echo "Usage: scripts/uninstall.sh [--remove-venv] [--remove-project-state] [--remove-user-data]"
      echo "Disable MCP separately by removing the skilllayer entry from your client configuration."
      exit 0 ;;
    *) echo "error: unknown option: $arg" >&2; exit 2 ;;
  esac
done

echo "Disable integration: remove mcpServers.skilllayer from your MCP client configuration."
if "$REMOVE_VENV"; then rm -rf "$ROOT/.venv"; echo "Removed: $ROOT/.venv"; fi
if "$REMOVE_PROJECT_STATE"; then rm -rf "$ROOT/.skilllayer"; echo "Removed: $ROOT/.skilllayer"; fi
if "$REMOVE_USER_DATA"; then rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/skilllayer"; echo "Removed user-level SkillLayer state."; fi
if ! "$REMOVE_PROJECT_STATE"; then echo "Project .skilllayer/ state was retained."; fi
