#!/usr/bin/env bash
# The change gate on four agent-made change sets. No model is involved: the commits are canned
# (each carries the co-author trailer Claude Code adds), while the gate runs for real — the tests
# are executed, every commit is scanned, receipts are sealed with a demo key and then checked.
#
#   examples/gate/run_demo.sh            # DEMO_PAUSE=1 slows it down for a recording
#
# Needs the `skilllayer` command (or SKILLLAYER_BIN=...) and a Python with pytest
# (DEMO_PYTHON=..., default: this checkout's .venv, then python3).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# The first install that actually has `gate`: an outdated or broken one earlier on PATH is skipped.
SKL=""
for candidate in "${SKILLLAYER_BIN:-}" "$(command -v skilllayer || true)" "$ROOT/.venv/bin/skilllayer"; do
  if [ -n "$candidate" ] && "$candidate" gate --help >/dev/null 2>&1; then SKL="$candidate"; break; fi
done
if [ -z "$SKL" ]; then echo "no working skilllayer with 'gate' found: install it or set SKILLLAYER_BIN" >&2; exit 2; fi
PY="${DEMO_PYTHON:-}"
[ -z "$PY" ] && [ -x "$ROOT/.venv/bin/python" ] && PY="$ROOT/.venv/bin/python"
[ -z "$PY" ] && PY="$(command -v python3 || true)"
if ! "$PY" -c "import pytest" 2>/dev/null; then echo "needs a Python with pytest: set DEMO_PYTHON" >&2; exit 2; fi
absolute() { case "$1" in /*) echo "$1" ;; */*) echo "$(cd "$(dirname "$1")" && pwd)/$(basename "$1")" ;; *) command -v "$1" ;; esac; }
SKL="$(absolute "$SKL")"  # the gate runs from inside the demo repository
PY="$(absolute "$PY")"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export SKILLLAYER_VERIFY_DIR="$WORK/verify-data"
RECEIPTS="$WORK/receipts"
REPO="$WORK/shop"
mkdir -p "$REPO/src" "$REPO/tests" "$REPO/migrations"
cat > "$REPO/src/promo.py" <<'PY'
def apply_promo(total, code):
    """SAVE10 takes 10% off; anything else changes nothing."""
    if code == "SAVE10":
        return round(total * 0.9, 2)
    return total
PY
cat > "$REPO/tests/test_promo.py" <<'PY'
from src.promo import apply_promo


def test_save10_takes_ten_percent_off():
    assert apply_promo(100, "SAVE10") == 90


def test_unknown_code_changes_nothing():
    assert apply_promo(100, "BOGUS") == 100
PY
printf '[pytest]\npythonpath = .\n' > "$REPO/pytest.ini"
printf '__pycache__/\n' > "$REPO/.gitignore"
printf 'create table orders (id int, total numeric);\n' > "$REPO/migrations/001_orders.sql"
printf 'version: 1\nprotected_paths:\n  - migrations/\n' > "$REPO/.skilllayer-policy.yml"
git -C "$REPO" init -q -b main
git -C "$REPO" config user.email dev@example.com
git -C "$REPO" config user.name dev
git -C "$REPO" add -A
git -C "$REPO" commit -qm "shop"

