#!/usr/bin/env python3
"""Check the skilllayer-verify plugin against a real Claude Code binary.

The Anthropic API is replaced by a scripted local stand-in, so no credentials, network or
model are involved. What is being tested is the harness side of the hook contract: that the
plugin's hooks run, that exit code 2 keeps the agent working and hands it the observed
facts, what the Stop payload contains, and how the harness treats a missing binary or a
hook that outlives its timeout.

Nothing touches your real Claude Code configuration: HOME and CLAUDE_CONFIG_DIR point at a
temporary directory, where the plugin is installed from this checkout.

    python scripts/e2e_claude_code_hook.py [--claude PATH] [--keep]

The Claude Code binary comes from --claude, then $CLAUDE_BIN, then `claude` on PATH. This is
not part of the pytest suite: it needs that binary and takes about half a minute.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BROKEN = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b  # fixed\n"


# --------------------------------------------------------------------------- scripted API


def _sse(events) -> bytes:
    return b"".join(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events)


def _turn(blocks, stop_reason):
    return [
        ("message_start", {"type": "message_start", "message": {"id": "msg_mock", "type": "message", "role": "assistant", "model": "claude-mock", "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}}),
        *blocks,
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 8}}),
        ("message_stop", {"type": "message_stop"}),
    ]


def text_turn(text):
    return _turn([
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ], "end_turn")


_tool_ids = itertools.count(1)


def tool_turn(name, tool_input):
    return _turn([
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": f"toolu_mock{next(_tool_ids)}", "name": name, "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool_input)}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ], "tool_use")


def write_file(rel: str, content: str):
    """A Bash tool call that overwrites a file (the fake agent's 'edit')."""
    command = "printf '%b' '" + content.replace("\n", "\\n") + f"' > {rel}"
    return tool_turn("Bash", {"command": command, "description": "edit"})


class ScriptedApi:
    """Plays a fixed list of assistant turns for the main conversation and records every request."""

    def __init__(self, turns):
        self.turns = turns
        self.main_requests: list[dict] = []
        self._lock = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, body: bytes, content_type="application/json"):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send(b"{}")

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                if not self.path.startswith("/v1/messages"):
                    return self._send(b"{}")
                if body.get("tools"):  # the agent conversation; other calls are housekeeping
                    with api._lock:
                        index = len(api.main_requests)
                        api.main_requests.append(body)
                    events = api.turns[index]() if index < len(api.turns) else text_turn("(script exhausted)")
                else:
                    events = text_turn("ok")
                if body.get("stream"):
                    return self._send(_sse(events), "text/event-stream")
                return self._send(json.dumps({"id": "msg_mock", "type": "message", "role": "assistant", "model": "claude-mock", "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 2}}).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def mentions(self, index: int, needle: str) -> bool:
        return needle in json.dumps(self.main_requests[index], ensure_ascii=False)

    def close(self):
        self.server.shutdown()


# --------------------------------------------------------------------------- environment


class Sandbox:
    def __init__(self, claude: str, tmp: Path):
        self.claude, self.tmp = claude, tmp
        self.home = tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.hook_log = tmp / "hook_calls.log"
        self._write_skilllayer_wrapper()

    def env(self, **extra) -> dict[str, str]:
        return {
            "PATH": os.pathsep.join([str(self.bin), "/usr/bin", "/bin"]),
            "HOME": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "ANTHROPIC_API_KEY": "sk-ant-mock-not-a-real-key",
            "SKILLLAYER_VERIFY_DIR": str(self.tmp / "verify-data"),
            "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "TERM": "dumb", "LANG": "en_US.UTF-8",
            **extra,
        }

    def _write_skilllayer_wrapper(self) -> None:
        """`skilllayer` on PATH: records the hook payload and exit code, then runs this checkout."""
        real = self.tmp / "skilllayer-real"
        real.write_text(f'#!/bin/sh\nPYTHONPATH="{ROOT / "src"}${{PYTHONPATH:+:$PYTHONPATH}}" exec "{sys.executable}" -m skilllayer "$@"\n')
        real.chmod(0o755)
        wrapper = self.bin / "skilllayer"
        wrapper.write_text(
            "#!/bin/sh\n"
            't="$(mktemp)"; o="$(mktemp)"\ncat > "$t"\n'
            f'{{ echo "ARGS: $*"; cat "$t"; echo; }} >> "{self.hook_log}"\n'
            f'"{real}" "$@" < "$t" > "$o"\ncode=$?\ncat "$o"\n'
            f'{{ printf \'STDOUT: %s\\n\' "$(cat "$o")"; echo "EXIT: $code"; }} >> "{self.hook_log}"\nrm -f "$t" "$o"\nexit $code\n'
        )
        wrapper.chmod(0o755)

    def run_claude(self, *args: str, env=None, cwd=None, timeout=120) -> subprocess.CompletedProcess:
        return subprocess.run([self.claude, *args], env=env or self.env(), cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def install_plugin(self) -> None:
        for args in (["plugin", "marketplace", "add", str(ROOT)], ["plugin", "install", "skilllayer-verify@skilllayer"]):
            done = self.run_claude(*args)
            if done.returncode != 0:
                raise SystemExit(f"could not set up the plugin ({' '.join(args)}):\n{done.stdout}\n{done.stderr}")

    def hook_calls(self) -> list[dict]:
        """[{args, payload, stdout, exit}] in order, from the wrapper's log."""
        calls: list[dict] = []
        if not self.hook_log.exists():
            return calls
        for chunk in self.hook_log.read_text().split("ARGS: ")[1:]:
            head, _, rest = chunk.partition("\n")
            payload_text, _, tail = rest.partition("STDOUT: ")
            stdout_text, _, exit_text = tail.rpartition("EXIT: ")
            calls.append({"args": head, "payload": json.loads(payload_text.strip() or "{}"), "stdout": stdout_text.strip(), "exit": int(exit_text.strip() or -1)})
        return calls


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src/calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests/test_calc.py").write_text("from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    for args in (["init", "-q"], ["config", "user.email", "t@e.x"], ["config", "user.name", "t"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


def system_messages(stdout: str) -> list[str]:
    found = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "system" and event.get("subtype") != "init":
            found.append(str(event.get("content") or event.get("text") or ""))
    return found


def play(sandbox: Sandbox, repo: Path, turns, *, env_extra=None, extra_args=(), home=None, prompt="fix the bug in src/calc.py"):
    api = ScriptedApi(turns)
    try:
        env = sandbox.env(ANTHROPIC_BASE_URL=api.url, **(env_extra or {}))
        if home is not None:
            env.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(home / ".claude"))
        proc = sandbox.run_claude("-p", prompt, "--permission-mode", "bypassPermissions", "--output-format", "stream-json", "--verbose", *extra_args, env=env, cwd=repo)
    finally:
        api.close()
    return proc, api


# --------------------------------------------------------------------------- scenarios


def scenario_a_failing_tests_block_the_stop_and_the_agent_recovers(sandbox: Sandbox, tmp: Path, check):
    repo = make_repo(tmp / "s1")
    proc, api = play(sandbox, repo, [
        lambda: write_file("src/calc.py", BROKEN),
        lambda: text_turn("All done. The tests pass."),
        lambda: write_file("src/calc.py", FIXED),
        lambda: text_turn("Fixed."),
    ])
    check("session ends normally", proc.returncode == 0, proc.stderr[-300:])
    check("the agent was kept working after the first stop (4 model calls, not 2)", len(api.main_requests) == 4, len(api.main_requests))
    check("the block reason reached the model, with the failing test named", len(api.main_requests) > 2 and api.mentions(2, "cannot be accepted as complete") and api.mentions(2, "test_add"))
    check("...and not before the hook had run", not api.mentions(0, "cannot be accepted as complete") and not api.mentions(1, "cannot be accepted as complete"))
    calls = sandbox.hook_calls()
    stops = [c for c in calls if "stop" in c["args"]]
    check("UserPromptSubmit and Stop hooks both ran", any("prompt" in c["args"] for c in calls) and len(stops) == 2, [c["args"] for c in calls])
    first = json.loads(stops[0]["stdout"] or "{}") if stops else {}
    check("first stop returned a block decision, with stop_hook_active false", first.get("decision") == "block" and stops[0]["payload"].get("stop_hook_active") is False)
    check("second stop was allowed, with stop_hook_active true", len(stops) == 2 and stops[1]["stdout"] == "" and stops[1]["payload"].get("stop_hook_active") is True)
    shown = system_messages(proc.stdout)
    check("the human is told why (not only a generic 'Stop hook error')", any("Stop says: skilllayer sent the agent back to work (attempt 1 of 2): tests failing" in m and "test_add" in m for m in shown), shown)
    check("Stop payload carries session_id, cwd, last_assistant_message", bool(stops) and all(k in stops[0]["payload"] for k in ("session_id", "cwd", "last_assistant_message")))
    check("the hook is given a test budget below its own timeout", all("--max-seconds 540" in c["args"] for c in calls))
    events = [json.loads(line) for line in next((tmp / "verify-data").rglob("log.jsonl")).read_text().splitlines()]
    check("verdicts recorded: TESTS_FAILING (blocked) then VERIFIED", [e["verdict"] for e in events] == ["TESTS_FAILING", "VERIFIED"], [e["verdict"] for e in events])


def scenario_b_a_missing_binary_is_announced_never_silent(sandbox: Sandbox, tmp: Path, check):
    repo = make_repo(tmp / "s2")
    proc, api = play(sandbox, repo, [lambda: text_turn("All done.")], env_extra={"PATH": "/usr/bin:/bin"})
    messages = system_messages(proc.stdout)
    check("the user is told the work was not verified", any("NOT verified" in m and "not found" in m for m in messages), messages)
    check("the stop was not blocked", len(api.main_requests) == 1)


def scenario_c_a_hook_that_outlives_its_timeout_is_dropped_silently(sandbox: Sandbox, tmp: Path, check):
    """Harness behaviour the design depends on: this is why the hook clamps its own test
    run (--max-seconds 540) below the 600 s timeout it registers."""
    repo = make_repo(tmp / "s3")
    bare_home = tmp / "bare_home"
    (bare_home / ".claude").mkdir(parents=True)
    settings = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "sleep 20; echo late >&2; exit 2", "timeout": 2}]}]}}
    proc, api = play(sandbox, repo, [lambda: text_turn("All done.")], home=bare_home, extra_args=("--settings", json.dumps(settings)))
    check("the stop goes through after the timeout", proc.returncode == 0 and len(api.main_requests) == 1)
    check("and nothing tells the user (which is what --max-seconds exists to prevent)", system_messages(proc.stdout) == [], system_messages(proc.stdout))


