from __future__ import annotations

WORKFLOWS = {
    "FindFunctionWorkflow": ["LocateRelevantCode", "ValidateChange"],
    "FixBugWorkflow": ["LocateRelevantCode", "ApplySmallCodeChange", "ValidateChange"],
    "RenameSymbolWorkflow": ["LocateRelevantCode", "RenameSymbolSafely", "ValidateChange"],
    "AddHelperWorkflow": [
        "LocateInsertionPoint",
        "InsertHelperFunction",
        "UpdateExports",
        "UpdateImports",
        "VerifyHelperUsage",
        "RunTests",
    ],
    "FixFailingTestWorkflow": [
        "RunTests",
        "ExplainFailure",
        "SelectSafeRepair",
        "ApplySafePatch",
        "VerifyTests",
    ],
    "RunTestsWorkflow": ["DetectTestCommand", "RunTests", "ParseTestFailures", "GenerateTestReport"],
    "SingleTestWorkflow": [
        "DetectTestTarget",
        "ValidateTarget",
        "RunSingleTest",
        "ParseResult",
        "GenerateSingleTestReport",
    ],
    "ExplainFailureWorkflow": [
        "RunTests",
        "ParseTestFailures",
        "ClassifyFailurePattern",
        "SuggestLikelyCause",
        "GenerateFailureExplanation",
    ],
    "BrowserSmokeWorkflow": ["LocateFrontendEntry", "RunBrowserSmoke", "InspectBrowserErrors", "GenerateSmokeReport"],
    "GitStatusWorkflow": ["DetectGitRepo", "ReadGitStatus", "ReadGitDiffStats", "SummarizeGitState"],
    "InspectRepoStructureWorkflow": [
        "MapDirectoryTree",
        "CountFileTypes",
        "ReportDirSizes",
        "IdentifyEntryPoints",
    ],
    "MapDependenciesWorkflow": [
        "ParseDependencyFiles",
        "ExtractVersionSpecs",
        "FlagUnpinnedDeps",
    ],
    "DetectDeadCodeWorkflow": [
        "ScanDefinitions",
        "FindReferences",
        "ClassifyUnused",
    ],
    "DependencyCheckWorkflow": [
        "ExtractDependencyName",
        "ScanDependencyFiles",
        "ScanImports",
        "GenerateDependencyReport",
    ],
    "SaveContextSnapshotWorkflow": [
        "WriteContextSnapshot",
        "RotateHistory",
        "RegenerateIndex",
    ],
    "TrackDecisionLogWorkflow": [
        "ClaimDecisionId",
        "WriteADR",
        "RegenerateIndex",
    ],
    "RememberUserPreferencesWorkflow": [
        "MergePreferences",
        "WritePreferences",
        "RegenerateIndex",
    ],
    "RehydrateContextWorkflow": [
        "ReadIndex",
        "OptionalDrillDown",
    ],
    "SearchDecisionLogWorkflow": [
        "LoadDecisions",
        "MatchFields",
        "RankResults",
        "ResolveSupersession",
    ],
    "AddTodoWorkflow": [
        "ValidateTodoText",
        "ClaimTodoId",
        "WriteTodo",
    ],
    "MarkTodoDoneWorkflow": [
        "LoadTodos",
        "UpdateTodoStatus",
        "SaveTodos",
    ],
    "ListTodosWorkflow": [
        "LoadTodos",
        "FilterByStatus",
    ],
    "InspectRuntimeEnvironmentWorkflow": [
        "ReadPythonVersion",
        "ReadInstalledPackages",
        "ReadEnvironmentVariables",
        "ReadPlatformInfo",
    ],
    "CheckPortAvailabilityWorkflow": [
        "ProbeSocket",
        "LookupProcess",
    ],
    "DetectRunningProcessesWorkflow": [
        "EnumerateProcesses",
        "ClassifyDevServices",
    ],
    "MonitorTestFlakinessWorkflow": [
        "RunTest",
        "CollectMetrics",
        "ComputeFlakiness",
    ],
    "MeasureTestSuiteSpeedWorkflow": [
        "RunFullSuite",
        "ParseDurations",
        "ComputeSpeedRating",
        "CompareBaseline",
    ],
    "DetectSecretPatternsWorkflow": [
        "ScanFiles",
        "MatchPatterns",
        "ClassifyFindings",
    ],
    "SearchWorkflow": [
        "Search",
        "CollectMatches",
        "FormatResults",
    ],
    "MeasureMemoryUsageWorkflow": [
        "SpawnProbe",
        "MeasureRSS",
        "TraceAllocations",
        "CompareBaseline",
    ],
    "ProfileCodeExecutionWorkflow": [
        "SpawnProfiler",
        "CollectStats",
        "RankHotspots",
        "CompareBaseline",
    ],
    "DetectRepoActivityWorkflow": [
        "LoadSnapshot",
        "DiffCommits",
        "DiffFiles",
        "DiffBranches",
        "SaveSnapshot",
    ],
    "WatchFileChangesWorkflow": [
        "LoadSnapshot",
        "ScanFiles",
        "DiffSnapshot",
        "SaveSnapshot",
    ],
    "CompareContextSnapshotsWorkflow": [
        "LoadSnapshotPair",
        "DiffState",
        "DiffOpenQuestions",
        "BuildComparisonReport",
    ],
    "WatchDependencyUpdatesWorkflow": [
        "ParseDependencies",
        "FetchLatestVersions",
        "ClassifyBumps",
        "BuildReport",
    ],
    "ListBranchesWorkflow": [
        "EnumerateBranches",
        "FetchAheadBehind",
        "BuildBranchReport",
    ],
    "GitLogWorkflow": [
        "ReadLog",
        "ParseCommitStats",
        "ApplyFilters",
    ],
    "GetCommitDetailsWorkflow": [
        "ResolveRef",
        "ReadCommitMeta",
        "ReadFileStats",
    ],
    "GitDiffWorkflow": [
        "ResolveRefs",
        "ReadNameStatus",
        "ReadNumstat",
        "BuildDiffPreviews",
    ],
    "GetFileHistoryWorkflow": [
        "ValidateFile",
        "ReadFileLog",
        "BuildHistoryReport",
    ],
    "GitBlameWorkflow": [
        "ValidateFile",
        "RunBlame",
        "ParsePorcelain",
    ],
    "FindMergeConflictsWorkflow": [
        "ScanFiles",
        "DetectMarkers",
        "BuildConflictReport",
    ],
    "ReportRealSessionUsageWorkflow": [
        "LocateSessionLogs",
        "ExtractUsageRecords",
        "AggregateUsage",
        "ClassifySkillLayerTools",
        "ApplyModelPricing",
        "BuildUsageReport",
    ],
    "ValidateMemoryWorkflow": [
        "LoadMemoryStore",
        "CheckIndexFreshness",
        "CheckFrontmatterSchemas",
        "CheckDecisionChainIntegrity",
        "CheckIdCounters",
        "BuildIntegrityReport",
    ],
    "SafeCodeChangeWorkflow": [
        "InspectRepoStructure",
        "InspectGitStatus",
        "IdentifyRelevantFilesAndSymbols",
        "ProduceBoundedChangePlan",
        "ValidateResultingDiff",
        "RunFocusedTests",
    ],
    "ReleaseReadinessWorkflow": [
        "InspectRepo",
        "InspectGitState",
        "DetectSecretsHonestly",
        "InspectPackagingMetadata",
        "InspectDependencyStatus",
        "InspectTestStatus",
        "SummarizeBlockersAndLimitations",
    ],
    "ResumeProjectWorkWorkflow": [
        "RehydrateSavedContext",
        "ValidateMemory",
        "InspectCurrentGitStatus",
        "CompareWithRememberedState",
        "IdentifyNextAction",
    ],
}

