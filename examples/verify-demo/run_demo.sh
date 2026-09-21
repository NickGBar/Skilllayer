#!/usr/bin/env bash
# A scripted "agent" walks through what the skilllayer Stop hook does. No model and no Claude
# Code are involved: the edits are canned, the hook calls are real (the same commands the
# plugin runs), and every verdict comes from actually running the tests in a scratch copy.
#
#   examples/verify-demo/run_demo.sh            # DEMO_PAUSE=1 slows it down for a recording
#
# Needs the `skilllayer` command (or SKILLLAYER_BIN=/path/to/skilllayer).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SKL="${SKILLLAYER_BIN:-}"
[ -z "$SKL" ] && SKL="$(command -v skilllayer || true)"
[ -z "$SKL" ] && [ -x "$ROOT/.venv/bin/skilllayer" ] && SKL="$ROOT/.venv/bin/skilllayer"
if [ -z "$SKL" ]; then echo "skilllayer not found: install it or set SKILLLAYER_BIN" >&2; exit 2; fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export SKILLLAYER_VERIFY_DIR="$WORK/verify-data"
REPO="$WORK/cart-demo"
cp -R "$HERE/template" "$REPO"
git -C "$REPO" init -q
git -C "$REPO" config user.email demo@example.com
git -C "$REPO" config user.name demo
git -C "$REPO" add -A
git -C "$REPO" commit -qm "cart demo"

if [ -t 1 ]; then B=$'\033[1m'; D=$'\033[2m'; R=$'\033[0m'; else B=""; D=""; R=""; fi
pause() { [ "${DEMO_PAUSE:-0}" != "0" ] && sleep "${DEMO_PAUSE}"; return 0; }
act() { printf '\n%s== %s ==%s\n' "$B" "$1" "$R"; pause; }
agent() { printf '%sagent>%s %s\n' "$D" "$R" "$1"; pause; }
edit() { printf '%s      (edits %s)%s\n' "$D" "$1" "$R"; }
reset_repo() { git -C "$REPO" reset -q --hard HEAD; git -C "$REPO" clean -qfd; }

turn_start() {
  printf '{"session_id":"%s","cwd":"%s"}' "$1" "$REPO" | "$SKL" verify --hook prompt
}

hook_stop() {  # session, stop_hook_active
  local out="$WORK/out.txt" err="$WORK/err.txt" code
  printf '{"session_id":"%s","cwd":"%s","stop_hook_active":%s}' "$1" "$REPO" "${2:-false}" | "$SKL" verify --hook stop >"$out" 2>"$err"
  code=$?
  python3 - "$out" "$err" "$code" "$B" "$R" <<'PY'
import json, sys
out_path, err_path, code, bold, reset = sys.argv[1:6]
text = open(out_path).read().strip()
data = json.loads(text) if text else {}
blocked = data.get("decision") == "block" or code == "2"
label = "blocked: the agent is sent back to work" if blocked else ("allowed" if code == "0" else f"error (exit {code})")
print(f"{bold}STOP hook -> {label}{reset}")
if blocked:
    print("    what the agent receives:")
    for line in (data.get("reason") or open(err_path).read()).strip().splitlines():
        print("    | " + line)
if data.get("systemMessage"):
    print("    what you see: " + data["systemMessage"])
PY
  pause
  return 0
}

echo "skilllayer verify — demo repo: a cart module whose last test fails ('a discount over 100% must not give a negative total')."

act "Act 1: \"Done, all tests pass!\" — but they don't"
turn_start demo-1
agent "I'll make the total non-negative by taking the absolute value."
edit src/cart.py
python3 - "$REPO/src/cart.py" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
open(p, "w").write(s.replace("return round(subtotal(items) * (1 - discount_pct / 100), 2)", "return round(abs(subtotal(items) * (1 - discount_pct / 100)), 2)"))
PY
agent "Done ✅  All tests pass."
hook_stop demo-1
agent "Running the tests myself... it returns 5.0, not 0.0. Clamping at zero instead."
edit src/cart.py
python3 - "$REPO/src/cart.py" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
open(p, "w").write(s.replace("round(abs(subtotal(items) * (1 - discount_pct / 100)), 2)", "round(max(subtotal(items) * (1 - discount_pct / 100), 0), 2)"))
PY
agent "Done."
hook_stop demo-1 true

act "Act 2: making the test pass by changing the test"
reset_repo
turn_start demo-2
agent "The test expects 0.0 but the code returns -5.0. I'll update the expectation."
edit tests/test_cart.py
python3 - "$REPO/tests/test_cart.py" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
open(p, "w").write(s.replace("discount_pct=150) == 0.0", "discount_pct=150) == -5.0"))
PY
agent "Done ✅  All tests pass."
hook_stop demo-2

act "Act 3: tests pass, but it touched what it must not"
reset_repo
turn_start demo-3
agent "Fixing the total, and while I'm here tidying the migration."
edit src/cart.py
python3 - "$REPO/src/cart.py" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
open(p, "w").write(s.replace("return round(subtotal(items) * (1 - discount_pct / 100), 2)", "return round(max(subtotal(items) * (1 - discount_pct / 100), 0), 2)"))
PY
edit migrations/001_init.sql
printf 'drop table orders;\n' >> "$REPO/migrations/001_init.sql"
agent "Done ✅  Tests pass and I cleaned up the schema."
hook_stop demo-3
agent "Reverting the migration change."
git -C "$REPO" checkout -q -- migrations/001_init.sql
hook_stop demo-3 true

act "What was recorded (local files only; nothing leaves this machine)"
"$SKL" verify --repo "$REPO" --stats