def scenario_e_the_exit_code_protocol_still_works_when_selected(sandbox: Sandbox, tmp: Path, check):
    repo = make_repo(tmp / "s6")
    proc, api = play(sandbox, repo, [
        lambda: write_file("src/calc.py", BROKEN),
        lambda: text_turn("All done."),
        lambda: write_file("src/calc.py", FIXED),
        lambda: text_turn("Fixed."),
    ], env_extra={"SKILLLAYER_BLOCK_STYLE": "exit2"})
    stops = [c for c in sandbox.hook_calls() if "stop" in c["args"]]
    check("the first stop exited 2", bool(stops) and stops[0]["exit"] == 2, [c["exit"] for c in stops])
    check("the reason still reached the model", len(api.main_requests) > 2 and api.mentions(2, "cannot be accepted as complete"))


def scenario_d_a_test_command_from_the_callers_environment_is_what_runs(sandbox: Sandbox, tmp: Path, check):
    repo = make_repo(tmp / "s5")
    proc, api = play(sandbox, repo, [
        lambda: write_file("src/calc.py", FIXED),  # the real tests pass...
        lambda: text_turn("Done."),
        lambda: text_turn("Still done."),
    ], env_extra={"SKILLLAYER_TEST_COMMAND": "sh -c 'echo custom-runner-failed; exit 3'"})
    check("the custom command decided the verdict (the real tests would have passed)", len(api.main_requests) >= 3 and api.mentions(2, "custom-runner-failed"), len(api.main_requests))


