"""Tests for DetectSecretPatternsWorkflow."""
from __future__ import annotations

import io
import re
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skilllayer.router.cascade import SkillRouter
from skilllayer.runner.core import (
    _SECRET_PATTERNS,
    _is_binary_file,
    _is_test_fixture_path,
    _load_gitignore_patterns,
    _matches_gitignore,
    build_detect_secrets_artifacts,
)

# ---------------------------------------------------------------------------
# Fake secrets used in tests — deliberately invalid, non-functional values
# ---------------------------------------------------------------------------

_ANT_KEY = "sk-ant-" + "a" * 24           # anthropic_api_key (7 + 24 = 31)
_OAI_KEY = "sk-" + "b" * 48              # openai_api_key (3 + 48 = 51)
_AWS_AK = "AKIA" + "A" * 16              # aws_access_key (20 chars)
_AWS_SK_LINE = "aws_secret_access_key=" + "c" * 40  # aws_secret_key (no space)
_PRIV_KEY = "-----BEGIN RSA PRIVATE KEY-----"
_GH_TOKEN = "ghp_" + "d" * 36            # github_token (4 + 36 = 40)
_BEARER = "Bearer " + "e" * 20           # bearer_token
_TOKEN_LINE = 'token = "supersecretvalue12345678"'  # generic_token (>16 chars)
_API_KEY_LINE = 'api_key = "myverylongapikey123456"'  # generic_api_key (>16)
_PUBLIC_IP = "8.8.8.8"                   # ip_address (non-private)
_DB_URL = "postgres://user:s3cr3tpass@db.example.com/mydb"  # database_url


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(tmp_path: Path, rel: str, content: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def _scan(tmp_path: Path) -> dict:
    return build_detect_secrets_artifacts(tmp_path)


def _findings_with(result: dict, pattern_name: str) -> list[dict]:
    return [f for f in result["findings"] if f["pattern_name"] == pattern_name]


# ---------------------------------------------------------------------------
# TestSecretPatterns — verify each pattern compiles and matches expected input
# ---------------------------------------------------------------------------

class TestSecretPatterns:
    def test_anthropic_key_pattern(self):
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "anthropic_api_key")
        assert pat["pattern"].search(_ANT_KEY)

    def test_anthropic_key_requires_20_chars(self):
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "anthropic_api_key")
        short = "sk-ant-" + "a" * 19
        assert not pat["pattern"].search(short)
        long_ = "sk-ant-" + "a" * 20
        assert pat["pattern"].search(long_)

    def test_openai_key_pattern(self):
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "openai_api_key")
        assert pat["pattern"].search(_OAI_KEY)

    def test_openai_key_requires_48_chars(self):
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "openai_api_key")
        short = "sk-" + "b" * 47
        assert not pat["pattern"].search(short)
        exact = "sk-" + "b" * 48
        assert pat["pattern"].search(exact)

    def test_anthropic_key_does_not_match_openai_pattern(self):
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "openai_api_key")
        # sk-ant-... has a hyphen after 'ant' which breaks the 48-char sequence
        assert not pat["pattern"].search(_ANT_KEY)

    def test_aws_access_key_pattern(self):
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "aws_access_key")
        assert pat["pattern"].search(_AWS_AK)

    def test_aws_secret_key_pattern(self):
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "aws_secret_key")
        assert pat["pattern"].search(_AWS_SK_LINE)

    def test_aws_secret_key_pattern_matches_quoted_value(self):
        # Regression test: the real-world form is quoted, e.g.
        # AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        # (this is AWS's own well-known example secret key, not a real credential).
        # The old regex required an unquoted 40-char run immediately after '=',
        # so it never matched this — the most common way secrets actually
        # appear in source (assigned as a string literal).
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "aws_secret_key")
        planted_double = 'AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"'
        planted_single = "AWS_SECRET_ACCESS_KEY = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'"
        assert pat["pattern"].search(planted_double)
        assert pat["pattern"].search(planted_single)

    def test_aws_secret_key_pattern_rejects_mismatched_quotes(self):
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "aws_secret_key")
        mismatched = "aws_secret_access_key = \"" + "c" * 40 + "'"
        assert not pat["pattern"].search(mismatched)

    def test_private_key_block_pattern(self):
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "private_key_block")
        assert pat["pattern"].search(_PRIV_KEY)
        assert pat["pattern"].search("-----BEGIN EC PRIVATE KEY-----")
        assert pat["pattern"].search("-----BEGIN ENCRYPTED PRIVATE KEY-----")
        assert pat["pattern"].search("-----BEGIN OPENSSH PRIVATE KEY-----")

    def test_github_token_pattern(self):
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "github_token")
        assert pat["pattern"].search(_GH_TOKEN)
        assert pat["pattern"].search("gho_" + "x" * 36)
        assert pat["pattern"].search("ghu_" + "x" * 36)
        assert pat["pattern"].search("ghs_" + "x" * 36)
        assert pat["pattern"].search("ghr_" + "x" * 36)

    def test_bearer_token_pattern(self):
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "bearer_token")
        assert pat["pattern"].search(_BEARER)

    def test_generic_token_pattern(self):
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "generic_token")
        assert pat["pattern"].search(_TOKEN_LINE)

    def test_generic_token_pattern_matches_unquoted_value(self):
        # Same quoted-vs-unquoted gap class as aws_secret_key, inverted: this
        # pattern used to require quotes and missed the unquoted form.
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "generic_token")
        assert pat["pattern"].search("token = supersecretvalue12345678")
        assert pat["pattern"].search("password = anotherlongsecretvalue")

    def test_generic_api_key_pattern(self):
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "generic_api_key")
        assert pat["pattern"].search(_API_KEY_LINE)
        assert pat["pattern"].search('api_secret = "averylongapivalue12345"')

    def test_generic_api_key_pattern_matches_unquoted_value(self):
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "generic_api_key")
        assert pat["pattern"].search("api_key = myverylongapikey123456")
        assert pat["pattern"].search("api_secret = averylongapivalue12345")

    def test_ip_address_excludes_private_ranges(self):
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "ip_address")
        # Private ranges — should NOT match
        assert not pat["pattern"].search("127.0.0.1")
        assert not pat["pattern"].search("10.0.0.1")
        assert not pat["pattern"].search("192.168.1.1")
        assert not pat["pattern"].search("172.16.0.1")
        assert not pat["pattern"].search("172.31.255.255")
        assert not pat["pattern"].search("0.0.0.0")
        # Public range — SHOULD match
        assert pat["pattern"].search("8.8.8.8")
        assert pat["pattern"].search("1.1.1.1")

    def test_database_url_pattern(self):
        pat = next(p for p in _SECRET_PATTERNS if p["name"] == "database_url")
        assert pat["pattern"].search(_DB_URL)
        assert pat["pattern"].search("mysql://root:pass@localhost/db")
        assert pat["pattern"].search("mongodb://admin:secret123@mongo.example.com/db")
        assert pat["pattern"].search("redis://user:redispass@cache.example.com")


