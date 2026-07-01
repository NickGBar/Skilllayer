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

33 workflows across 9 categories. 1200+ tests. Zero LLM calls on any workflow.

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
python -m skilllayer doctor
```

## MCP Integration

SkillLayer exposes 30 MCP tools for Claude Code. See `docs/CLAUDE_CODE_SETUP.md` for setup.

```json
{
  "mcpServers": {
    "skilllayer": {
      "command": "python",
      "args": ["-m", "skilllayer.mcp_server"]
    }
  }
}
```

## Why Zero LLM Calls?

Every routine task Claude reasons through costs tokens. SkillLayer replaces
reasoning with deterministic execution:

- git status → `skilllayer_git_status` → structured JSON, 0 LLM calls
- find function → `skilllayer_run` → structured JSON, 0 LLM calls
- run tests → `skilllayer_run` → structured JSON, 0 LLM calls

## License

Business Source License 1.1 (BSL 1.1)
Free for non-commercial use. Commercial use requires a license.
Converts to Apache 2.0 on 2031-01-01.

## Status

Early access. Looking for Claude Code power users to test and give honest
feedback. See `TESTER_GUIDE.md` to get started.