def scenario_f_a_projects_settings_can_enable_the_plugin_for_a_teammate(sandbox: Sandbox, tmp: Path, check):
    """The team-rollout path: a machine that knows the marketplace but has not installed the
    plugin gets it from `enabledPlugins` in the repository's committed .claude/settings.json.
    (Registering the marketplace itself through `extraKnownMarketplaces` is not exercised:
    a non-interactive session does not apply it.)"""
    teammate = Sandbox(sandbox.claude, tmp / "teammate")
    known = teammate.run_claude("plugin", "marketplace", "add", str(ROOT))
    check("marketplace registered on the teammate's machine", known.returncode == 0, known.stderr[-200:])
    repo = make_repo(tmp / "s7")
    (repo / ".claude").mkdir()
    (repo / ".claude/settings.json").write_text(json.dumps({"enabledPlugins": {"skilllayer-verify@skilllayer": True}}))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "team settings"], cwd=repo, check=True, capture_output=True)
    proc, api = play(teammate, repo, [lambda: write_file("src/calc.py", BROKEN), lambda: text_turn("Done."), lambda: text_turn("Done again.")])
    stops = [c for c in teammate.hook_calls() if "stop" in c["args"]]
    check("the hooks ran without the plugin ever being installed by hand", bool(stops), [c["args"] for c in teammate.hook_calls()])
    check("and the first stop was blocked", bool(stops) and json.loads(stops[0]["stdout"] or "{}").get("decision") == "block")


