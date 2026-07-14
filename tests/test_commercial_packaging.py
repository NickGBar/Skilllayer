from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_one_prompt_leads_with_safety_and_covers_trial():
    text = _read("ONE_PROMPT_TEST.md")
    assert "Safety rules" in text[:700]
    for required in ("skilllayer-tester-sandbox", "Safe Code Change", "Release Readiness", "Resume Project Work", "results.md", "Do not push, commit, upload results"):
        assert required in text


def test_install_with_ai_is_confirmation_gated_and_does_not_touch_user_repos():
    text = _read("INSTALL_WITH_AI.md")
    for required in (
        "ask for my confirmation",
        "real initialize and\n   tools/list handshake",
        "Safe Code Change",
        "Release Readiness",
        "Resume Project Work",
        "Do not inspect, search, or modify any existing repository",
        "Do not use sudo",
        "rollback instructions",
    ):
        assert required in text
    assert "Free early access" in text


def test_commercial_materials_make_consistent_truthful_claims():
    combined = "\n".join(_read(rel) for rel in ("README.md", "INSTALL_WITH_AI.md", "ONE_PROMPT_TEST.md", "EARLY_ACCESS.md", "docs/index.html"))
    for phrase in ("Safe Code Change", "Release Readiness", "Resume Project Work", "Free early access", "macOS", "automatic dependency installation"):
        assert phrase in combined
    for forbidden in ("$49", "paid beta", "production ready", "guaranteed safe", "proven token savings"):
        assert forbidden not in combined.lower()


def test_results_template_has_required_privacy_and_commercial_fields():
    text = _read("RESULTS_TEMPLATE.md")
    for heading in ("# Environment", "# Installation", "# MCP", "# Safe Code Change", "# Environment Remediation", "# Release Readiness", "# Resume Project Work", "# Safety", "# Commercial Feedback", "# Consent"):
        assert heading in text
    assert "no automatic upload occurred" in text.lower()
    assert "credentials" in text


def test_landing_page_has_required_ctas_and_no_fake_social_proof():
    text = _read("docs/index.html")
    for phrase in ("Professional engineering skills for AI coding agents.", "Install with one prompt", "Try the safe sandbox", "View on GitHub", "Free early access", "No automatic dependency installation", "No hidden repository writes"):
        assert phrase in text
    assert "https://github.com/NickGBar/Skilllayer/blob/main/INSTALL_WITH_AI.md" in text
    for forbidden in ("testimonial", "customers", "users served", "limited slots"):
        assert forbidden not in text.lower()


def test_issue_templates_and_demo_are_present_and_sanitized():
    beta = _read(".github/ISSUE_TEMPLATE/beta-interest.yml")
    bug = _read(".github/ISSUE_TEMPLATE/bug_report.yml")
    demo = _read("DEMO_SCRIPT.md")
    assert "mature version" in beta.lower()
    assert "payment details" in beta.lower()
    assert "API keys" in bug
    assert "source code" in bug
    assert "60–120" in demo
    assert "external-user" in demo and "evidence" in demo
