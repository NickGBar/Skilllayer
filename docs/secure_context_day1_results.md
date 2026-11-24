# Secure Context PoC — Day 1 results

**Run date:** 24 November 2026

**Planned sprint day:** Wednesday, 25 November 2026

**Repository snapshot:** `481697442167c47a070e9d9665e8049306fed239` (`origin/main`)
**Scope:** synthetic-only local detector comparison. No customer data, real credentials, provider keys, or product code changes.

## Result

The first day now has a reproducible technical result: a fixed synthetic matrix was run through SkillLayer Core's existing regex scanner and Gitleaks 8.30.1; Presidio's email/US-phone pattern recognizers were separately checked for detection and masking. This is a smoke profile, not a representative security benchmark and not evidence of production coverage.

## Secret scanner comparison

The matrix contains nine positive files (Anthropic-shaped key, OpenAI-shaped key, AWS access ID, AWS secret, GitHub token, generic API key, curl bearer header, plain bearer header, and private-key block) and two negative controls (PII only and clean code). Every value is generated deterministically from seed `20261124`, written only to a temporary directory, and removed on exit. Reports print rule identifiers and filenames only, never matched values.

| Scanner | Positive files detected | Negative files flagged | Findings | Notes |
|---|---:|---:|---:|---|
| SkillLayer Core `DetectSecretPatternsWorkflow` | 9/9 | 0/2 | 11 | Some files match more than one rule; Anthropic-shaped fixture matched only broad `generic_token`, not a provider-specific rule. |
| Gitleaks 8.30.1, bundled default config | 7/9 | 0/2 | 7 | Missed the standalone AWS secret assignment and plain bearer header; detected the curl bearer-header form. |

Core rule IDs observed: `aws_access_key`, `aws_secret_key`, `bearer_token`, `generic_api_key`, `generic_token`, `github_token`, `openai_api_key`, `private_key_block`. Gitleaks rule IDs observed: `anthropic-api-key`, `aws-access-token`, `curl-auth-header`, `generic-api-key`, `github-pat`, `private-key`.

The fixtures were adjusted to match documented detector shapes (including the configured Anthropic key length and AWS access-key alphabet). Counts therefore describe this declared matrix only; they must not be interpreted as recall or precision for real secrets.

## Presidio PII probe

Temporary isolated environment: `presidio-analyzer==2.2.365`, `presidio-anonymizer==2.2.364`. The probe uses only `EmailRecognizer`, `PhoneRecognizer` for US numbers, and `NoOpNlpEngine`; it does not load an NLP model or evaluate name recognition.

- Detected synthetic email and US phone: 2 entities.
- Masked both originals (`<EMAIL_1>`, `<PHONE_1>`); original values absent after transform.
- Preserved unrelated date text.
- Clean control produced 0 findings.
- Public-suffix lookup is configured to use the package's local snapshot; the probe does not fetch it at runtime.

This verifies a narrow local transformation path. It does not establish coverage for names, non-US phone formats, identifiers, source-code secrets, or business-sensitive logic. Presidio's own documentation also cautions that detection cannot guarantee all sensitive data is found.

## Reproduction

From the repository root, with the project's source available on `PYTHONPATH`:

```sh
PYTHONPATH=src python3 experiments/secure_context_day1/scan_matrix.py \
  --gitleaks /path/to/gitleaks-8.30.1/gitleaks
```

The Presidio probe runs in an isolated environment with the two pinned packages:

```sh
python experiments/secure_context_day1/presidio_probe.py
```

Both scripts generate synthetic data in memory or a temporary directory, report aggregate metadata, and exit nonzero only on execution/expected-output failures. The Gitleaks binary used for this run was the official Darwin ARM64 v8.30.1 release; its downloaded archive SHA-256 was verified against the published release checksum: `b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5`. The binary and Python environment are under `/private/tmp`, not installed into the project or system.

The experiment regression tests check deterministic fixture generation, the declared positive/negative matrix counts, scan completeness, and absence of generated token-shaped strings from console output. Targeted result: **2 passed** with the project's configured Python interpreter. The full repository suite was not run for this isolated research change.

## Component and license notes

- [Gitleaks v8.30.1 release](https://github.com/gitleaks/gitleaks/releases/tag/v8.30.1); project is MIT-licensed. The run used its bundled default configuration; no rules were added or changed.
- [Presidio installation documentation](https://microsoft.github.io/presidio/installation/) and [Presidio project](https://github.com/microsoft/presidio); project is MIT-licensed. Only the analyzer and anonymizer libraries were installed into a temporary virtual environment.
- These checks do not choose LiteLLM or another gateway. No gateway route was exercised and no customer key was used.

## Decision and limitations

Keep the hybrid boundary. Reuse detector outputs as inputs to SkillLayer's own policy; do not treat any detector's successful scan as proof that a request is safe. Presidio's narrow recognizers are useful for a demonstrable email/phone transformation. Gitleaks catches useful standard secret forms but the default configuration misses cases in this small declared matrix; SkillLayer's scanner caught all matrix cases but includes broad generic matches and duplicate findings. Neither result supports a production security claim.

Next sprint work should depend on Thursday's interview (26 November): confirm which data classes matter, the actual gateway and existing detector stack, allowed processing location, and one concrete request path. No Core integration or customer-data test is authorized by this synthetic probe. Dataset Pipeline remains deferred; Pre-Review Loop remains a release gate.
