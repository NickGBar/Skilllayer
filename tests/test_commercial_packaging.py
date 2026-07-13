from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_one_prompt_leads_with_safety_and_covers_trial():
    text = _read("ONE_PROMPT_TEST.md")
    assert "Safety rules" in text[:700]
    for required in ("skilllayer-tester-sandbox", "Safe Code Change", "Release Readiness", "Resume Project Work", "results.md", "Do not push, commit, upload results"):
        assert required in text


def test_commercial_materials_make_consistent_truthful_claims():
    combined = "\n".join(_read(rel) for rel in ("README.md", "ONE_PROMPT_TEST.md", "BETA_OFFER.md", "site/index.html"))
    for phrase in ("Safe Code Change", "Release Readiness", "Resume Project Work", "$49", "macOS", "automatic dependency installation"):
        assert phrase in combined
    for forbidden in ("paid users", "production ready", "guaranteed safe", "proven token savings"):
        assert forbidden not in combined.lower()


def test_results_template_has_required_privacy_and_commercial_fields():
    text = _read("RESULTS_TEMPLATE.md")
    for heading in ("# Environment", "# Installation", "# MCP", "# Safe Code Change", "# Environment Remediation", "# Release Readiness", "# Resume Project Work", "# Safety", "# Commercial Feedback", "# Consent"):
        assert heading in text
    assert "no automatic upload occurred" in text.lower()
    assert "credentials" in text


def test_landing_page_has_required_ctas_and_no_fake_social_proof():
    text = _read("site/index.html")
    for phrase in ("Professional engineering skills for AI coding agents.", "Try the safe sandbox", "View on GitHub", "$49 one-time", "No automatic dependency installation", "No hidden repository writes"):
        assert phrase in text
    for forbidden in ("testimonial", "customers", "users served", "limited slots"):
        assert forbidden not in text.lower()


def test_issue_templates_and_demo_are_present_and_sanitized():
    beta = _read(".github/ISSUE_TEMPLATE/beta-interest.yml")
    bug = _read(".github/ISSUE_TEMPLATE/bug_report.yml")
    demo = _read("DEMO_SCRIPT.md")
    assert "payment" in beta.lower()
    assert "API keys" in bug
    assert "source code" in bug
    assert "60–120" in demo
    assert "external-user" in demo and "evidence" in demo