# ---------------------------------------------------------------------------
# TestIsTestFixturePath
# ---------------------------------------------------------------------------

class TestIsTestFixturePath:
    def test_test_file_detected(self):
        assert _is_test_fixture_path("tests/test_something.py")

    def test_test_file_in_subdir(self):
        assert _is_test_fixture_path("src/tests/test_something.py")

    def test_non_test_file(self):
        assert not _is_test_fixture_path("src/main.py")

    def test_file_named_test_without_prefix(self):
        assert not _is_test_fixture_path("src/testing_utils.py")

    def test_file_with_test_in_name_not_in_tests_dir(self):
        assert not _is_test_fixture_path("src/test_runner.py")

    def test_windows_path_separator(self):
        assert _is_test_fixture_path("tests\\test_something.py")


# ---------------------------------------------------------------------------
# TestIsBinaryFile
# ---------------------------------------------------------------------------

class TestIsBinaryFile:
    def test_text_file_not_binary(self, tmp_path):
        f = tmp_path / "file.py"
        f.write_text("print('hello')")
        assert not _is_binary_file(f)

    def test_file_with_null_byte_is_binary(self, tmp_path):
        f = tmp_path / "binary.bin"
        f.write_bytes(b"hello\x00world")
        assert _is_binary_file(f)

    def test_empty_file_not_binary(self, tmp_path):
        f = tmp_path / "empty.py"
        f.write_text("")
        assert not _is_binary_file(f)


