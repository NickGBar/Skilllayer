"""Routing tests for the three professional skill packs.

Verifies SkillRouter selects safe_code_change / release_readiness /
resume_project_work for varied natural-language phrasing, that ambiguous
prose never silently routes to one of them (or any write-capable workflow),
and that adding these predicates did not regress existing routes.
"""
from __future__ import annotations

import pytest

from skilllayer.router import SkillRouter


@pytest.mark.parametrize(
    ("phrase", "expected_task_type"),
    [
        ("Исправь этот баг аккуратно и проверь результат.", "safe_code_change"),
        ("Make this change without touching unrelated files.", "safe_code_change"),
        ("Implement this safely and validate it.", "safe_code_change"),
        ("Перед изменением составь план и проверь diff.", "safe_code_change"),
        ("Можно ли это пушить?", "release_readiness"),
        ("Проверь, готов ли проект к релизу.", "release_readiness"),
        ("Is this safe to release?", "release_readiness"),
        ("Покажи блокеры перед публикацией.", "release_readiness"),
        ("Продолжи работу с прошлого раза.", "resume_project_work"),
        ("Восстанови контекст проекта.", "resume_project_work"),
        ("What was completed and what should I do next?", "resume_project_work"),
        ("Продолжи задачу в новой сессии.", "resume_project_work"),
    ],
)
def test_professional_routing_recall_evaluation(phrase, expected_task_type):
    assert SkillRouter().route(phrase).task_type == expected_task_type


@pytest.mark.parametrize(
    ("phrase", "expected_task_type"),
    [
        ("run the unit tests", "run_tests"),
        ("show git status", "git_status"),
        ("find function authenticate", "find_function"),
        ("scan for secrets", "detect_secrets"),
        ("make the CLI faster", "clarify_intent"),
        ("push the current branch", "clarify_intent"),
        ("show blockers in this function", "clarify_intent"),
        ("restore context", "rehydrate_context"),
    ],
)
def test_professional_routing_negative_controls(phrase, expected_task_type):
    assert SkillRouter().route(phrase).task_type == expected_task_type


class TestSafeChangeRouting:
    def test_matches_varied_phrasing(self):
        router = SkillRouter()
        phrases = [
            "implement this safely",
            "help me change this feature safely",
            "plan a safe code modification",
            "verify this change",
            "implement this issue without breaking unrelated behavior",
        ]
        for phrase in phrases:
            decision = router.route(phrase)
            assert decision.task_type == "safe_code_change", phrase
            assert decision.workflow == "SafeCodeChangeWorkflow"
            assert decision.matched is True


class TestReleaseReadinessRouting:
    def test_matches_varied_phrasing(self):
        router = SkillRouter()
        phrases = [
            "is this ready to ship",
            "check before release",
            "can I publish this repo",
            "prepare for external testers",
            "run a professional pre-release check",
        ]
        for phrase in phrases:
            decision = router.route(phrase)
            assert decision.task_type == "release_readiness", phrase
            assert decision.workflow == "ReleaseReadinessWorkflow"
            assert decision.matched is True


class TestResumeWorkRouting:
    def test_matches_varied_phrasing(self):
        router = SkillRouter()
        phrases = [
            "continue where I stopped",
            "what was I doing",
            "what should I work on next",
            "restore the project context",
            "start a new session and recover the project state",
        ]
        for phrase in phrases:
            decision = router.route(phrase)
            assert decision.task_type == "resume_project_work", phrase
            assert decision.workflow == "ResumeProjectWorkWorkflow"
            assert decision.matched is True


class TestAmbiguousAndNoMatch:
    def test_ambiguous_prose_does_not_silently_pick_a_skill(self):
        router = SkillRouter()
        decision = router.route("make the CLI faster")
        assert decision.task_type != "safe_code_change"
        assert decision.task_type != "release_readiness"
        assert decision.task_type != "resume_project_work"
        # Never silently routes to a write-capable workflow either.
        assert decision.matched is False
        assert decision.task_type == "clarify_intent"

    def test_bare_restore_context_keeps_existing_low_level_route(self):
        # Deliberately narrower than "restore the project context" — must not
        # regress into the new, richer skill.
        router = SkillRouter()
        decision = router.route("restore context")
        assert decision.task_type == "rehydrate_context"


class TestExistingRoutesUnaffected:
    def test_git_status_unaffected(self):
        assert SkillRouter().route("git status").task_type == "git_status"

    def test_run_tests_unaffected(self):
        assert SkillRouter().route("run the test suite").task_type == "run_tests"

    def test_find_function_unaffected(self):
        assert SkillRouter().route("find the definition of foo").task_type == "find_function"

    def test_detect_secrets_unaffected(self):
        assert SkillRouter().route("scan for secrets").task_type == "detect_secrets"

    def test_rehydrate_context_narrow_phrasing_unaffected(self):
        assert SkillRouter().route("show saved context").task_type == "rehydrate_context"
