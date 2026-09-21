#!/bin/sh
# Claude Code hook entry point for `skilllayer verify`.  Usage: skilllayer-hook.sh prompt|stop
#
# The binary is looked up in a few places because hooks often start with a smaller PATH
# than the user's shell (GUI launches). If it cannot be found the Stop hook says so, loudly
# and without blocking: a missing verifier must never look like a passed verification.
#
# The 540 s test budget stays under the 600 s hook timeout in hooks.json, so a slow test run
# ends as a visible UNVERIFIED_TIMEOUT instead of the harness killing the hook and letting
# the stop through unnoticed.
#
# SKILLLAYER_TEST_COMMAND (optional) replaces test auto-detection, e.g. "make test". It is read
# from the caller's environment on purpose — never from a file inside the repository being
# judged, which would let a cloned repo choose what runs.
mode="$1"
for candidate in "${SKILLLAYER_BIN:-}" "$(command -v skilllayer 2>/dev/null)" "$HOME/.local/bin/skilllayer"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    if [ -n "${SKILLLAYER_TEST_COMMAND:-}" ]; then
      exec "$candidate" verify --hook "$mode" --max-seconds 540 --test-command "$SKILLLAYER_TEST_COMMAND"
    fi
    exec "$candidate" verify --hook "$mode" --max-seconds 540
  fi
done
if [ "$mode" = "stop" ]; then
  printf '%s\n' '{"systemMessage": "skilllayer: the verify plugin is enabled but the `skilllayer` command was not found, so this work was NOT verified. Install the skilllayer package (see the plugin README) or set SKILLLAYER_BIN."}'
fi
exit 0