WRITE_BEHAVIORS = frozenset({
    "read_only",
    "stateful",
    "modifying",
    "external_side_effects_possible",
})


def _write_capability(
    write_behavior: str,
    *,
    state_locations: tuple[str, ...] = (),
    requires_write_consent: bool = False,
    may_dirty_worktree: bool = False,
) -> dict[str, object]:
    """Build the canonical write-capability fields stored with each workflow.

    Keeping these fields inside WORKFLOW_METADATA makes workflow discovery,
    CLI listing, MCP listing, and safety tests consume one declaration rather
    than maintaining parallel workflow-name lists.
    """
    if write_behavior not in WRITE_BEHAVIORS:
        raise ValueError(f"unsupported write behavior: {write_behavior}")
    return {
        "write_behavior": write_behavior,
        "state_locations": list(state_locations),
        "requires_write_consent": requires_write_consent,
        "may_dirty_worktree": may_dirty_worktree,
    }


WORKFLOW_METADATA = {
    "FindFunctionWorkflow": {
        "stability": "stable",
        "summary": "Locate function definitions and relevant code with repository search.",
        **_write_capability("read_only"),
    },
    "FixBugWorkflow": {
        "stability": "internal",
        "summary": "Reserved simple bug-fix route; deterministic edit capability remains limited.",
        **_write_capability(
            "modifying",
            state_locations=("repository_source_files",),
            requires_write_consent=True,
            may_dirty_worktree=True,
        ),
    },
    "RenameSymbolWorkflow": {
        "stability": "internal",
        "summary": "Internal, gated pending safeguards: repository-wide symbol replacement currently has no preview, confirmation, or rollback and can rewrite unintended matches. Not callable by external MCP agents until guardrails are added.",
        **_write_capability(
            "modifying",
            state_locations=("repository_python_files",),
            requires_write_consent=True,
            may_dirty_worktree=True,
        ),
    },
    "AddHelperWorkflow": {
        "stability": "internal",
        "summary": "Internal helper-insertion workflow; hidden from first-user guidance because placement assumptions remain repository-specific.",
        **_write_capability(
            "modifying",
            state_locations=("repository_python_files",),
            requires_write_consent=True,
            may_dirty_worktree=True,
        ),
    },
    "FixFailingTestWorkflow": {
        # INTERNAL — blocked from MCP until two hard blockers are resolved:
        # 1. No rollback: if verification fails after a patch is applied, the repo
        #    is left in the modified state. The caller is told to revert manually.
        # 2. Live-edit default: dry_run defaults to False; callers must opt in to
        #    safety rather than opt in to writes.
        "stability": "internal",
        "summary": "Apply a small catalog of deterministic test-failure repairs. Internal only — no rollback on failed fix attempt and live-edit is the current default.",
        **_write_capability(
            "modifying",
            state_locations=("repository_source_and_test_files", "repository_defined_test_side_effects"),
            requires_write_consent=True,
            may_dirty_worktree=True,
        ),
    },
    "RunTestsWorkflow": {
        "stability": "stable",
        "summary": "Run detected project tests (pytest, unittest, npm/pnpm/yarn) and return structured pass/fail output. SkillLayer does not directly edit source files, but repository-defined tests may create files or external state. Zero LLM calls — deterministic subprocess execution.",
        **_write_capability(
            "external_side_effects_possible",
            state_locations=("repository_defined_test_side_effects",),
            may_dirty_worktree=True,
        ),
    },
    "SingleTestWorkflow": {
        "stability": "stable",
        "summary": "Run one explicit test target and return structured pass/fail output. SkillLayer does not directly edit source files, but repository-defined tests may create files or external state. Zero LLM calls — deterministic subprocess execution.",
        **_write_capability(
            "external_side_effects_possible",
            state_locations=("repository_defined_test_side_effects",),
            may_dirty_worktree=True,
        ),
    },
    "ExplainFailureWorkflow": {
        "stability": "stable",
        "summary": "Run tests and classify failures with deterministic regex matching. SkillLayer does not directly edit source files, but repository-defined tests may create files or external state. Zero LLM calls.",
        **_write_capability(
            "external_side_effects_possible",
            state_locations=("repository_defined_test_side_effects",),
            may_dirty_worktree=True,
        ),
    },
    "BrowserSmokeWorkflow": {
        "stability": "stable",
        "summary": "Run minimal browser smoke checks for a configured page and selectors. Screenshot/report writes are disabled by default and require browser_smoke.write_artifacts=true; every artifact path is returned and .gitignore is never edited.",
        **_write_capability(
            "external_side_effects_possible",
            state_locations=("configured_browser_smoke_output_dir", "network_requests"),
            requires_write_consent=True,
            may_dirty_worktree=True,
        ),
    },
    "GitStatusWorkflow": {
        "stability": "stable",
        "summary": "Read git status and diff stats without staging, committing, resetting, checking out, or pushing. Zero LLM calls — purely deterministic.",
        **_write_capability("read_only"),
    },
    "InspectRepoStructureWorkflow": {
        "stability": "stable",
        "summary": "Map repository directory structure, count files by type, report sizes per directory, and identify entry points without reading file contents. Zero LLM calls — purely deterministic.",
        **_write_capability("read_only"),
    },
    "MapDependenciesWorkflow": {
        "stability": "stable",
        "summary": "Parse requirements.txt, pyproject.toml, package.json, and Pipfile to extract all dependencies with version specs, types, and pinned status. Flags unpinned deps. Never modifies files. Zero LLM calls.",
        **_write_capability("read_only"),
    },
    "DetectDeadCodeWorkflow": {
        "stability": "stable",
        "summary": "Scan Python files for functions and classes that are defined but never called or imported. Flags potentially unused symbols with confidence levels (certain/possible). Excludes test files from the unused check. Never modifies files. Zero LLM calls.",
        **_write_capability("read_only"),
    },
    "DependencyCheckWorkflow": {
        "stability": "stable",
        "summary": "Check whether a named dependency is declared in requirements.txt, pyproject.toml, or package.json and whether it appears in source imports. Never modifies files. Zero LLM calls — purely deterministic.",
        **_write_capability("read_only"),
    },
    "SaveContextSnapshotWorkflow": {
        "stability": "stable",
        "summary": "Write a context snapshot (state + open questions) to .skilllayer/context/latest.md. Rotates previous snapshot to history (keeps last 5). Regenerates INDEX.md. Never reads source files. Zero LLM calls.",
        **_write_capability(
            "stateful",
            state_locations=(
                ".skilllayer/.memory.lock",
                ".skilllayer/context/latest.md",
                ".skilllayer/context/history/*.md",
                ".skilllayer/INDEX.md",
            ),
            may_dirty_worktree=True,
        ),
    },
    "TrackDecisionLogWorkflow": {
        "stability": "stable",
        "summary": "Append an immutable ADR to .skilllayer/decisions/. Sequence-numbered, never edited after accepted. Supersession flips status of prior decision. Regenerates INDEX.md. Zero LLM calls.",
        **_write_capability(
            "stateful",
            state_locations=(
                ".skilllayer/.memory.lock",
                ".skilllayer/.state.json",
                ".skilllayer/decisions/*.md",
                ".skilllayer/INDEX.md",
            ),
            may_dirty_worktree=True,
        ),
    },
    "RememberUserPreferencesWorkflow": {
        "stability": "stable",
        "summary": "Upsert per-repo coding preferences into .skilllayer/preferences.md grouped by domain. Last-writer-wins per key. Regenerates INDEX.md. Zero LLM calls.",
        **_write_capability(
            "stateful",
            state_locations=(
                ".skilllayer/.memory.lock",
                ".skilllayer/preferences.md",
                ".skilllayer/INDEX.md",
            ),
            may_dirty_worktree=True,
        ),
    },
    "RehydrateContextWorkflow": {
        "stability": "stable",
        "summary": "Read-only digest of .skilllayer/INDEX.md plus optional drill-down: full context, specific decision by id, or full preferences. Never modifies files. Zero LLM calls.",
        **_write_capability("read_only"),
    },
    "SearchDecisionLogWorkflow": {
        "stability": "stable",
        "summary": "Search TrackDecisionLog's decisions/*.md by keyword. Case-insensitive substring match (not regex) across title/context/decision/reasoning/consequences by default; narrow with an optional fields list. Ranking: title match ranks above body-only match, then more matched fields ranks higher, then newest created (with decision_id as a tiebreak, since created has only second-level resolution) as the final tiebreak. Every result reports status, superseded_by, and current_decision_id — resolved by walking the full superseded_by chain, not just one hop — plus matched_fields and a short matched_snippets excerpt per matched field. Read-only; never writes. Zero LLM calls — purely deterministic string comparison.",
        **_write_capability("read_only"),
    },
    "AddTodoWorkflow": {
        "stability": "stable",
        "summary": "Add a discrete, actionable todo distinct from SaveContextSnapshot's Open Questions (which have no id, no independent lifecycle, and are diffed by exact-string match, not individually tracked). Claims an id via the same atomically-incrementing counter TrackDecisionLog uses for decision_id (a separate 'next_todo_id' sequence in .state.json). Rejects empty text (error_code empty_todo_text) or text over 500 characters (error_code todo_text_too_long) — a todo is a short imperative phrase, not a paragraph. Writes to .skilllayer/todos.json and regenerates INDEX.md. Zero LLM calls — purely deterministic.",
        **_write_capability(
            "stateful",
            state_locations=(
                ".skilllayer/.memory.lock",
                ".skilllayer/.state.json",
                ".skilllayer/todos.json",
                ".skilllayer/INDEX.md",
            ),
            may_dirty_worktree=True,
        ),
    },
    "MarkTodoDoneWorkflow": {
        "stability": "stable",
        "summary": "Set a todo's status by id. Defaults to marking it done (stamps done_at); pass status='open' to reopen a previously-done todo (clears done_at). Returns error_code memory_record_not_found if the id does not exist, or invalid_status if status is not 'open'/'done'. Writes to .skilllayer/todos.json and regenerates INDEX.md. Zero LLM calls — purely deterministic.",
        **_write_capability(
            "stateful",
            state_locations=(
                ".skilllayer/.memory.lock",
                ".skilllayer/todos.json",
                ".skilllayer/INDEX.md",
            ),
            may_dirty_worktree=True,
        ),
    },
    "ListTodosWorkflow": {
        "stability": "stable",
        "summary": "List todos from .skilllayer/todos.json, filtered by status: 'open' (default — the far more common query), 'done', or 'all'. Returns open_count/done_count/total_count alongside the filtered list. Read-only; never writes. Zero LLM calls — purely deterministic.",
        **_write_capability("read_only"),
    },
    "InspectRuntimeEnvironmentWorkflow": {
        "stability": "stable",
        "summary": "Inspect the active Python runtime: version, interpreter path, active venv, platform, installed packages, and CI-relevant environment variables. ANTHROPIC_API_KEY presence is reported as true/false — the value is never returned. Never reads source files. Zero LLM calls — purely deterministic.",
        **_write_capability("read_only"),
    },
    "CheckPortAvailabilityWorkflow": {
        "stability": "stable",
        "summary": "Check whether a TCP port is available on a given host. Returns available (bool), checked_at (ISO 8601), and process info (pid/name/user via psutil) when port is taken. Never reads source files. Zero LLM calls — purely deterministic.",
        **_write_capability(
            "external_side_effects_possible",
            state_locations=("network_socket",),
        ),
    },
    "DetectRunningProcessesWorkflow": {
        "stability": "stable",
        "summary": "Enumerate running processes and classify well-known dev services (web server, database, test runner, dev server, docker). Never exposes environment variables or command line arguments. Skips inaccessible processes gracefully. Zero LLM calls — purely deterministic.",
        **_write_capability("read_only"),
    },
    "MonitorTestFlakinessWorkflow": {
        "stability": "stable",
        "summary": "Run a test or test suite N times (default 5, max 20) using the existing test runner and report pass rate, flakiness flag, deduplicated failure messages, and per-run durations. Zero LLM calls — purely deterministic execution.",
        **_write_capability(
            "external_side_effects_possible",
            state_locations=("repository_defined_test_side_effects",),
            may_dirty_worktree=True,
        ),
    },
    "MeasureTestSuiteSpeedWorkflow": {
        "stability": "stable",
        "summary": "Run the full test suite once and return structured speed metrics: total_duration_ms, test_count, passed/failed/skipped, up to 5 slowest_tests (pytest --durations=5), speed_rating (fast/normal/slow/very_slow), and baseline_delta_ms vs. .skilllayer/test_speed_baseline.json. Baseline persistence requires explicit consent and every written path is reported. Never edits .gitignore. Zero LLM calls — purely deterministic.",
        **_write_capability(
            "external_side_effects_possible",
            state_locations=(
                "repository_defined_test_side_effects",
                ".skilllayer/test_speed_baseline.json",
            ),
            requires_write_consent=True,
            may_dirty_worktree=True,
        ),
    },
    "DetectSecretPatternsWorkflow": {
        "stability": "stable",
        "summary": "Scan repository files for accidentally committed secrets using regex patterns. Detects Anthropic/OpenAI/AWS keys, private key blocks, GitHub tokens, generic API key assignments, bearer tokens, hardcoded IPs, and database URLs. Returns findings with file, line, pattern_name, severity, and match_preview (first 6 chars only — never the full value). Skips binary files, files over 1MB, and paths excluded by .gitignore or search-ignore rules. Flags findings in test files as likely_test_fixture. Zero LLM calls — purely deterministic.",
        **_write_capability("read_only"),
    },
    "SearchWorkflow": {
        "stability": "stable",
        "summary": "Search repository files for a text or regex pattern. Returns structured matches with file path, line number, 1-indexed column, a stripped preview (max 120 chars), and match_start/match_end offsets for highlighting. Supports literal text and regex modes, optional case-sensitive flag, and filename glob filtering (file_pattern). Skips binary files, files over 1MB, .venv, __pycache__, and gitignored paths. Caps results at 100 by default with truncated: true when exceeded. Zero LLM calls — purely deterministic.",
        **_write_capability("read_only"),
    },
    "MeasureMemoryUsageWorkflow": {
        # INTERNAL — gated pending an execution-safety fix: the target path is not
        # confined to the authorized repository (an absolute path outside the repo
        # is accepted as-is), and the probe subprocess suppresses BaseException from
        # the target script, so a target that fails or exits non-zero can still be
        # reported as a successful measurement. Not callable by external MCP agents
        # until both are fixed.
        "stability": "internal",
        "summary": "Measure peak RSS memory of a Python (.py) file by running it in an isolated subprocess. Returns peak_memory_mb, baseline_memory_mb (subprocess RSS before execution), delta_memory_mb, duration_ms, tracemalloc_top (up to 10 largest allocations by file/line), and a rating (efficient/moderate/heavy/very_heavy). Baseline persistence to .skilllayer/memory_baseline.json requires explicit consent; .gitignore is never edited. Only .py files supported. INTERNAL: target path is not confined to the repository and target failures can be masked as success; not exposed over MCP until fixed.",
        **_write_capability(
            "external_side_effects_possible",
            state_locations=("target_script_side_effects", ".skilllayer/memory_baseline.json"),
            requires_write_consent=True,
            may_dirty_worktree=True,
        ),
    },
    "ProfileCodeExecutionWorkflow": {
        # INTERNAL — same execution-safety gap as MeasureMemoryUsageWorkflow: an
        # absolute target path outside the repository is accepted, and the profile
        # probe subprocess suppresses BaseException from the target, so a failing
        # target can still return profiling stats as if it ran successfully. Not
        # callable by external MCP agents until both are fixed.
        "stability": "internal",
        "summary": "Profile a Python (.py) file using cProfile in an isolated subprocess. Returns total_duration_ms (wall clock), cpu_time_ms, function_calls, and hotspots. Baseline persistence to .skilllayer/profile_baseline.json requires explicit consent; .gitignore is never edited. Only .py files supported. INTERNAL: target path is not confined to the repository and target failures can be masked as success; not exposed over MCP until fixed.",
        **_write_capability(
            "external_side_effects_possible",
            state_locations=("target_script_side_effects", ".skilllayer/profile_baseline.json"),
            requires_write_consent=True,
            may_dirty_worktree=True,
        ),
    },
    "DetectRepoActivityWorkflow": {
        "stability": "stable",
        "summary": "Detect repo changes since the last saved snapshot. Returns commits_since, files_changed, branches_changed, and a summary. Persisting .skilllayer/repo_activity_snapshot.json requires explicit consent; every written path is reported and .gitignore is never edited. Zero LLM calls — deterministic git subprocess.",
        **_write_capability(
            "stateful",
            state_locations=(".skilllayer/repo_activity_snapshot.json",),
            requires_write_consent=True,
            may_dirty_worktree=True,
        ),
    },
    "WatchFileChangesWorkflow": {
        "stability": "stable",
        "summary": "Detect files added/modified/deleted on disk since the last saved snapshot, independent of git. Persisting .skilllayer/file_watch_snapshot.json requires explicit consent; every written path is reported and .gitignore is never edited. Scope defaults to the whole repo. Never returns file content. Zero LLM calls — deterministic filesystem scan.",
        **_write_capability(
            "stateful",
            state_locations=(".skilllayer/file_watch_snapshot.json",),
            requires_write_consent=True,
            may_dirty_worktree=True,
        ),
    },
    "CompareContextSnapshotsWorkflow": {
        "stability": "stable",
        "summary": "Diff two saved SaveContextSnapshotWorkflow context snapshots (context/latest.md and context/history/*.md) to show how understanding of a repo evolved between sessions. Selects the pair via from_index/to_index (0 = latest, 1 = one snapshot back) or from_timestamp/to_timestamp; defaults to latest vs. the single most recent history file. Returns state_diff (unified_diff line list plus word_count_before/after — a textual diff, not a semantic one) and open_questions_diff (resolved/new/still_open lists via exact string match — a question that was reworded rather than resolved will show as one resolved and one new, since there is no semantic matching without an LLM call). Read-only: never writes to context/ or INDEX.md. Explicit error if fewer than two snapshots exist. Zero LLM calls — purely deterministic text comparison.",
        **_write_capability("read_only"),
    },
    "WatchDependencyUpdatesWorkflow": {
        "stability": "stable",
        "summary": "Check pinned dependencies against their latest published versions. Parses requirements.txt, pyproject.toml, and package.json; queries PyPI and npm registries (5s timeout per request). Returns per-dependency {name, current_version, latest_version, outdated, major_bump, minor_bump, patch_bump, source} plus summary counts (outdated_count, up_to_date_count, unknown_count) and the detected package_manager (pip/npm/both/none). Gracefully degrades on network failure — sets latest_version: null and outdated: false. Only pinned versions checked; unpinned deps silently skipped. Zero LLM calls — purely deterministic HTTP.",
        **_write_capability(
            "external_side_effects_possible",
            state_locations=("network_requests",),
        ),
    },
    "ListBranchesWorkflow": {
        "stability": "stable",
        "summary": "List all local and remote git branches with last-commit hash, message, and date. Computes ahead/behind counts versus the default branch (main or master) for local branches. Returns branches[], current_branch, and total_count. Explicit error if not a git repository. Author email is never exposed. Zero LLM calls — purely deterministic git subprocess.",
        **_write_capability("read_only"),
    },
    "GitLogWorkflow": {
        "stability": "stable",
        "summary": "Read git commit log with per-commit stats (files_changed, insertions, deletions). Supports limit (default 20, max 100), author filter, since date filter, and path filter. Author email is never exposed — author_name only. Zero LLM calls — purely deterministic git subprocess.",
        **_write_capability("read_only"),
    },
    "GetCommitDetailsWorkflow": {
        "stability": "stable",
        "summary": "Get full details of a specific git commit by hash: full message, author name (never email), date, parent hashes, and a per-file breakdown (path, status, insertions, deletions). Explicit error if commit hash not found. Zero LLM calls — purely deterministic git subprocess.",
        **_write_capability("read_only"),
    },
    "GitDiffWorkflow": {
        "stability": "stable",
        "summary": "Show diff between two git refs or between a ref and the working tree. Returns per-file {path, status, insertions, deletions, diff_preview} where diff_preview is capped at 500 chars per file. Supports optional path filter. Explicit errors for unresolvable refs. Zero LLM calls — purely deterministic git subprocess.",
        **_write_capability("read_only"),
    },
    "GetFileHistoryWorkflow": {
        "stability": "stable",
        "summary": "Get commit history for a specific file (git log -- file). Returns commits[], total_shown, first_commit_date, and last_modified_date. Author email is never exposed. Explicit error if file not found in git history. Limit defaults to 10, max 50. Zero LLM calls — purely deterministic git subprocess.",
        **_write_capability("read_only"),
    },
    "GitBlameWorkflow": {
        "stability": "stable",
        "summary": "Run git blame on a file and return per-line {line_number, content, commit_hash, author_name, date, summary}. Author email is never exposed. Capped at 200 lines with truncated flag. Supports optional start_line/end_line range. Explicit error if file not found. Returns unique_authors list and date_range. Zero LLM calls — purely deterministic git subprocess.",
        **_write_capability("read_only"),
    },
    "FindMergeConflictsWorkflow": {
        "stability": "stable",
        "summary": "Scan repository files for merge conflict markers (<<<<<<< HEAD). Returns per-file {file, conflict_count, sections[{start_line, separator_line, end_line}]}, total_files_with_conflicts, total_conflict_sections, and clean (true if no conflicts). Uses standard ignore rules. Pure file scan — no git commands. Zero LLM calls — purely deterministic.",
        **_write_capability("read_only"),
    },
    "ReportRealSessionUsageWorkflow": {
        "stability": "stable",
        "summary": "Report real, measured token usage from Claude Code's own local session logs (~/.claude/projects/<slug>/*.jsonl) — by session, by model, and by tool, with SkillLayer's own mcp__skilllayer__* tools reported separately. Reads only token counts, model ids, tool names, timestamps, and session/project identifiers via a strict allowlist; never reads prompt or response text. Defaults to the current project (override with scope='all', project, since/until, projects_dir). Estimated cost uses a SkillLayer-maintained rate table; unknown models return null cost, never a guess. Every report carries an explicit methodology note: usage is measured per assistant message, not per tool call, and no baseline comparison is possible. Read-only, never modifies Claude Code data. Zero LLM calls, zero network — purely deterministic.",
        **_write_capability("read_only"),
    },
    "ValidateMemoryWorkflow": {
        "stability": "stable",
        "summary": "Check the .skilllayer/ memory store for internal consistency: 20 deterministic checks across INDEX.md freshness (vs. a freshly regenerated index), decision supersession chain integrity (dangling supersedes/superseded_by, status mismatches, cycles including self-reference, duplicate ids), frontmatter/schema validity per file type (context/latest.md, decisions/*.md, preferences.md, todos.json), and .state.json id-counter drift (next_decision_id/next_todo_id behind the highest existing id). Returns store_exists (false if no store yet — zero checks run, never a crash), summary {checks_run, passed, warnings, broken}, and a flat findings[] list (check/severity/file/message/suggested_fix). Standalone, on-demand — unlike NeedsWrap, not run automatically on every memory write, since it fully parses every decision file and walks the supersession graph. Read-only, never repairs anything — suggested fixes are text only. Zero LLM calls — purely deterministic.",
        **_write_capability("read_only"),
    },
    "SafeCodeChangeWorkflow": {
        "stability": "stable",
        "summary": "Professional skill: orchestrates repository/git inspection, keyword-based relevant-file search, a bounded change plan (phase='plan'), and post-edit diff/test validation (phase='validate') into one bounded verdict. SkillLayer never edits source files itself — the host AI coding agent performs the actual change between the two calls; executed_by is always reported as 'host_agent'. Optionally saves a context snapshot on validate. Zero LLM calls.",
        **_write_capability(
            "external_side_effects_possible",
            state_locations=(
                ".skilllayer/.memory.lock",
                ".skilllayer/context/latest.md",
                ".skilllayer/context/history/*.md",
                ".skilllayer/INDEX.md",
            ),
            may_dirty_worktree=True,
        ),
    },
    "ReleaseReadinessWorkflow": {
        "stability": "stable",
        "summary": "Professional skill: aggregates repository inspection, git status, honest secret scanning, dependency inspection (unknown semantics preserved), memory integrity, and packaging metadata into one bounded release verdict. Bounded by default — detects the test command without running it (pass deep=True to execute the suite). Never certifies a repository as secure; any incomplete or skipped check reduces the verdict rather than becoming a false READY. Zero LLM calls, read-only (deep=True may execute the target repository's own test suite).",
        **_write_capability("external_side_effects_possible"),
    },
    "ResumeProjectWorkWorkflow": {
        "stability": "stable",
        "summary": "Professional skill: rehydrates saved .skilllayer/ context, validates memory integrity, inspects current git status, and compares against the last saved activity snapshot to report completed work, constraints, next action, and detected drift for a brand-new session. Always read-only unless confirm_update=True is passed together with new_state — memory is never overwritten implicitly. Zero LLM calls.",
        **_write_capability(
            "stateful",
            state_locations=(
                ".skilllayer/.memory.lock",
                ".skilllayer/context/latest.md",
                ".skilllayer/context/history/*.md",
                ".skilllayer/INDEX.md",
            ),
            may_dirty_worktree=True,
        ),
    },
}

