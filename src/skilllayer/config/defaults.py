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
}

WORKFLOW_METADATA = {
    "FindFunctionWorkflow": {
        "stability": "stable",
        "summary": "Locate function definitions and relevant code with repository search.",
    },
    "FixBugWorkflow": {
        "stability": "internal",
        "summary": "Reserved simple bug-fix route; deterministic edit capability remains limited.",
    },
    "RenameSymbolWorkflow": {
        "stability": "internal",
        "summary": "Internal, gated pending safeguards: repository-wide symbol replacement currently has no preview, confirmation, or rollback and can rewrite unintended matches. Not callable by external MCP agents until guardrails are added.",
    },
    "AddHelperWorkflow": {
        "stability": "internal",
        "summary": "Internal helper-insertion workflow; hidden from first-user guidance because placement assumptions remain repository-specific.",
    },
    "FixFailingTestWorkflow": {
        # INTERNAL — blocked from MCP until two hard blockers are resolved:
        # 1. No rollback: if verification fails after a patch is applied, the repo
        #    is left in the modified state. The caller is told to revert manually.
        # 2. Live-edit default: dry_run defaults to False; callers must opt in to
        #    safety rather than opt in to writes.
        "stability": "internal",
        "summary": "Apply a small catalog of deterministic test-failure repairs. Internal only — no rollback on failed fix attempt and live-edit is the current default.",
    },
    "RunTestsWorkflow": {
        "stability": "stable",
        "summary": "Run detected project tests (pytest, unittest, npm/pnpm/yarn) and return structured pass/fail output with pass_count, failure_count, and duration_ms. Explicit error when no runner detected or run times out. Never modifies files. Zero LLM calls — purely deterministic subprocess execution.",
    },
    "SingleTestWorkflow": {
        "stability": "stable",
        "summary": "Run one explicit test target (file, file::function, or function name) and return structured pass/fail output. Explicit distinct error codes for not-found, ambiguous, file-missing, and unspecified targets. Timeout returns TIMEOUT outcome. Never modifies files. Zero LLM calls — purely deterministic subprocess execution.",
    },
    "ExplainFailureWorkflow": {
        "stability": "stable",
        "summary": "Run tests and classify each failure with deterministic regex-based pattern matching (assertion_failure, import_error, type_error, etc.). Returns structured diagnoses with failure_type, confidence, evidence_snippet, file, line, likely_cause, and suggested_next_steps. Handles no failures, timeout, unparseable output, and no test runner gracefully. Never modifies files. Zero LLM calls — purely deterministic.",
    },
    "BrowserSmokeWorkflow": {
        "stability": "stable",
        "summary": "Run minimal browser smoke checks for a configured page and selectors.",
    },
    "GitStatusWorkflow": {
        "stability": "stable",
        "summary": "Read git status and diff stats without staging, committing, resetting, checking out, or pushing. Zero LLM calls — purely deterministic.",
    },
    "InspectRepoStructureWorkflow": {
        "stability": "stable",
        "summary": "Map repository directory structure, count files by type, report sizes per directory, and identify entry points without reading file contents. Zero LLM calls — purely deterministic.",
    },
    "MapDependenciesWorkflow": {
        "stability": "stable",
        "summary": "Parse requirements.txt, pyproject.toml, package.json, and Pipfile to extract all dependencies with version specs, types, and pinned status. Flags unpinned deps. Never modifies files. Zero LLM calls.",
    },
    "DetectDeadCodeWorkflow": {
        "stability": "stable",
        "summary": "Scan Python files for functions and classes that are defined but never called or imported. Flags potentially unused symbols with confidence levels (certain/possible). Excludes test files from the unused check. Never modifies files. Zero LLM calls.",
    },
    "DependencyCheckWorkflow": {
        "stability": "stable",
        "summary": "Check whether a named dependency is declared in requirements.txt, pyproject.toml, or package.json and whether it appears in source imports. Never modifies files. Zero LLM calls — purely deterministic.",
    },
    "SaveContextSnapshotWorkflow": {
        "stability": "stable",
        "summary": "Write a context snapshot (state + open questions) to .skilllayer/context/latest.md. Rotates previous snapshot to history (keeps last 5). Regenerates INDEX.md. Never reads source files. Zero LLM calls.",
    },
    "TrackDecisionLogWorkflow": {
        "stability": "stable",
        "summary": "Append an immutable ADR to .skilllayer/decisions/. Sequence-numbered, never edited after accepted. Supersession flips status of prior decision. Regenerates INDEX.md. Zero LLM calls.",
    },
    "RememberUserPreferencesWorkflow": {
        "stability": "stable",
        "summary": "Upsert per-repo coding preferences into .skilllayer/preferences.md grouped by domain. Last-writer-wins per key. Regenerates INDEX.md. Zero LLM calls.",
    },
    "RehydrateContextWorkflow": {
        "stability": "stable",
        "summary": "Read-only digest of .skilllayer/INDEX.md plus optional drill-down: full context, specific decision by id, or full preferences. Never modifies files. Zero LLM calls.",
    },
    "InspectRuntimeEnvironmentWorkflow": {
        "stability": "stable",
        "summary": "Inspect the active Python runtime: version, interpreter path, active venv, platform, installed packages, and CI-relevant environment variables. ANTHROPIC_API_KEY presence is reported as true/false — the value is never returned. Never reads source files. Zero LLM calls — purely deterministic.",
    },
    "CheckPortAvailabilityWorkflow": {
        "stability": "stable",
        "summary": "Check whether a TCP port is available on a given host. Returns available (bool), checked_at (ISO 8601), and process info (pid/name/user via psutil) when port is taken. Never reads source files. Zero LLM calls — purely deterministic.",
    },
    "DetectRunningProcessesWorkflow": {
        "stability": "stable",
        "summary": "Enumerate running processes and classify well-known dev services (web server, database, test runner, dev server, docker). Never exposes environment variables or command line arguments. Skips inaccessible processes gracefully. Zero LLM calls — purely deterministic.",
    },
    "MonitorTestFlakinessWorkflow": {
        "stability": "stable",
        "summary": "Run a test or test suite N times (default 5, max 20) using the existing test runner and report pass rate, flakiness flag, deduplicated failure messages, and per-run durations. Zero LLM calls — purely deterministic execution.",
    },
    "MeasureTestSuiteSpeedWorkflow": {
        "stability": "stable",
        "summary": "Run the full test suite once and return structured speed metrics: total_duration_ms, test_count, passed/failed/skipped, up to 5 slowest_tests (pytest --durations=5), speed_rating (fast/normal/slow/very_slow), and baseline_delta_ms vs. a persisted .skilllayer/test_speed_baseline.json. Saves a new baseline after each run. Never modifies source files. Zero LLM calls — purely deterministic.",
    },
    "DetectSecretPatternsWorkflow": {
        "stability": "stable",
        "summary": "Scan repository files for accidentally committed secrets using regex patterns. Detects Anthropic/OpenAI/AWS keys, private key blocks, GitHub tokens, generic API key assignments, bearer tokens, hardcoded IPs, and database URLs. Returns findings with file, line, pattern_name, severity, and match_preview (first 6 chars only — never the full value). Skips binary files, files over 1MB, and paths excluded by .gitignore or search-ignore rules. Flags findings in test files as likely_test_fixture. Zero LLM calls — purely deterministic.",
    },
    "SearchWorkflow": {
        "stability": "stable",
        "summary": "Search repository files for a text or regex pattern. Returns structured matches with file path, line number, 1-indexed column, a stripped preview (max 120 chars), and match_start/match_end offsets for highlighting. Supports literal text and regex modes, optional case-sensitive flag, and filename glob filtering (file_pattern). Skips binary files, files over 1MB, .venv, __pycache__, and gitignored paths. Caps results at 100 by default with truncated: true when exceeded. Zero LLM calls — purely deterministic.",
    },
    "MeasureMemoryUsageWorkflow": {
        "stability": "stable",
        "summary": "Measure peak RSS memory of a Python (.py) file by running it in an isolated subprocess. Returns peak_memory_mb, baseline_memory_mb (subprocess RSS before execution), delta_memory_mb, duration_ms, tracemalloc_top (up to 10 largest allocations by file/line), and a rating (efficient/moderate/heavy/very_heavy). Persists a baseline to .skilllayer/memory_baseline.json and computes baseline_delta_mb on subsequent runs for the same target. Only .py files supported; explicit errors for missing or non-Python targets. Zero LLM calls — purely deterministic.",
    },
    "ProfileCodeExecutionWorkflow": {
        "stability": "stable",
        "summary": "Profile a Python (.py) file using cProfile in an isolated subprocess. Returns total_duration_ms (wall clock), cpu_time_ms, function_calls, and hotspots (up to 10 by cumulative time with function/file/line/calls/total_time_ms/per_call_ms). Stdlib hotspots are filtered by default; pass include_stdlib=True to include them. Rating: fast (<100ms), normal (100-1000ms), slow (1-5s), very_slow (>5s). Persists a baseline to .skilllayer/profile_baseline.json and computes baseline_delta_ms on subsequent runs. Only .py files supported. Zero LLM calls — purely deterministic.",
    },
    "DetectRepoActivityWorkflow": {
        "stability": "stable",
        "summary": "Detect repo changes since the last saved snapshot. Returns commits_since (hash/message/author/timestamp), files_changed (path/status/insertions/deletions), branches_changed (name/status), and a summary (commit_count, files_changed_count, insertions_total, deletions_total, active_since). Saves a snapshot to .skilllayer/repo_activity_snapshot.json after every run; first_run: true on the initial call. Author email is never exposed. Explicit error if called outside a git repository. Zero LLM calls — purely deterministic git subprocess.",
    },
    "WatchDependencyUpdatesWorkflow": {
        "stability": "stable",
        "summary": "Check pinned dependencies against their latest published versions. Parses requirements.txt, pyproject.toml, and package.json; queries PyPI and npm registries (5s timeout per request). Returns per-dependency {name, current_version, latest_version, outdated, major_bump, minor_bump, patch_bump, source} plus summary counts (outdated_count, up_to_date_count, unknown_count) and the detected package_manager (pip/npm/both/none). Gracefully degrades on network failure — sets latest_version: null and outdated: false. Only pinned versions checked; unpinned deps silently skipped. Zero LLM calls — purely deterministic HTTP.",
    },
    "ListBranchesWorkflow": {
        "stability": "stable",
        "summary": "List all local and remote git branches with last-commit hash, message, and date. Computes ahead/behind counts versus the default branch (main or master) for local branches. Returns branches[], current_branch, and total_count. Explicit error if not a git repository. Author email is never exposed. Zero LLM calls — purely deterministic git subprocess.",
    },
    "GitLogWorkflow": {
        "stability": "stable",
        "summary": "Read git commit log with per-commit stats (files_changed, insertions, deletions). Supports limit (default 20, max 100), author filter, since date filter, and path filter. Author email is never exposed — author_name only. Zero LLM calls — purely deterministic git subprocess.",
    },
    "GetCommitDetailsWorkflow": {
        "stability": "stable",
        "summary": "Get full details of a specific git commit by hash: full message, author name (never email), date, parent hashes, and a per-file breakdown (path, status, insertions, deletions). Explicit error if commit hash not found. Zero LLM calls — purely deterministic git subprocess.",
    },
    "GitDiffWorkflow": {
        "stability": "stable",
        "summary": "Show diff between two git refs or between a ref and the working tree. Returns per-file {path, status, insertions, deletions, diff_preview} where diff_preview is capped at 500 chars per file. Supports optional path filter. Explicit errors for unresolvable refs. Zero LLM calls — purely deterministic git subprocess.",
    },
    "GetFileHistoryWorkflow": {
        "stability": "stable",
        "summary": "Get commit history for a specific file (git log -- file). Returns commits[], total_shown, first_commit_date, and last_modified_date. Author email is never exposed. Explicit error if file not found in git history. Limit defaults to 10, max 50. Zero LLM calls — purely deterministic git subprocess.",
    },
    "GitBlameWorkflow": {
        "stability": "stable",
        "summary": "Run git blame on a file and return per-line {line_number, content, commit_hash, author_name, date, summary}. Author email is never exposed. Capped at 200 lines with truncated flag. Supports optional start_line/end_line range. Explicit error if file not found. Returns unique_authors list and date_range. Zero LLM calls — purely deterministic git subprocess.",
    },
    "FindMergeConflictsWorkflow": {
        "stability": "stable",
        "summary": "Scan repository files for merge conflict markers (<<<<<<< HEAD). Returns per-file {file, conflict_count, sections[{start_line, separator_line, end_line}]}, total_files_with_conflicts, total_conflict_sections, and clean (true if no conflicts). Uses standard ignore rules. Pure file scan — no git commands. Zero LLM calls — purely deterministic.",
    },
}

COMMAND_METADATA = {
    "InspectRepo": {
        "stability": "stable",
        "summary": "Inspect file counts, Python LOC, tests, and detected test command.",
    },
    "Doctor": {
        "stability": "stable",
        "summary": "Check local SkillLayer runtime readiness.",
    },
    "FeedbackStatus": {
        "stability": "maintainer",
        "summary": "Summarize local tester feedback registry status without network or database dependencies.",
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
}

TASK_ROUTES = {
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
