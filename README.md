# SkillLayer

SkillLayer is a deterministic execution layer for Claude Code agents.

Not a skill library. Not an LLM. Not a replacement for Claude Code.

## The Problem

Claude Code burns tokens reasoning through the same routine tasks on every
session — inspect a repo, run tests, find a function, check git status.
Every reasoning step costs money.

Gartner predicts AI coding costs will exceed the average developer salary by
2028 due to rising token consumption. Token discipline will not emerge through
developer choice alone.

## The Fix

SkillLayer handles routine tasks deterministically. Zero LLM calls.
Structured JSON output. Sub-second execution.

> Claude reasons. SkillLayer executes.

## What's Inside

32 workflows across 9 categories. 1200+ tests. Zero LLM calls on any workflow.

| Category | Workflows |
|----------|-----------|
| Code Intelligence | Search, FindFunction, InspectRepoStructure, MapDependencies, DetectDeadCode, DetectSecretPatterns |
| Test Running | RunTests, SingleTest, ExplainFailure, MonitorFlakiness, MeasureTestSuiteSpeed |
| Git | GitStatus, GitLog, GitDiff, GitBlame, ListBranches, GetCommitDetails, GetFileHistory, FindMergeConflicts |
| Memory | SaveContextSnapshot, TrackDecisionLog, RememberUserPreferences, RehydrateContext |
| Environment | InspectRuntime, CheckPort, DetectProcesses |
| Performance | MeasureMemoryUsage, ProfileCodeExecution |
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

SkillLayer exposes 30 MCP tools for Claude Code. See `docs/CLAUDE_CODE_SETUP.md` for setup.

```json
{
  "mcpServers": {
    "skilllayer": {
      "command": "/ABSOLUTE/PATH/TO/PROJECT/.venv/bin/python",
      "args": ["-m", "skilllayer.mcp_server"],
      "cwd": "/ABSOLUTE/PATH/TO/PROJECT"
    }
  }
}
```

Use an absolute path to the venv's Python — MCP clients generally launch the
server without your shell's venv active. Run `python scripts/generate_mcp_config.py`
after activating your venv to generate this with the real paths filled in.

## Why Zero LLM Calls?

Every routine task Claude reasons through costs tokens. SkillLayer replaces
reasoning with deterministic execution:

- `skilllayer_run(task="git status")` → structured JSON, 0 LLM calls
- `skilllayer_run(task="find function X")` → structured JSON, 0 LLM calls
- `skilllayer_run(task="run tests")` → structured JSON, 0 LLM calls

There is one MCP tool for routing tasks — `skilllayer_run` — plus 29 more for
specific operations (git log, blame, memory, dependency mapping, and so on).
Run `python -m skilllayer.mcp_server --list-tools` for the full, current list.

## License

Business Source License 1.1 (BSL 1.1)
Free for non-commercial use. Commercial use requires a license.
Converts to Apache 2.0 on 2031-01-01.

## Status

Early access. Looking for Claude Code power users to test and give honest
feedback. See `TESTER_GUIDE.md` to get started.