COMMAND_METADATA = {
    "InspectRepo": {
        "stability": "stable",
        "summary": "Inspect file counts, Python LOC, tests, and detected test command.",
        **_write_capability("read_only"),
    },
    "Doctor": {
        "stability": "stable",
        "summary": "Check local SkillLayer runtime readiness.",
        **_write_capability("read_only"),
    },
    "FeedbackStatus": {
        "stability": "maintainer",
        "summary": "Summarize local tester feedback registry status without network or database dependencies.",
        **_write_capability("read_only"),
    },
}

MACROS = {
    "LocateRelevantCode": ["list_files", "search_symbol", "open_file"],
    "ApplySmallCodeChange": ["replace_text", "insert_text"],
    "RenameSymbolSafely": ["search_symbol", "replace_text", "run_tests"],
    "AddHelperFunction": ["insert_text", "run_tests"],
    "ValidateChange": ["run_tests"],
    "RepairFailure": ["inspect_error", "replace_text", "run_tests"],
    "DetectTestCommand": ["inspect_repo"],
    "RunTests": ["run_tests"],
    "DetectTestTarget": ["parse_task"],
    "ValidateTarget": ["inspect_repo"],
    "RunSingleTest": ["run_tests"],
    "ParseResult": ["inspect_error"],
    "GenerateSingleTestReport": ["inspect_error"],
    "ParseTestFailures": ["inspect_error"],
    "ClassifyFailurePattern": ["inspect_error"],
    "SuggestLikelyCause": ["inspect_error"],
    "GenerateFailureExplanation": ["inspect_error"],
    "GenerateTestReport": ["inspect_error"],
    "ExplainFailure": ["inspect_error"],
    "ParseTestFailure": ["inspect_error"],
    "LocateFailingSymbol": ["search_symbol", "open_file"],
    "ClassifySimpleFailure": ["inspect_error"],
    "ApplySimplePatch": ["replace_text"],
    "SelectSafeRepair": ["inspect_error", "search_symbol", "open_file"],
    "ApplySafePatch": ["replace_text"],
    "VerifyTests": ["run_tests"],
    "VerifyPatch": ["run_tests"],
    "LocateInsertionPoint": ["list_files", "open_file"],
    "InsertHelperFunction": ["insert_text"],
    "UpdateExports": ["replace_text"],
    "UpdateImports": ["replace_text"],
    "VerifyHelperUsage": ["search_symbol"],
    "LocateFrontendEntry": ["browser_open_page"],
    "RunBrowserSmoke": ["browser_wait_ready", "browser_element_exists"],
    "InspectBrowserErrors": ["browser_check_console_errors", "browser_check_network_errors"],
    "GenerateSmokeReport": ["browser_screenshot"],
    "DetectGitRepo": ["git_rev_parse"],
    "ReadGitStatus": ["git_status"],
    "ReadGitDiffStats": ["git_diff_stat"],
    "SummarizeGitState": ["git_status_summary"],
    "MapDirectoryTree": ["list_files"],
    "CountFileTypes": ["list_files"],
    "ReportDirSizes": ["list_files"],
    "IdentifyEntryPoints": ["list_files"],
    "ParseDependencyFiles": ["read_file"],
    "ExtractVersionSpecs": ["read_file"],
    "FlagUnpinnedDeps": ["read_file"],
    "ScanDefinitions": ["list_files", "read_file"],
    "FindReferences": ["search_symbol"],
    "ClassifyUnused": ["inspect_error"],
    "ExtractDependencyName": ["parse_task"],
    "ScanDependencyFiles": ["read_file"],
    "ScanImports": ["search_symbol"],
    "GenerateDependencyReport": ["dependency_report"],
    "LocateSessionLogs": ["read_file"],
    "ExtractUsageRecords": ["read_file"],
    "AggregateUsage": ["read_file"],
    "ClassifySkillLayerTools": ["read_file"],
    "ApplyModelPricing": ["read_file"],
    "BuildUsageReport": ["read_file"],
    "LoadMemoryStore": ["read_file"],
    "CheckIndexFreshness": ["read_file"],
    "CheckFrontmatterSchemas": ["read_file"],
    "CheckDecisionChainIntegrity": ["read_file"],
    "CheckIdCounters": ["read_file"],
    "BuildIntegrityReport": ["read_file"],
}

