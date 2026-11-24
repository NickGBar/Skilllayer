# Secure Context detector spike

This folder contains the synthetic-only detector comparison for sprint day 1.
It is a repeatable smoke profile, not a security benchmark and not a production
allow/deny policy. The scripts create credential-shaped test strings locally;
they never use customer data or real credentials.

## SkillLayer Core and optional Gitleaks

From the repository root, run the Core scan:

```sh
PYTHONPATH=src python3 experiments/secure_context_day1/scan_matrix.py
```

To include Gitleaks, install the official release for the current OS/architecture,
verify its archive against the checksum published with that release, then pass
the extracted binary path:

```sh
PYTHONPATH=src python3 experiments/secure_context_day1/scan_matrix.py \
  --gitleaks /path/to/gitleaks
```

The report contains counts, filenames, and rule IDs only. Gitleaks JSON is
redacted and kept inside a temporary directory that is removed at exit.

## Presidio email/phone probe

Use a throwaway virtual environment; Presidio is not a SkillLayer runtime
dependency and is intentionally absent from `pyproject.toml`:

```sh
python3 -m venv /tmp/skilllayer-presidio-probe
/tmp/skilllayer-presidio-probe/bin/python -m pip install \
  -r experiments/secure_context_day1/requirements-presidio.txt
/tmp/skilllayer-presidio-probe/bin/python \
  experiments/secure_context_day1/presidio_probe.py
```

The probe pins the analyzer and anonymizer versions, uses only Presidio's email
and US-phone pattern recognizers, and uses `NoOpNlpEngine`. It does not load an
NLP model, perform general named-entity recognition, or fetch the public suffix
list at runtime.

See [`docs/secure_context_day1_results.md`](../../docs/secure_context_day1_results.md)
for measured results, exact limitations, and the component release references.