def scenario_g_the_registered_timeout_leaves_room_for_the_test_budget(sandbox: Sandbox, tmp: Path, check):
    hooks = json.loads((ROOT / "plugin/hooks/hooks.json").read_text())["hooks"]
    timeout = hooks["Stop"][0]["hooks"][0]["timeout"]
    script = (ROOT / "plugin/hooks/skilllayer-hook.sh").read_text()
    check("Stop hook timeout (600) is above the 540 s test budget the wrapper passes", timeout > 540 and "--max-seconds 540" in script, timeout)


# --------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--claude", default=None, help="Path to the Claude Code binary (default: $CLAUDE_BIN, then `claude` on PATH).")
    parser.add_argument("--keep", action="store_true", help="Keep the temporary directory for inspection.")
    args = parser.parse_args()
    claude = args.claude or os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not claude:
        print("Claude Code binary not found: pass --claude or set CLAUDE_BIN.", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="skilllayer-e2e-"))
    failures: list[str] = []

    def check(label: str, ok: bool, detail=None) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + ("" if ok or detail is None else f"   [{detail}]"))
        if not ok:
            failures.append(label)

    try:
        sandbox = Sandbox(claude, tmp)
        version = sandbox.run_claude("--version").stdout.strip()
        print(f"Claude Code: {version}")
        sandbox.install_plugin()
        for scenario in (scenario_a_failing_tests_block_the_stop_and_the_agent_recovers, scenario_b_a_missing_binary_is_announced_never_silent, scenario_c_a_hook_that_outlives_its_timeout_is_dropped_silently, scenario_d_a_test_command_from_the_callers_environment_is_what_runs, scenario_e_the_exit_code_protocol_still_works_when_selected, scenario_f_a_projects_settings_can_enable_the_plugin_for_a_teammate, scenario_g_the_registered_timeout_leaves_room_for_the_test_budget):
            print(f"\n{scenario.__name__[9:].replace('_', ' ')}")
            sandbox.hook_log.unlink(missing_ok=True)
            scenario(sandbox, tmp, check)
    finally:
        if args.keep:
            print(f"\nkept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'FAILED: ' + str(len(failures)) + ' check(s)' if failures else 'all checks passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