TASK_ROUTES = {
    # Read-only no-match fallback. Ambiguous prose lands here (never on a
    # write-capable workflow) and gets a "did you mean ...?" clarification.
    "clarify_intent": {"workflow": "ClarifyIntentWorkflow", "macro": "SuggestWorkflows"},
    "safe_code_change": {"workflow": "SafeCodeChangeWorkflow", "macro": "ProduceBoundedChangePlan"},
    "release_readiness": {"workflow": "ReleaseReadinessWorkflow", "macro": "SummarizeBlockersAndLimitations"},
    "resume_project_work": {"workflow": "ResumeProjectWorkWorkflow", "macro": "IdentifyNextAction"},
    "find_function": {"workflow": "FindFunctionWorkflow", "macro": "LocateRelevantCode"},
    "fix_simple_bug": {"workflow": "FixBugWorkflow", "macro": "ApplySmallCodeChange"},
    "rename_symbol": {"workflow": "RenameSymbolWorkflow", "macro": "RenameSymbolSafely"},
    "add_helper_function": {"workflow": "AddHelperWorkflow", "macro": "AddHelperFunction"},
    "fix_failing_test": {"workflow": "FixFailingTestWorkflow", "macro": "RepairFailure"},
    "run_tests": {"workflow": "RunTestsWorkflow", "macro": "RunTests"},
    "single_test": {"workflow": "SingleTestWorkflow", "macro": "RunSingleTest"},
    "explain_failure": {"workflow": "ExplainFailureWorkflow", "macro": "GenerateFailureExplanation"},
    "browser_smoke": {"workflow": "BrowserSmokeWorkflow", "macro": "RunBrowserSmoke"},
    "git_status": {"workflow": "GitStatusWorkflow", "macro": "ReadGitStatus"},
    "inspect_repo_structure": {"workflow": "InspectRepoStructureWorkflow", "macro": "MapDirectoryTree"},
    "map_dependencies": {"workflow": "MapDependenciesWorkflow", "macro": "ParseDependencyFiles"},
    "detect_dead_code": {"workflow": "DetectDeadCodeWorkflow", "macro": "ScanDefinitions"},
    "dependency_check": {"workflow": "DependencyCheckWorkflow", "macro": "ScanDependencyFiles"},
    "save_context": {"workflow": "SaveContextSnapshotWorkflow", "macro": "WriteContextSnapshot"},
    "track_decision": {"workflow": "TrackDecisionLogWorkflow", "macro": "WriteADR"},
    "remember_preferences": {"workflow": "RememberUserPreferencesWorkflow", "macro": "MergePreferences"},
    "rehydrate_context": {"workflow": "RehydrateContextWorkflow", "macro": "ReadIndex"},
    "compare_context_snapshots": {"workflow": "CompareContextSnapshotsWorkflow", "macro": "LoadSnapshotPair"},
    "search_decisions": {"workflow": "SearchDecisionLogWorkflow", "macro": "LoadDecisions"},
    "add_todo": {"workflow": "AddTodoWorkflow", "macro": "ValidateTodoText"},
    "mark_todo_done": {"workflow": "MarkTodoDoneWorkflow", "macro": "LoadTodos"},
    "list_todos": {"workflow": "ListTodosWorkflow", "macro": "LoadTodos"},
    "inspect_runtime": {"workflow": "InspectRuntimeEnvironmentWorkflow", "macro": "ReadRuntimeInfo"},
    "check_port": {"workflow": "CheckPortAvailabilityWorkflow", "macro": "ProbeSocket"},
    "detect_processes": {"workflow": "DetectRunningProcessesWorkflow", "macro": "EnumerateProcesses"},
    "monitor_flakiness": {"workflow": "MonitorTestFlakinessWorkflow", "macro": "RunTest"},
    "measure_test_speed": {"workflow": "MeasureTestSuiteSpeedWorkflow", "macro": "RunFullSuite"},
    "detect_secrets": {"workflow": "DetectSecretPatternsWorkflow", "macro": "ScanFiles"},
    "search": {"workflow": "SearchWorkflow", "macro": "Search"},
    "measure_memory": {"workflow": "MeasureMemoryUsageWorkflow", "macro": "SpawnProbe"},
    "profile_execution": {"workflow": "ProfileCodeExecutionWorkflow", "macro": "SpawnProfiler"},
    "detect_activity": {"workflow": "DetectRepoActivityWorkflow", "macro": "LoadSnapshot"},
    "watch_file_changes": {"workflow": "WatchFileChangesWorkflow", "macro": "LoadSnapshot"},
    "watch_deps": {"workflow": "WatchDependencyUpdatesWorkflow", "macro": "ParseDependencies"},
    "list_branches": {"workflow": "ListBranchesWorkflow", "macro": "EnumerateBranches"},
    "git_log": {"workflow": "GitLogWorkflow", "macro": "ReadLog"},
    "get_commit": {"workflow": "GetCommitDetailsWorkflow", "macro": "ResolveRef"},
    "git_diff": {"workflow": "GitDiffWorkflow", "macro": "ResolveRefs"},
    "file_history": {"workflow": "GetFileHistoryWorkflow", "macro": "ReadFileLog"},
    "git_blame": {"workflow": "GitBlameWorkflow", "macro": "RunBlame"},
    "find_conflicts": {"workflow": "FindMergeConflictsWorkflow", "macro": "ScanFiles"},
}


