from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import TASK_ROUTES, WORKFLOWS


# Routing/intent decision for "no rule matched the task". Vague or ambiguous
# prose must NOT silently route to a write-capable, code-editing workflow (that
# is how "make the CLI faster" once reached FixBugWorkflow, stopped only by the
# internal-tier gate). The no-match fallback is instead a read-only clarification
# response that asks the user to pick a concrete workflow. Confidence is 0.0
# because, by definition, nothing matched.
FALLBACK_TASK_TYPE = "clarify_intent"
FALLBACK_CONFIDENCE = 0.0


@dataclass
class RouteDecision:
    task_type: str
    workflow: str
    macro: str
    confidence: float
    source: str
    llm_calls: int = 0
    # True when a routing rule matched the task; False when the router fell back
    # because nothing matched. This makes "no suitable workflow" an explicit
    # routing outcome rather than an implicit choice of workflow.
    matched: bool = True


class SkillRouter:
    """Small extracted router cascade from the A2/A3 experiments.

    This preserves the existing workflow set. It intentionally does not add new
    task types or use a real model by default. Its sole concern is intent: which
    workflow best matches a task, and what to fall back to when nothing matches.
    It knows nothing about whether a workflow is allowed to run.
    """

    def route(self, task_description: str) -> RouteDecision:
        text = task_description.lower()
        match = self._match_task_type(text)
        matched = match is not None
        if match is None:
            task_type, confidence = FALLBACK_TASK_TYPE, FALLBACK_CONFIDENCE
        else:
            task_type, confidence = match
        route = TASK_ROUTES[task_type]
        return RouteDecision(
            task_type=task_type,
            workflow=route["workflow"],
            macro=route["macro"],
            confidence=confidence,
            source="rule_router",
            llm_calls=0,
            matched=matched,
        )

    @staticmethod
    def macro_sequence(workflow: str) -> list[str]:
        return list(WORKFLOWS.get(workflow, []))

    def _match_task_type(self, text: str) -> tuple[str, float] | None:
        """Return the matched (task_type, confidence), or None when nothing matches."""
        if self._looks_like_save_context_request(text):
            return "save_context", 0.92
        if self._looks_like_track_decision_request(text):
            return "track_decision", 0.92
        if self._looks_like_remember_preferences_request(text):
            return "remember_preferences", 0.92
        if self._looks_like_rehydrate_context_request(text):
            return "rehydrate_context", 0.92
        if self._looks_like_compare_context_snapshots_request(text):
            return "compare_context_snapshots", 0.92
        if self._looks_like_search_decisions_request(text):
            return "search_decisions", 0.92
        if self._looks_like_add_todo_request(text):
            return "add_todo", 0.92
        if self._looks_like_mark_todo_done_request(text):
            return "mark_todo_done", 0.92
        if self._looks_like_list_todos_request(text):
            return "list_todos", 0.92
        if self._looks_like_inspect_runtime_request(text):
            return "inspect_runtime", 0.92
        if self._looks_like_check_port_request(text):
            return "check_port", 0.92
        if self._looks_like_monitor_flakiness_request(text):
            return "monitor_flakiness", 0.92
        if self._looks_like_detect_processes_request(text):
            return "detect_processes", 0.92
        if self._looks_like_measure_test_speed_request(text):
            return "measure_test_speed", 0.92
        if self._looks_like_measure_memory_request(text):
            return "measure_memory", 0.92
        if self._looks_like_profile_execution_request(text):
            return "profile_execution", 0.92
        if self._looks_like_detect_secrets_request(text):
            return "detect_secrets", 0.92
        if self._looks_like_search_request(text):
            return "search", 0.92
        if self._looks_like_find_conflicts_request(text):
            return "find_conflicts", 0.92
        if self._looks_like_git_blame_request(text):
            return "git_blame", 0.92
        if self._looks_like_list_branches_request(text):
            return "list_branches", 0.92
        if self._looks_like_get_commit_request(text):
            return "get_commit", 0.92
        if self._looks_like_file_history_request(text):
            return "file_history", 0.92
        if self._looks_like_git_log_request(text):
            return "git_log", 0.92
        if self._looks_like_git_diff_request(text):
            return "git_diff", 0.92
        if self._looks_like_watch_file_changes_request(text):
            return "watch_file_changes", 0.92
        if self._looks_like_detect_activity_request(text):
            return "detect_activity", 0.92
        if self._looks_like_watch_deps_request(text):
            return "watch_deps", 0.92
        if self._looks_like_git_status_request(text):
            return "git_status", 0.90
        if self._looks_like_inspect_repo_structure_request(text):
            return "inspect_repo_structure", 0.92
        if self._looks_like_map_dependencies_request(text):
            return "map_dependencies", 0.92
        if self._looks_like_dependency_check_request(text):
            return "dependency_check", 0.88
        if re.search(r"\b(browser smoke|smoke test|browser|frontend|page loads?|console errors?|network errors?|form still works|form works)\b", text):
            return "browser_smoke", 0.86
        if re.search(r"\b(rename|renaming|update imports?|import after rename)\b", text):
            return "rename_symbol", 0.88
        if self._looks_like_add_helper_request(text):
            return "add_helper_function", 0.82
        if (
            re.search(r"\b(fix(?:ing)?|repair|debug)\s+(?:the\s+)?(?:failing\s+)?tests?\b", text)
            or re.search(r"\b(fix(?:ing)?|repair|debug)\b.*\b(pytest|test|tests|failure|failures)\b", text)
            or re.search(r"\bmake\s+(?:the\s+)?tests?\s+pass\b", text)
        ):
            return "fix_failing_test", 0.86
        if re.search(
            r"\b(explain|diagnose|analy[sz]e|why)\b.*\b(pytest|npm test|test|tests|failure|failures|failing)\b",
            text,
        ) or re.search(r"\bwhy are tests failing\b", text):
            return "explain_failure", 0.88
        if self._looks_like_single_test_request(text):
            return "single_test", 0.88
        if re.search(
            r"\b(run|check|execute)\s+(?:the\s+)?(?:project\s+)?(?:unit\s+)?(?:test suite|tests?|pytest|unittest|npm test|pnpm test|yarn test)\b",
            text,
        ) or re.search(
            r"\b(run|check|execute)\s+(?:all\s+)?(?:the\s+)?(?:project\s+)?(?:unit\s+)?tests?\b",
            text,
        ) or re.search(r"\b(run pytest|run npm test|run pnpm test|run yarn test|test suite)\b", text):
            return "run_tests", 0.88
        if self._looks_like_detect_dead_code_request(text):
            return "detect_dead_code", 0.92
        if re.search(r"\b(find|locate|where is|definition)\b", text):
            return "find_function", 0.90
        return None

    def _looks_like_add_helper_request(self, text: str) -> bool:
        if re.search(r"\b(improve|make\s+project\s+better|fix\s+issue|clean\s+up|refactor)\b", text):
            return False
        return bool(
            re.search(
                r"\badd\s+(?:a\s+|small\s+|pure\s+|utility\s+)*helper\s+function\s+`?[a-z_][a-z0-9_]*`?\b",
                text,
            )
            or re.search(
                r"\badd\s+(?:a\s+|small\s+|pure\s+|utility\s+)*helper\s+`?[a-z_][a-z0-9_]*`?\b",
                text,
            )
        )

    def _looks_like_detect_dead_code_request(self, text: str) -> bool:
        return bool(
            re.search(r"\bdead\s+code\b", text)
            or re.search(r"\bunused\s+(?:function|class|method|symbol|code)s?\b", text)
            or re.search(r"\b(?:function|class|method|symbol)s?\s+(?:never\s+called|not\s+used|never\s+used|not\s+called|never\s+referenced)\b", text)
            or re.search(r"\bwhich\s+(?:function|class|method|symbol)s?\s+(?:are\s+)?(?:unused|never\s+called|dead|unreachable)\b", text)
            or re.search(r"\b(?:detect|find|scan|identify|list|show)\b.*\b(?:dead|unused|unreachable)\b", text)
            or re.search(r"\bnever\s+(?:called|used|referenced)\b", text)
        )

    def _looks_like_map_dependencies_request(self, text: str) -> bool:
        # Require explicit "all" / "map" / "list" scope to avoid colliding with
        # _looks_like_dependency_check_request which handles single-dep lookups.
        if re.search(r"\b(install|update|upgrade|remove|uninstall)\b", text):
            return False
        return bool(
            re.search(r"\b(map|list|show|dump|extract|scan)\s+(?:all\s+)?(?:project\s+)?dep(?:endencies)?\b", text)
            or re.search(r"\ball\s+dep(?:endencies)?\b", text)
            or re.search(r"\bdep(?:endency)?\s+(map|list|tree|graph|manifest|inventory)\b", text)
            or re.search(r"\b(requirements|pyproject|pipfile|package\.json)\s+dep(?:endencies)?\b", text)
            or re.search(r"\bwhat\s+(dep(?:endencies|s)?|packages?|requirements?)\s+(are\s+)?(?:in|used\s+by|declared\s+in)\b", text)
            or re.search(r"\b(unpinned|pinned|unversioned)\s+dep(?:endencies|s)?\b", text)
            or re.search(r"\bmap\s+dep(?:endencies)?\b", text)
        )

    # Well-known service names matched by detect_processes
    _KNOWN_SERVICES = re.compile(
        r"\b(postgres|postgresql|mysql|mongo|redis|sqlite|nginx|apache|caddy|uvicorn|gunicorn|"
        r"node|vite|webpack|docker|dockerd|containerd|pytest|jest|mocha|"
        r"database|db|web\s+server|dev\s+server|test\s+runner)\b",
        re.I,
    )

    def _looks_like_monitor_flakiness_request(self, text: str) -> bool:
        return bool(
            re.search(r"\bflak(y|iness|ier)\b", text, re.I)
            or re.search(r"\bhow\s+often\b.*\bfail\b", text, re.I)
            or re.search(r"\b(run|execute)\b.*\b\d+\s+times?\b", text, re.I)
            or re.search(r"\breliabilit(y|ies)\b", text, re.I)
            or re.search(r"\bmonitor\b.*\btest\b", text, re.I)
            or re.search(r"\bpass[\s-]rate\b", text, re.I)
            or re.search(r"\b\d+\s+times?\b.*\breport\b", text, re.I)
        )

    def _looks_like_measure_memory_request(self, text: str) -> bool:
        return bool(
            re.search(r"\bmeasure\s+memory\s+usage\b", text, re.I)
            or re.search(r"\bhow\s+much\s+memory\b", text, re.I)
            or re.search(r"\bprofile\s+memory\b", text, re.I)
            or re.search(r"\bcheck\s+memory\s+usage\b", text, re.I)
            or re.search(r"\bmemory\s+efficient\b", text, re.I)
            or re.search(r"\bmemory\s+profile\b", text, re.I)
        )

    def _looks_like_profile_execution_request(self, text: str) -> bool:
        # Note: "profile memory" is already captured by _looks_like_measure_memory_request above.
        return bool(
            re.search(r"\bprofile\s+execution\b", text, re.I)
            or re.search(r"\bhow\s+fast\s+is\b", text, re.I)
            or re.search(r"\bbenchmark\b", text, re.I)
            or re.search(r"\bwhat\s+is\s+slow\s+in\b", text, re.I)
            or re.search(r"\bmeasure\s+execution\s+time\b", text, re.I)
            or re.search(r"\bprofile\s+\S", text, re.I)
        )

    def _looks_like_measure_test_speed_request(self, text: str) -> bool:
        # Guard: "N times" queries belong to monitor_flakiness
        if re.search(r"\b\d+\s+times?\b", text, re.I):
            return False
        return bool(
            re.search(r"\bhow\s+(?:fast|slow)\b.*\btest", text, re.I)
            or re.search(r"\bmeasure\s+test\s+speed\b", text, re.I)
            or re.search(r"\bbenchmark\s+(?:the\s+)?test\s+suite\b", text, re.I)
            or re.search(r"\bhow\s+long\b.*\btests?\s+take\b", text, re.I)
            or re.search(r"\btests?\s+(?:are\s+)?(?:getting\s+)?(?:too\s+)?slow(?:er|est)?\b", text, re.I)
            or re.search(r"\btest\s+suite\s+speed\b", text, re.I)
            or re.search(r"\btest\s+suite\b.*\bslow(?:er|est)?\b", text, re.I)
            or re.search(r"\bcompare\b.*\btest\s+speed\b", text, re.I)
            or re.search(r"\btest\s+speed\b.*\bbaseline\b", text, re.I)
            or re.search(r"\bbaseline\b.*\btest\s+speed\b", text, re.I)
            or re.search(r"\btest\s+suite\b.*\bbaseline\b", text, re.I)
        )

    def _looks_like_search_request(self, text: str) -> bool:
        # Guard: "where is X used" with scope qualifier, or subject looks like a code symbol
        # (underscore or camelCase, NOT lowercase package names — those belong to dependency_check).
        # No re.I on the symbol check so [A-Z] stays case-sensitive.
        _where_used = bool(
            re.search(r"\bwhere\s+(?:is|are)\b.{0,60}\bused\b.{0,40}\b(?:codebase|repo|repository|code)\b", text, re.I)
            or re.search(r"\bwhere\s+(?:is|are)\s+\S*(?:_\S+|[A-Z]\w+)\s*\w*\s+used\b", text)
        )
        return bool(
            re.search(r"\bsearch\s+(?:the\s+)?(?:codebase|repo|repository|code)\b", text, re.I)
            or re.search(r"\bsearch\s+for\b", text, re.I)
            or re.search(r"\bgrep\s+for\b", text, re.I)
            or re.search(r"\bfind\s+all\s+(?:occurrences?\s+of|files\s+containing)\b", text, re.I)
            or _where_used
            or re.search(r"\bfind\b.{0,60}\bin\s+(?:the\s+)?(?:codebase|repo|repository|code)\b", text, re.I)
        )

    def _looks_like_watch_file_changes_request(self, text: str) -> bool:
        # Deliberately does NOT claim generic "what files changed" phrasing —
        # that already belongs to GitDiffWorkflow (\bwhat\s+files?\s+changed\b),
        # which shows actual diff content between two git refs, a different
        # concern from this workflow's add/modified/deleted-with-no-content
        # scan. Only phrasing specific to disk state / untracked / uncommitted
        # is claimed here, so the two workflows don't compete for the same text.
        return bool(
            re.search(r"\bwatch\s+(?:for\s+)?file\s+changes\b", text, re.I)
            or re.search(r"\bdetect\s+file\s+changes\b", text, re.I)
            or re.search(r"\buncommitted\s+changes\b", text, re.I)
            or re.search(r"\buntracked\s+files\b", text, re.I)
            or re.search(r"\bfiles?\s+changed\s+on\s+disk\b", text, re.I)
        )

    def _looks_like_watch_deps_request(self, text: str) -> bool:
        return bool(
            re.search(r"\bcheck\s+for\s+(?:dependency\s+)?updates\b", text, re.I)
            or re.search(r"\boutdated\s+(?:packages?|dep(?:endencies|s)?)\b", text, re.I)
            or re.search(r"\bdep(?:endencies|s)?\s+(?:need|that\s+need)\s+updating\b", text, re.I)
            or re.search(r"\bnewer\s+versions?\s+available\b", text, re.I)
            or re.search(r"\bwatch\s+dependency\s+updates\b", text, re.I)
            or re.search(r"\bare\s+my\s+dep(?:endencies|s)?\s+outdated\b", text, re.I)
        )

    def _looks_like_detect_activity_request(self, text: str) -> bool:
        return bool(
            re.search(r"\bwhat\s+changed\s+since\b", text, re.I)
            or re.search(r"\bdetect\s+(?:repo\s+)?activity\b", text, re.I)
            or re.search(r"\bwhat\s+happened\s+in\s+(?:this\s+)?repo\b", text, re.I)
            or re.search(r"\bshow\s+recent\s+activity\b", text, re.I)
            or re.search(r"\bhas\s+anything\s+changed\b", text, re.I)
            or re.search(r"\bwhat\s+commits\s+were\s+made\b", text, re.I)
        )

    def _looks_like_detect_secrets_request(self, text: str) -> bool:
        return bool(
            re.search(r"\bscan\b.*\bsecrets?\b", text, re.I)
            or re.search(r"\bcheck\b.*\bleaked?\b.*\bcredential", text, re.I)
            or re.search(r"\bfind\b.*\bapi.?keys?\b", text, re.I)
            or re.search(r"\bdetect\s+secrets?\b", text, re.I)
            or re.search(r"\bhardcoded?\b.*\bpasswords?\b", text, re.I)
            or re.search(r"\bsensitive\s+data\b", text, re.I)
            or re.search(r"\bleaked?\s+(?:api.?key|credential|secret|token)\b", text, re.I)
            or re.search(r"\bscan\b.*\bcredential", text, re.I)
            or re.search(r"\bcheck\b.*\bapi.?key", text, re.I)
        )

    def _looks_like_detect_processes_request(self, text: str) -> bool:
        # Don't steal port-number queries — those belong to check_port
        if re.search(r"\bport\s+\d+\b", text, re.I):
            return False
        return bool(
            re.search(r"\b(what|which|list|show|detect|enumerate)\b.*\b(process(es)?|service(s)?|running)\b", text, re.I)
            or re.search(r"\bprocess(es)?\s+(are\s+)?running\b", text, re.I)
            or re.search(r"\brunning\s+(process(es)?|service(s)?)\b", text, re.I)
            or re.search(r"\bwhat(\'s|s)?\s+running\b", text, re.I)
            or re.search(r"\bdetect\s+running\b", text, re.I)
            or (
                re.search(r"\bis\b.*\b(running|up|active|started)\b", text, re.I)
                and self._KNOWN_SERVICES.search(text)
            )
            or (
                re.search(r"\bis\b.*\b(running|up|active|started)\b", text, re.I)
                and re.search(r"\b(database|db|server|service)\b", text, re.I)
            )
        )

    def _looks_like_check_port_request(self, text: str) -> bool:
        return bool(
            re.search(r"\b(check|is|probe|test)\b.*\bport\s+\d+\b", text, re.I)
            or re.search(r"\bport\s+\d+\b.*\b(free|open|available|taken|in use|running|bound)\b", text, re.I)
            or re.search(r"\bis\s+(?:anything|something)\s+running\s+on\s+(?:port\s+)?\d+\b", text, re.I)
            or re.search(r"\bis\s+(?:the\s+)?(?:\w+\s+)?server\s+running\s+on\s+(?:port\s+)?\d+\b", text, re.I)
            or re.search(r"\bcheck\s+port\b", text, re.I)
            or re.search(r"\bport\s+availability\b", text, re.I)
        )

    def _looks_like_inspect_runtime_request(self, text: str) -> bool:
        return bool(
            re.search(r"\b(inspect|show|list|describe|check)\b.*\b(runtime|python\s+environment|python\s+version|venv|virtual\s+env)\b", text)
            or re.search(r"\b(runtime|environment)\s+(info|information|details|summary|report)\b", text)
            or re.search(r"\b(what\s+python\s+version|which\s+python)\b", text)
            or re.search(r"\b(list|show)\s+installed\s+packages?\b", text)
            or re.search(r"\binspect\s+runtime\b", text)
            or re.search(r"\binspect\s+(the\s+)?(python\s+)?environment\b", text)
        )

    def _looks_like_inspect_repo_structure_request(self, text: str) -> bool:
        # Don't intercept dependency queries — those route to map_dependencies
        if re.search(r"\bdepe?nd", text):
            return False
        return bool(
            re.search(r"\b(inspect|map|show|describe|analyze|analyse)\b.*\b(repo|repository|project|directory|structure|tree)\b", text)
            or re.search(r"\b(repo|repository|project)\s+(structure|layout|tree|overview|map)\b", text)
            or re.search(r"\b(directory|dir)\s+(structure|tree|layout|map)\b", text)
            or re.search(r"\b(file\s+count|files?\s+by\s+type|entry\s+points?)\b", text)
            or re.search(r"\bwhat\s+files?\s+(are\s+)?in\s+(this\s+)?(repo|project|directory)\b", text)
            or re.search(r"\b(inspect|map)\s+repo\s+structure\b", text)
        )

    def _looks_like_git_status_request(self, text: str) -> bool:
        if re.search(r"\b(commit|stage|unstage|reset|checkout|push|discard|revert|stash|apply|merge|rebase|pull)\b", text):
            return False
        return bool(
            re.search(r"\bgit\s+status\b", text)
            or re.search(r"\bshow\s+git\s+status\b", text)
            or re.search(r"\bsummarize\s+git\s+changes\b", text)
            or re.search(r"\bwhat\s+changed\??\b", text)
            or re.search(r"\bshow\s+repo\s+changes\b", text)
            or re.search(r"\bcheck\s+(?:the\s+)?working\s+tree\b", text)
            or re.search(r"\bsummarize\s+(?:unstaged|staged)\s+changes\b", text)
        )

    def _looks_like_dependency_check_request(self, text: str) -> bool:
        if re.search(r"\b(install|update|upgrade|remove|uninstall|delete|add)\b.*\b(dependency|package|module|library|requirements?)\b", text):
            return False
        if re.search(r"\b(dependency|dependencies|package|packages|library|module)\b", text) and re.search(
            r"\b(check|find|where|is|show|declared|used|usage)\b",
            text,
        ):
            return True
        return bool(
            re.search(r"\bis\s+`?[@a-z0-9_.\-/]+`?\s+(?:used|declared)\??\b", text)
            or re.search(r"\bwhere\s+is\s+`?[@a-z0-9_.\-/]+`?\s+used\??\b", text)
            or re.search(r"\bwhere\s+is\s+`?[@a-z0-9_.\-/]+`?\s+imported\??\b", text)
        )

    def _looks_like_save_context_request(self, text: str) -> bool:
        return bool(
            re.search(r"\bsave\s+context\b", text)
            or re.search(r"\bsave\s+(?:a\s+)?(?:context\s+)?snapshot\b", text)
            or re.search(r"\bsnapshot\s+(?:current\s+)?(?:repo\s+)?context\b", text)
            or re.search(r"\brecord\s+(?:current\s+)?(?:repo\s+)?state\b", text)
        )

    def _looks_like_track_decision_request(self, text: str) -> bool:
        return bool(
            re.search(r"\btrack\s+(?:a\s+)?decision\b", text)
            or re.search(r"\blog\s+(?:a\s+)?decision\b", text)
            or re.search(r"\brecord\s+(?:a\s+)?decision\b", text)
            or re.search(r"\badd\s+(?:a\s+)?(?:decision|adr)\b", text)
            or re.search(r"\barchitectural\s+decision\b", text)
        )

    def _looks_like_remember_preferences_request(self, text: str) -> bool:
        return bool(
            re.search(r"\bremember\s+(?:my\s+)?(?:coding\s+)?preferences?\b", text)
            or re.search(r"\bsave\s+(?:my\s+)?(?:coding\s+)?preferences?\b", text)
            or re.search(r"\bstore\s+(?:my\s+)?preferences?\b", text)
            or re.search(r"\bset\s+(?:coding\s+)?preferences?\b", text)
            or re.search(r"\bupdate\s+preferences?\b", text)
        )

    def _looks_like_rehydrate_context_request(self, text: str) -> bool:
        return bool(
            re.search(r"\brehydrate\s+context\b", text)
            or re.search(r"\brestore\s+context\b", text)
            or re.search(r"\bload\s+context\b", text)
            or re.search(r"\bshow\s+(?:saved\s+)?context\b", text)
            or re.search(r"\bread\s+(?:saved\s+)?context\b", text)
            or re.search(r"\bwhat\s+(?:is\s+(?:the\s+)?|was\s+(?:the\s+)?)current\s+context\b", text)
            or re.search(r"\bshow\s+(?:open\s+)?questions?\b", text)
            or re.search(r"\bshow\s+(?:recent\s+)?decisions?\b", text)
            or re.search(r"\bshow\s+(?:my\s+)?preferences?\b", text)
        )

    def _looks_like_compare_context_snapshots_request(self, text: str) -> bool:
        return bool(
            re.search(r"\bcompare\s+(?:my\s+)?(?:context\s+)?snapshots?\b", text)
            or re.search(r"\bdiff\s+(?:my\s+)?(?:context\s+)?snapshots?\b", text)
            or re.search(r"\bhow\s+did\s+(?:my\s+)?understanding\s+change\b", text)
            or re.search(r"\bwhat\s+changed\s+(?:in\s+)?(?:my\s+)?context\s+since\b", text)
            or re.search(r"\bcompare\s+context\s+(?:versions?|history)\b", text)
        )

    def _looks_like_search_decisions_request(self, text: str) -> bool:
        # Deliberately avoids two existing bare triggers checked earlier in
        # this cascade: _looks_like_track_decision_request's
        # \barchitectural\s+decision\b (any text containing that phrase, e.g.
        # "why did we make this architectural decision", stays with
        # track_decision — unchanged, pre-existing behavior) and
        # _looks_like_rehydrate_context_request's
        # \bshow\s+(?:recent\s+)?decisions?\b ("show decisions about X" stays
        # with rehydrate_context). Neither is being touched; this workflow's
        # phrasing is chosen to not overlap with either.
        return bool(
            re.search(r"\bwhy\s+did\s+we\s+(?:choose|decide|use|pick|select)\b", text, re.I)
            or re.search(r"\bsearch\s+decisions?\b", text, re.I)
            or re.search(r"\bfind\s+(?:a\s+|the\s+)?decisions?\s+(?:about|on|regarding)\b", text, re.I)
            or re.search(r"\blook\s+up\s+(?:the\s+)?decisions?\b", text, re.I)
            or re.search(r"\bquery\s+(?:the\s+)?decisions?\b", text, re.I)
            or re.search(r"\bsearch\s+(?:the\s+)?decision\s+log\b", text, re.I)
        )

    def _looks_like_add_todo_request(self, text: str) -> bool:
        return bool(
            re.search(r"\badd\s+(?:a\s+|another\s+)?todo\b", text, re.I)
            or re.search(r"\bcreate\s+(?:a\s+)?todo\b", text, re.I)
            or re.search(r"\bnew\s+todo\b", text, re.I)
        )

    def _looks_like_mark_todo_done_request(self, text: str) -> bool:
        return bool(
            re.search(r"\bmark\s+todo\b", text, re.I)
            or re.search(r"\b(?:complete|finish)\s+todo\b", text, re.I)
            or re.search(r"\breopen\s+todo\b", text, re.I)
        )

    def _looks_like_list_todos_request(self, text: str) -> bool:
        return bool(
            re.search(r"\b(?:show|list)\s+(?:all\s+)?(?:my\s+)?(?:done\s+|completed?\s+|finished\s+|open\s+)?todos?\b", text, re.I)
            or re.search(r"\bwhat(?:'s|\s+is)\s+left\s+to\s+do\b", text, re.I)
            or re.search(r"\bwhat\s+(?:todos?|to-?dos?)\s+(?:are|do i have)\b", text, re.I)
            or re.search(r"\bmy\s+todo\s+list\b", text, re.I)
        )

    def _looks_like_single_test_request(self, text: str) -> bool:
        if re.search(r"\b(all|project|suite|entire)\s+tests?\b", text) or re.search(r"\btest\s+suite\b", text):
            return False
        return bool(
            re.search(r"\brun\s+(?:only\s+)?(?:single\s+)?test\s+[`'\"]?[\w./\\:-]+", text)
            or re.search(r"\bre-?run\s+(?:only\s+)?(?:the\s+)?(?:failing\s+)?test\b", text)
            or re.search(r"\brun\s+failing\s+test\b", text)
            or re.search(r"\brun\s+single\s+test\b", text)
            or re.search(r"\brun\s+only\s+test_[a-zA-Z0-9_]+", text)
            or re.search(r"\brun\s+test\s+[\w./\\-]+\.py(?:::[\w\[\]-]+)?", text)
        )

    def _looks_like_list_branches_request(self, text: str) -> bool:
        return bool(
            re.search(r"\blist\s+(?:all\s+)?(?:git\s+)?branches?\b", text, re.I)
            or re.search(r"\bshow\s+(?:all\s+)?branches?\b", text, re.I)
            or re.search(r"\bwhat\s+branches?\s+exist\b", text, re.I)
            or re.search(r"\bwhat\s+branch\s+am\s+i\s+on\b", text, re.I)
            or re.search(r"\blist\s+git\s+branches?\b", text, re.I)
            or re.search(r"\bbranches?\s+(?:in\s+this\s+repo|available)\b", text, re.I)
        )

    def _looks_like_git_log_request(self, text: str) -> bool:
        return bool(
            re.search(r"\bshow\s+(?:git\s+)?log\b", text, re.I)
            or re.search(r"\bgit\s+log\b", text, re.I)
            or re.search(r"\bcommit\s+history\b", text, re.I)
            or re.search(r"\brecent\s+commits?\b", text, re.I)
            or re.search(r"\bshow\s+commit\s+history\b", text, re.I)
            or re.search(r"\bwho\s+committed\s+recently\b", text, re.I)
            or re.search(r"\blast\s+\d+\s+commits?\b", text, re.I)
        )

    def _looks_like_get_commit_request(self, text: str) -> bool:
        return bool(
            re.search(r"\bshow\s+commit\s+\S+\b", text, re.I)
            or re.search(r"\bwhat\s+changed\s+in\s+commit\s+\S+\b", text, re.I)
            or re.search(r"\bget\s+commit\s+details?\b", text, re.I)
            or re.search(r"\bshow\s+me\s+commit\s+\S+\b", text, re.I)
            or re.search(r"\bwhat\s+did\s+commit\s+\S+\s+do\b", text, re.I)
        )

    def _looks_like_git_diff_request(self, text: str) -> bool:
        if re.search(r"\bwhat\s+changed\s+since\b", text, re.I):
            return False
        return bool(
            re.search(r"\bshow\s+(?:the\s+)?diff\b", text, re.I)
            or re.search(r"\bgit\s+diff\b", text, re.I)
            or re.search(r"\bwhat\s+files?\s+changed\b", text, re.I)
            or re.search(r"\bwhat\s+changed\s+between\b", text, re.I)
            or re.search(r"\bshow\s+me\s+(?:the\s+)?diff\b", text, re.I)
        )

    def _looks_like_file_history_request(self, text: str) -> bool:
        return bool(
            re.search(r"\bhistory\s+(?:of|for)\s+(?:file\s+)?\S+\b", text, re.I)
            or re.search(r"\bfile\s+history\b", text, re.I)
            or re.search(r"\bwho\s+changed\s+\S+\b", text, re.I)
            or re.search(r"\bwhen\s+was\s+\S+\s+(?:last\s+)?modified\b", text, re.I)
            or re.search(r"\bshow\s+history\s+for\b", text, re.I)
            or re.search(r"\bwhat\s+commits?\s+touched\b", text, re.I)
        )

    def _looks_like_git_blame_request(self, text: str) -> bool:
        return bool(
            re.search(r"\bgit\s+blame\b", text, re.I)
            or re.search(r"\bblame\b", text, re.I)
            or re.search(r"\bwho\s+wrote\b", text, re.I)
            or re.search(r"\bwho\s+is\s+responsible\s+for\b", text, re.I)
            or re.search(r"\bwho\s+wrote\s+line\s+\d+\b", text, re.I)
        )

    def _looks_like_find_conflicts_request(self, text: str) -> bool:
        return bool(
            re.search(r"\bfind\s+(?:merge\s+)?conflicts?\b", text, re.I)
            or re.search(r"\bcheck\s+for\s+(?:merge\s+)?conflicts?\b", text, re.I)
            or re.search(r"\bare\s+there\s+(?:any\s+)?conflicts?\b", text, re.I)
            or re.search(r"\bdo\s+i\s+have\s+conflicts?\b", text, re.I)
            or re.search(r"\bshow\s+conflicts?\b", text, re.I)
            or re.search(r"\bmerge\s+conflicts?\b", text, re.I)
        )