# ---------------------------------------------------------------------------
# TestGitignoreHelpers
# ---------------------------------------------------------------------------

class TestGitignoreHelpers:
    def test_load_returns_empty_when_no_gitignore(self, tmp_path):
        patterns = _load_gitignore_patterns(tmp_path)
        assert patterns == []

    def test_load_skips_comments_and_blank_lines(self, tmp_path):
        gi = tmp_path / ".gitignore"
        gi.write_text("# comment\n\n*.pyc\n.venv/\n")
        patterns = _load_gitignore_patterns(tmp_path)
        assert patterns == ["*.pyc", ".venv/"]

    def test_matches_extension_pattern(self):
        assert _matches_gitignore("build/output.pyc", ["*.pyc"])

    def test_matches_directory_pattern(self):
        assert _matches_gitignore(".venv/lib/site.py", [".venv"])

    def test_no_match_when_pattern_differs(self):
        assert not _matches_gitignore("src/main.py", ["*.pyc"])

    def test_slash_pattern_matches_full_path(self):
        assert _matches_gitignore(".skilllayer/baseline.json", [".skilllayer/baseline.json"])


# ---------------------------------------------------------------------------
# TestDetectAnthropicKey — critical severity
# ---------------------------------------------------------------------------

class TestDetectAnthropicKey:
    def test_detected_as_critical(self, tmp_path):
        _write(tmp_path, "src/config.py", f'KEY = "{_ANT_KEY}"\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "anthropic_api_key")
        assert len(findings) == 1
        assert findings[0]["severity"] == "critical"

    def test_line_number_correct(self, tmp_path):
        _write(tmp_path, "src/config.py", f"# line 1\nKEY = '{_ANT_KEY}'\n")
        result = _scan(tmp_path)
        findings = _findings_with(result, "anthropic_api_key")
        assert findings[0]["line"] == 2

    def test_file_path_relative(self, tmp_path):
        _write(tmp_path, "src/config.py", f'KEY = "{_ANT_KEY}"\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "anthropic_api_key")
        assert findings[0]["file"] == "src/config.py"


# ---------------------------------------------------------------------------
# TestDetectOpenAIKey — critical severity
# ---------------------------------------------------------------------------

class TestDetectOpenAIKey:
    def test_detected_as_critical(self, tmp_path):
        _write(tmp_path, "src/llm.py", f'api_key = "{_OAI_KEY}"\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "openai_api_key")
        assert len(findings) >= 1
        assert findings[0]["severity"] == "critical"


# ---------------------------------------------------------------------------
# TestDetectAWSKey — critical severity
# ---------------------------------------------------------------------------

class TestDetectAWSKey:
    def test_access_key_detected(self, tmp_path):
        _write(tmp_path, "deploy/creds.sh", f"AWS_ACCESS_KEY_ID={_AWS_AK}\n")
        result = _scan(tmp_path)
        findings = _findings_with(result, "aws_access_key")
        assert len(findings) == 1
        assert findings[0]["severity"] == "critical"

    def test_secret_key_detected(self, tmp_path):
        _write(tmp_path, "deploy/creds.sh", f"{_AWS_SK_LINE}\n")
        result = _scan(tmp_path)
        findings = _findings_with(result, "aws_secret_key")
        assert len(findings) == 1
        assert findings[0]["severity"] == "critical"

    def test_secret_key_detected_when_quoted(self, tmp_path):
        # End-to-end regression test for the exact form reported by the
        # external tester: a quoted assignment, which the pattern used to miss.
        _write(
            tmp_path,
            "config.py",
            'AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"\n',
        )
        result = _scan(tmp_path)
        findings = _findings_with(result, "aws_secret_key")
        assert len(findings) == 1
        assert findings[0]["severity"] == "critical"


# ---------------------------------------------------------------------------
# TestDetectPrivateKey — critical severity
# ---------------------------------------------------------------------------

class TestDetectPrivateKey:
    def test_rsa_private_key_detected(self, tmp_path):
        _write(tmp_path, "certs/key.pem", f"{_PRIV_KEY}\nMIIEpAIBAAKCAQEA...\n")
        result = _scan(tmp_path)
        findings = _findings_with(result, "private_key_block")
        assert len(findings) == 1
        assert findings[0]["severity"] == "critical"

    def test_ec_private_key_detected(self, tmp_path):
        _write(tmp_path, "certs/ec.pem", "-----BEGIN EC PRIVATE KEY-----\n")
        result = _scan(tmp_path)
        findings = _findings_with(result, "private_key_block")
        assert len(findings) == 1


# ---------------------------------------------------------------------------
# TestDetectGitHubToken — critical severity
# ---------------------------------------------------------------------------

class TestDetectGitHubToken:
    def test_github_pat_detected(self, tmp_path):
        _write(tmp_path, "ci/config.yml", f"token: {_GH_TOKEN}\n")
        result = _scan(tmp_path)
        findings = _findings_with(result, "github_token")
        assert len(findings) == 1
        assert findings[0]["severity"] == "critical"


# ---------------------------------------------------------------------------
# TestDetectGenericToken — high severity
# ---------------------------------------------------------------------------

class TestDetectGenericToken:
    def test_token_assignment_detected_as_high(self, tmp_path):
        _write(tmp_path, "src/auth.py", f"{_TOKEN_LINE}\n")
        result = _scan(tmp_path)
        findings = _findings_with(result, "generic_token")
        assert len(findings) == 1
        assert findings[0]["severity"] == "high"

    def test_password_assignment_detected(self, tmp_path):
        _write(tmp_path, "src/db.py", 'password = "averylongpassword12345"\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "generic_token")
        assert len(findings) == 1

    def test_short_value_not_detected(self, tmp_path):
        _write(tmp_path, "src/auth.py", 'token = "shortval"\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "generic_token")
        assert len(findings) == 0


# ---------------------------------------------------------------------------
# TestDetectBearerToken — high severity
# ---------------------------------------------------------------------------

class TestDetectBearerToken:
    def test_bearer_token_detected(self, tmp_path):
        _write(tmp_path, "src/client.py", f'headers = {{"Authorization": "{_BEARER}"}}\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "bearer_token")
        assert len(findings) == 1
        assert findings[0]["severity"] == "high"


# ---------------------------------------------------------------------------
# TestDetectIPAddress — medium severity
# ---------------------------------------------------------------------------

class TestDetectIPAddress:
    def test_public_ip_detected_as_medium(self, tmp_path):
        _write(tmp_path, "src/config.py", f'SERVER = "{_PUBLIC_IP}"\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "ip_address")
        assert len(findings) == 1
        assert findings[0]["severity"] == "medium"

    def test_localhost_not_detected(self, tmp_path):
        _write(tmp_path, "src/config.py", 'HOST = "127.0.0.1"\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "ip_address")
        assert len(findings) == 0

    def test_private_ip_not_detected(self, tmp_path):
        _write(tmp_path, "src/config.py", 'HOST = "192.168.1.100"\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "ip_address")
        assert len(findings) == 0


# ---------------------------------------------------------------------------
# TestDetectDatabaseURL — medium severity
# ---------------------------------------------------------------------------

class TestDetectDatabaseURL:
    def test_postgres_url_detected(self, tmp_path):
        _write(tmp_path, "src/db.py", f'DB = "{_DB_URL}"\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "database_url")
        assert len(findings) == 1
        assert findings[0]["severity"] == "medium"


# ---------------------------------------------------------------------------
# TestCleanRepo
# ---------------------------------------------------------------------------

class TestCleanRepo:
    def test_empty_repo_is_clean(self, tmp_path):
        result = _scan(tmp_path)
        assert result["clean"] is True
        assert result["findings"] == []
        assert result["findings_count"] == 0
        assert result["scan_complete"] is True
        assert result["status"] == "clear_for_supported_patterns"
        assert result["success"] is True

    def test_file_without_secrets_is_clean(self, tmp_path):
        _write(tmp_path, "src/main.py", "def hello():\n    return 'world'\n")
        result = _scan(tmp_path)
        assert result["clean"] is True

    def test_clean_sets_scanned_files(self, tmp_path):
        _write(tmp_path, "src/main.py", "x = 1\n")
        result = _scan(tmp_path)
        assert result["scanned_files"] >= 1


# ---------------------------------------------------------------------------
# TestMatchPreview — never expose full value
# ---------------------------------------------------------------------------

class TestMatchPreview:
    def test_preview_is_6_chars_plus_ellipsis(self, tmp_path):
        _write(tmp_path, "src/config.py", f'KEY = "{_ANT_KEY}"\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "anthropic_api_key")
        preview = findings[0]["match_preview"]
        assert preview.endswith("...")
        assert len(preview) == 9  # 6 chars + "..."

    def test_preview_does_not_contain_full_value(self, tmp_path):
        _write(tmp_path, "src/config.py", f'KEY = "{_ANT_KEY}"\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "anthropic_api_key")
        preview = findings[0]["match_preview"]
        assert preview != _ANT_KEY
        assert _ANT_KEY not in preview

    def test_preview_for_aws_access_key(self, tmp_path):
        _write(tmp_path, "creds.env", f"AWS_ACCESS_KEY_ID={_AWS_AK}\n")
        result = _scan(tmp_path)
        findings = _findings_with(result, "aws_access_key")
        preview = findings[0]["match_preview"]
        assert preview.endswith("...")
        assert _AWS_AK not in preview

    def test_preview_for_private_key(self, tmp_path):
        _write(tmp_path, "key.pem", f"{_PRIV_KEY}\n")
        result = _scan(tmp_path)
        findings = _findings_with(result, "private_key_block")
        preview = findings[0]["match_preview"]
        assert preview.endswith("...")
        assert _PRIV_KEY not in preview


# ---------------------------------------------------------------------------
# TestTestFixtureFlag
# ---------------------------------------------------------------------------

class TestTestFixtureFlag:
    def test_finding_in_test_file_flagged(self, tmp_path):
        _write(tmp_path, "tests/test_auth.py", f'KEY = "{_ANT_KEY}"\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "anthropic_api_key")
        assert len(findings) == 1
        assert findings[0]["likely_test_fixture"] is True

    def test_finding_in_src_file_not_flagged(self, tmp_path):
        _write(tmp_path, "src/config.py", f'KEY = "{_ANT_KEY}"\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "anthropic_api_key")
        assert len(findings) == 1
        assert findings[0]["likely_test_fixture"] is False

    def test_same_key_test_and_src_flagged_differently(self, tmp_path):
        _write(tmp_path, "tests/test_config.py", f'KEY = "{_ANT_KEY}"\n')
        _write(tmp_path, "src/config.py", f'KEY = "{_ANT_KEY}"\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "anthropic_api_key")
        assert len(findings) == 2
        fixture_flags = {f["file"]: f["likely_test_fixture"] for f in findings}
        assert fixture_flags["tests/test_config.py"] is True
        assert fixture_flags["src/config.py"] is False


# ---------------------------------------------------------------------------
# TestSkippedFiles
# ---------------------------------------------------------------------------

class TestSkippedFiles:
    def test_binary_file_skipped(self, tmp_path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"hello\x00world" + f"KEY = {_ANT_KEY}".encode())
        result = _scan(tmp_path)
        findings = _findings_with(result, "anthropic_api_key")
        assert len(findings) == 0
        assert result["skipped_files"] >= 1
        assert result["skipped_reasons"].get("binary", 0) >= 1
        # A binary/unsupported file was skipped -> the scan cannot vouch for
        # it, so it must be reported as incomplete, never a blanket clean.
        assert result["scan_complete"] is False
        assert result["status"] == "incomplete"
        assert result["clean"] is False
        assert result["success"] is True

    def test_large_file_skipped(self, tmp_path):
        f = tmp_path / "large.py"
        # Write just over 1MB
        f.write_text("x = 1\n" * 200_000)
        result = _scan(tmp_path)
        assert result["skipped_files"] >= 1
        assert result["skipped_reasons"].get("oversized", 0) >= 1
        assert result["scan_complete"] is False
        assert result["status"] == "incomplete"
        assert result["clean"] is False

    def test_large_file_with_secret_not_scanned(self, tmp_path):
        f = tmp_path / "large.py"
        padding = "x = 1\n" * 200_000
        f.write_text(padding + f'\nKEY = "{_ANT_KEY}"\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "anthropic_api_key")
        assert len(findings) == 0

    def test_venv_excluded(self, tmp_path):
        # .venv is a permanent, disclosed exclusion (VCS-internal/third-party
        # content), not an incidental gap -> does not mark the scan incomplete.
        _write(tmp_path, ".venv/lib/site-packages/pkg.py", f'KEY = "{_ANT_KEY}"\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "anthropic_api_key")
        assert len(findings) == 0
        assert result["skipped_files"] >= 1
        assert result["skipped_reasons"].get("excluded_dir", 0) >= 1
        assert result["scan_complete"] is True
        assert result["status"] == "clear_for_supported_patterns"
        assert result["clean"] is True

    def test_pycache_excluded(self, tmp_path):
        _write(tmp_path, "src/__pycache__/module.cpython-312.pyc.py", f'KEY = "{_ANT_KEY}"\n')
        result = _scan(tmp_path)
        findings = _findings_with(result, "anthropic_api_key")
        assert len(findings) == 0

    def test_gitignored_file_skipped(self, tmp_path):
        # The task's canonical example: a gitignored .env is exactly the kind
        # of file most likely to hold a real secret, so skipping it must
        # never be silently folded into "clean".
        gi = tmp_path / ".gitignore"
        gi.write_text("secrets.env\n")
        _write(tmp_path, "secrets.env", f"KEY={_ANT_KEY}\n")
        result = _scan(tmp_path)
        findings = _findings_with(result, "anthropic_api_key")
        assert len(findings) == 0
        assert result["skipped_files"] >= 1
        assert result["skipped_reasons"].get("gitignored", 0) >= 1
        assert result["scan_complete"] is False
        assert result["status"] == "incomplete"
        assert result["clean"] is False


# ---------------------------------------------------------------------------
# TestReturnStructure
# ---------------------------------------------------------------------------

class TestReturnStructure:
    def test_all_required_keys_present(self, tmp_path):
        result = _scan(tmp_path)
        assert "findings" in result
        assert "scanned_files" in result
        assert "scanned_bytes" in result
        assert "skipped_files" in result
        assert "skipped_bytes" in result
        assert "skipped_reasons" in result
        assert "scan_complete" in result
        assert "findings_count" in result
        assert "clean" in result
        assert "checked_at" in result
        assert "status" in result
        assert "success" in result
        assert "error_code" in result

    def test_checked_at_is_iso8601(self, tmp_path):
        import re as _re
        result = _scan(tmp_path)
        assert _re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", result["checked_at"])

    def test_findings_count_matches_findings_list(self, tmp_path):
        _write(tmp_path, "src/a.py", f'K1 = "{_ANT_KEY}"\n')
        _write(tmp_path, "src/b.py", f'K2 = "{_ANT_KEY}"\n')
        result = _scan(tmp_path)
        assert result["findings_count"] == len(result["findings"])

    def test_finding_has_all_fields(self, tmp_path):
        _write(tmp_path, "src/config.py", f'KEY = "{_ANT_KEY}"\n')
        result = _scan(tmp_path)
        f = result["findings"][0]
        assert "file" in f
        assert "line" in f
        assert "pattern_name" in f
        assert "severity" in f
        assert "match_preview" in f
        assert "likely_test_fixture" in f

    def test_workflow_name_correct(self, tmp_path):
        result = _scan(tmp_path)
        assert result["workflow"] == "DetectSecretPatternsWorkflow"


# ---------------------------------------------------------------------------
# TestZeroLLMCalls
# ---------------------------------------------------------------------------

class TestZeroLLMCalls:
    def test_no_llm_calls(self, tmp_path):
        _write(tmp_path, "src/config.py", f'KEY = "{_ANT_KEY}"\n')
        with patch("skilllayer.runner.core.LLMClient") as mock_llm:
            _scan(tmp_path)
        mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# TestRouter
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str | None:
        route = self.router.route(text)
        return route.task_type if route else None

    def test_scan_for_secrets(self):
        assert self._route("scan for secrets") == "detect_secrets"

    def test_check_for_leaked_credentials(self):
        assert self._route("check for leaked credentials") == "detect_secrets"

    def test_find_api_keys_in_code(self):
        assert self._route("find api keys in code") == "detect_secrets"

    def test_detect_secrets(self):
        assert self._route("detect secrets") == "detect_secrets"

    def test_are_there_hardcoded_passwords(self):
        assert self._route("are there any hardcoded passwords") == "detect_secrets"

    def test_check_for_sensitive_data(self):
        assert self._route("check for sensitive data") == "detect_secrets"

    def test_scan_for_leaked_api_key(self):
        assert self._route("scan for leaked api key") == "detect_secrets"

    def test_check_for_api_keys(self):
        assert self._route("check for api keys in repo") == "detect_secrets"

    def test_detect_secrets_in_repo(self):
        assert self._route("detect secrets in this repo") == "detect_secrets"

    def test_does_not_match_detect_processes(self):
        assert self._route("detect running processes") != "detect_secrets"

    def test_does_not_match_detect_dead_code(self):
        result = self._route("detect dead code")
        assert result != "detect_secrets"

    def test_does_not_match_scan_tests(self):
        result = self._route("scan for slow tests")
        assert result != "detect_secrets"


# ---------------------------------------------------------------------------
# TestCLIOutput
# ---------------------------------------------------------------------------

def _make_result(**overrides) -> dict:
    base = {
        "success": True,
        "workflow": "DetectSecretPatternsWorkflow",
        "findings": [],
        "scanned_files": 10,
        "skipped_files": 2,
        "findings_count": 0,
        "clean": True,
        "checked_at": "2026-01-01T00:00:00+00:00",
        "macro_sequence": ["ScanFiles", "MatchPatterns", "ClassifyFindings"],
        "validation_status": "not_applicable",
        "dry_run": False,
        "tool_calls": 0,
        "llm_calls": 0,
        "logs_path": None,
    }
    base.update(overrides)
    return base


class TestCLIOutput:
    def _run(self, result: dict) -> str:
        from skilllayer.cli import print_human_run
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_human_run(result)
        return buf.getvalue()

    def test_clean_output(self):
        out = self._run(_make_result())
        assert "clean: True" in out

    def test_findings_count_shown(self):
        out = self._run(_make_result())
        assert "findings: 0" in out

    def test_scanned_files_shown(self):
        out = self._run(_make_result())
        assert "scanned: 10" in out

    def test_skipped_files_shown(self):
        out = self._run(_make_result())
        assert "skipped: 2" in out

    def test_findings_grouped_by_severity(self):
        finding = {
            "file": "src/config.py",
            "line": 5,
            "pattern_name": "anthropic_api_key",
            "severity": "critical",
            "match_preview": "sk-ant...",
            "likely_test_fixture": False,
        }
        out = self._run(_make_result(findings=[finding], findings_count=1, clean=False))
        assert "critical:" in out
        assert "anthropic_api_key" in out

    def test_test_fixture_label(self):
        finding = {
            "file": "tests/test_auth.py",
            "line": 3,
            "pattern_name": "anthropic_api_key",
            "severity": "critical",
            "match_preview": "sk-ant...",
            "likely_test_fixture": True,
        }
        out = self._run(_make_result(findings=[finding], findings_count=1, clean=False))
        assert "[test fixture]" in out

    def test_checked_at_shown(self):
        out = self._run(_make_result())
        assert "checked_at" in out