# --- Stability metadata accessor -------------------------------------------
# Reading a workflow's declared stability tier is plain metadata access and
# lives with the metadata. The *policy* built on top of these tiers (which tiers
# are blocked, the denial reason, enforcement) is the access-control boundary
# and lives in skilllayer.security.
def workflow_stability(workflow: str | None) -> str:
    """Return the declared stability tier for a workflow (default experimental)."""
    if not workflow:
        return "experimental"
    return str(WORKFLOW_METADATA.get(workflow, {}).get("stability", "experimental"))


# --- Verified Task Execution professional skill catalog entry --------------
# Unlike safe_code_change/release_readiness/resume_project_work, this skill is
# deliberately NOT wired into SkillRouter's automatic task_type routing: every
# VTE operation (vte_start/vte_status/vte_checkpoint/vte_resume/vte_finalize/
# vte_abandon) is an explicit tool call an agent chooses to make, never a
# silent side effect of free-text classification. This entry exists so the
# skill is truthfully discoverable (via skilllayer_list_skills and
# diagnostics) without expanding automatic routing surface.
VERIFIED_TASK_EXECUTION_SKILL = {
    "name": "verified_task_execution",
    "purpose": (
        "Guide an agent to call the public VTE MCP tools in the correct order "
        "so a code change is scoped, checkpointed, safely resumable after an "
        "interruption, and only ever reported complete when backed by "
        "recorded, persisted evidence — never by an LLM's own claim."
    ),
    "activation_examples": [
        "implement this safely",
        "make this change and verify it",
        "continue this interrupted task",
        "do this as a verified task",
        "fix this without touching unrelated files",
        "finish this and prove the tests passed",
    ],
    "non_activation_examples": [
        "what does this function do",
        "explain this file to me",
        "translate this docstring to Spanish",
        "brainstorm some feature ideas",
        "just make the edit, don't bother verifying anything",
    ],
    "required_mcp_tools": [
        "skilllayer_vte_start", "skilllayer_vte_status", "skilllayer_vte_checkpoint",
        "skilllayer_vte_resume", "skilllayer_vte_finalize", "skilllayer_vte_abandon",
    ],
    "supported_lifecycle": ["start", "checkpoint", "interrupt", "resume", "finalize", "abandon"],
    "expected_receipt_schema_version": 1,
    "safety_guarantees": [
        "Never reports TASK_VERIFIED_COMPLETE without recorded test evidence.",
        "Never silently authorizes a scope, ownership, or resume expansion; an "
        "ambiguous case returns a single-use, scoped, non-transferable "
        "confirmation_token instead of proceeding.",
        "Never resets, stashes, checks out, rebases, or otherwise mutates Git history.",
        "Never invoked automatically by SkillRouter/skilllayer_run; every VTE "
        "operation is an explicit tool call the agent chooses to make.",
    ],
    "known_limitations": [
        "No explicit ownership-release primitive; a lease is released only "
        "when its bounded TTL expires.",
        ".skilllayer/ must be listed in the repository's .gitignore for scope "
        "validation to behave correctly.",
        "Scope paths support only exact files (EXPLICIT) or directory "
        "prefixes (PREFIX); no glob (**) syntax.",
        "vte_checkpoint does not accept raw command/validation records; "
        "record test outcomes via vte_finalize's tests_recorded/tests_passed.",
    ],
}
