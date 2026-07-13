# SkillLayer

SkillLayer is a deterministic execution and capability layer for Claude Code
agents.

Not a skill library. Not an LLM. Not a replacement for Claude Code.

## The Problem

SkillLayer provides local, structured operations for recurring repository
tasks such as inspection, search, test execution, and Git status. SkillLayer does not claim to prove counterfactual token savings.

## The Fix

SkillLayer routes supported requests to deterministic local implementations.
It makes no LLM calls itself and returns structured JSON. Runtime depends on
the repository, command, and environment; test, browser, and network-backed
workflows are not sub-second guarantees.

> Claude reasons. SkillLayer executes.

## What's Inside

Use `skilllayer workflows --json` for the authoritative per-workflow status,
stability, and write-behavior inventory. The stable public API is not a blanket safety claim:
some workflows run project tests, use a browser, access the network, or write
explicitly disclosed state beneath `.skilllayer/`.

| Area | Representative workflows |
|----------|-----------|
| Code Intelligence | Search, FindFunction, InspectRepoStructure, MapDependencies, DetectDeadCode, DetectSecretPatterns |
| Test Running | RunTests, SingleTest, ExplainFailure, MonitorFlakiness, MeasureTestSuiteSpeed |
| Git | GitStatus, GitLog, GitDiff, GitBlame, ListBranches, GetCommitDetails, GetFileHistory, FindMergeConflicts |
| Memory | SaveContextSnapshot, TrackDecisionLog, RememberUserPreferences, RehydrateContext |
| Environment | InspectRuntime, CheckPort, DetectProcesses |
| Performance | MeasureMemoryUsage, ProfileCodeExecution (disabled over MCP pending an execution-safety fix; not currently MCP-exposed) |
| Monitoring | DetectRepoActivity, WatchDependencyUpdates |
| Dependency | DependencyCheck |
| Browser | BrowserSmoke |

## Quick Start

```bash
git clone https://github.com/NickGBar/Skilllayer.git
cd Skilllayer
./scripts/install.sh
source .venv/bin/activate
python -m skilllayer doctor
```

## MCP Integration

The server derives its MCP tool list from registered handlers; use
`python -m skilllayer.mcp_server --list-tools` for the current list. See
`docs/CLAUDE_CODE_SETUP.md` for setup.

```json
{
  "mcpServers": {
    "skilllayer": {
      "command": "/ABSOLUTE/PATH/TO/PROJECT/.venv/bin/python",
      "args": ["-m", "skilllayer.mcp_server"]
    }
  }
}
```

Use an absolute path to the venv's Python — MCP clients generally launch the
server without your shell's venv active. Run
`python scripts/generate_mcp_config.py` after activating your venv to generate
the authoritative configuration and validate it with `skilllayer mcp-config-check`.

## Why Zero LLM Calls?

SkillLayer replaces certain routine tool operations with deterministic
execution:

- `skilllayer_run(task="git status")` → structured JSON, 0 LLM calls
- `skilllayer_run(task="find function X")` → structured JSON, 0 LLM calls
- `skilllayer_run(task="run tests")` → structured JSON, 0 LLM calls

There is one MCP tool for routing tasks — `skilllayer_run` — plus specific
operations (git log, blame, memory, dependency mapping, and so on).
Run `python -m skilllayer.mcp_server --list-tools` for the full, current list.

## License

Business Source License 1.1 (BSL 1.1)
Free for non-commercial use. Commercial use requires a license.
Converts to Apache 2.0 on 2031-01-01.

## Status

Early access. Looking for Claude Code power users to test and give honest
feedback. See `TESTER_GUIDE.md` to get started.
