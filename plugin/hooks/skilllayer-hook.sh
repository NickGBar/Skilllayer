#!/bin/sh
# Claude Code hook entry point for `skilllayer verify`.  Usage: skilllayer-hook.sh prompt|stop
#
# The binary is looked up in a few places because hooks often start with a smaller PATH
# than the user's shell (GUI launches): $SKILLLAYER_BIN, every absolute directory on PATH,
# then ~/.local/bin. Relative PATH entries are skipped — they would resolve inside the
# repository being judged. If nothing usable is found the Stop hook says so, loudly and
# without blocking: a missing verifier must never look like a passed verification.
#
# Each candidate must actually run `verify` before it is used. A stale install (an editable
# install whose checkout is gone, or a version from before `verify`) would otherwise either
# crash — which Claude Code shows only as a generic "Stop hook error" — or exit 2 from its
# argument parser, which Claude Code would take as "block this stop".
#
# The 540 s test budget stays under the 600 s hook timeout in hooks.json, so a slow test run
# ends as a visible UNVERIFIED_TIMEOUT instead of the harness killing the hook and letting
# the stop through unnoticed.
#
# SKILLLAYER_TEST_COMMAND (optional) replaces test auto-detection, e.g. "make test". It is read
# from the caller's environment on purpose — never from a file inside the repository being
# judged, which would let a cloned repo choose what runs.
mode="$1"
unusable=""

try() {
  [ -n "$1" ] && [ -x "$1" ] || return 0
  if ! "$1" verify --help </dev/null >/dev/null 2>&1; then
    unusable="$1"
    return 0
  fi
  if [ -n "${SKILLLAYER_TEST_COMMAND:-}" ]; then
    exec "$1" verify --hook "$mode" --max-seconds 540 --test-command "$SKILLLAYER_TEST_COMMAND"
  fi
  exec "$1" verify --hook "$mode" --max-seconds 540
}

try "${SKILLLAYER_BIN:-}"
saved_ifs="$IFS"
IFS=:
for dir in $PATH; do
  IFS="$saved_ifs"
  case "$dir" in
    /*) try "$dir/skilllayer" ;;
  esac
  IFS=:
done
IFS="$saved_ifs"
try "$HOME/.local/bin/skilllayer"

if [ "$mode" = "stop" ]; then
  if [ -n "$unusable" ]; then
    printf '%s\n' '{"systemMessage": "skilllayer: the `skilllayer` command on this machine is broken or too old to run `verify`, so this work was NOT verified. Reinstall it (see the plugin README) or set SKILLLAYER_BIN."}'
  else
    printf '%s\n' '{"systemMessage": "skilllayer: the verify plugin is enabled but the `skilllayer` command was not found, so this work was NOT verified. Install the skilllayer package (see the plugin README) or set SKILLLAYER_BIN."}'
  fi
fi
exit 0