if [ -t 1 ]; then B=$'\033[1m'; D=$'\033[2m'; R=$'\033[0m'; else B=""; D=""; R=""; fi
pause() { [ "${DEMO_PAUSE:-0}" != "0" ] && sleep "${DEMO_PAUSE}"; return 0; }
act() { printf '\n%s== %s ==%s\n' "$B" "$1" "$R"; pause; }
agent() { printf '%sagent>%s %s\n' "$D" "$R" "$1"; pause; }
new_branch() { git -C "$REPO" switch -q main; git -C "$REPO" switch -q -c "$1"; }
agent_commit() {
  git -C "$REPO" add -A
  git -C "$REPO" commit -qm "$1" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
}
# The demo key is not a secret; in CI it is a masked variable the gate takes out of the
# environment before it runs any of the project's code.
export SKILLLAYER_RECEIPT_KEY="demo-key" SKILLLAYER_RECEIPT_KEY_ID="demo"
gate() {
  printf '%s$ skilllayer gate --base main%s\n' "$D" "$R"
  (cd "$REPO" && "$SKL" gate --base main --test-command "$PY -m pytest -q -p no:cacheprovider" \
      --receipt-dir "$RECEIPTS" --event-log "$RECEIPTS/events.jsonl" "$@")
  printf '%s(exit %s: %s)%s\n' "$B" "$?" "0 accepted, 2 blocked, 3 unverified" "$R"
  pause
}

act "1. The agent does the task properly"
new_branch agent/free-shipping
cat >> "$REPO/src/promo.py" <<'PY'


def shipping(total):
    return 0 if total >= 50 else 5
PY
cat >> "$REPO/tests/test_promo.py" <<'PY'


def test_free_shipping_from_50():
    from src.promo import shipping
    assert shipping(50) == 0 and shipping(49) == 5
PY
agent "Added free shipping from 50 with a test."
agent_commit "feat: free shipping from 50"
gate

act "2. The agent says the tests pass. They do not."
new_branch agent/promo-fix
sed 's/0\.9/0.8/' "$REPO/src/promo.py" > "$REPO/src/promo.py.new" && mv "$REPO/src/promo.py.new" "$REPO/src/promo.py"
agent "Done: SAVE10 fixed, all tests pass."
agent_commit "fix: SAVE10 (all tests pass)"
gate

act "3. The agent changes a migration, and unprotects migrations/ in the same commit"
new_branch agent/index
printf 'create index orders_total on orders(total);\n' > "$REPO/migrations/002_index.sql"
printf 'version: 1\nprotected_paths: []\n' > "$REPO/.skilllayer-policy.yml"
agent "Added an index migration. Also tidied the policy file."
agent_commit "perf: index orders.total"
gate

act "4. The agent commits a key, then deletes it in the next commit"
new_branch agent/payments
KEY="AKIA""IOSFODNN7EXAMPLE"  # the documented AWS example key, split so this file never holds it whole
printf 'PAYMENTS_KEY = "%s"\n' "$KEY" > "$REPO/src/settings.py"
agent_commit "feat: payments settings"
rm "$REPO/src/settings.py"
agent "Payments wired up. (Removed the settings file again, nothing to see.)"
agent_commit "chore: remove settings"
gate

act "5. The receipts: the evidence each verdict rests on"
ls -1 "$RECEIPTS" | sed 's/^/  /'
first="$(ls "$RECEIPTS"/*-VERIFIED.json | head -1)"
printf '%s$ skilllayer gate --verify-receipt %s%s\n' "$D" "$(basename "$first")" "$R"
"$SKL" gate --verify-receipt "$first"
"$PY" - "$first" <<'PY'
import json, sys
path = sys.argv[1]
receipt = json.load(open(path))
receipt["verdict"] = "VERIFIED"
receipt["checks"][-1]["status"] = "passed"
receipt["reasons"] = []
receipt["head"]["sha"] = "0" * 40  # pretend a different commit was the one that passed
json.dump(receipt, open(path, "w"))
PY
printf '%s(someone edits the receipt to vouch for a different commit)%s\n' "$D" "$R"
"$SKL" gate --verify-receipt "$first"
printf '\n  one line per run for the SIEM (%s):\n' "events.jsonl"
"$PY" - "$RECEIPTS/events.jsonl" <<'PY'
import json, sys
for line in open(sys.argv[1]):
    e = json.loads(line)
    print(f"  {e['verdict']:<10} head {e['head'][:10]}  ai-assisted commits {e['ai_assisted_commits']}  {', '.join(e['reasons']) or '-'}")
PY
