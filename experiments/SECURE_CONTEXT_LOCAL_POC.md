# Secure Context local policy probe

This synthetic-only experiment connects the existing SkillLayer secret scanner,
Presidio email and US/RU-phone recognizers, and a small deterministic policy.
It has no gateway, egress control, customer integration, or production guarantees.
It does not detect confidential business logic and cannot establish that code
is safe to share. Only the tested request path could ever be controlled.

Create a temporary virtual environment and install `presidio-analyzer==2.2.365`
and `presidio-anonymizer==2.2.364`. From the repository root run:

```sh
PYTHONPATH=.:src python -m experiments.secure_context_local_poc
PYTHONPATH=.:src python -m experiments.secure_context_matrix
PYTHONPATH=.:src python -m experiments.secure_context_route_probe
PYTHONPATH=.:src python -m unittest discover -s tests -p test_secure_context_local_poc.py -v
```

The experiment prints only scenario names and decisions. It creates temporary
files for the existing scanner; operating-system temporary storage must be
considered sensitive. Use synthetic input only. An incomplete scanner result,
secret finding, failed dependency, failed transformation, or remaining detected
PII blocks the operation. Restricted material returns `LOCAL_ONLY`; no external
request is made. An `ALLOW` decision means only that these narrow checks did not
find an issue. It is not proof of safety.

Presidio uses pattern recognizers for email and US/RU phone numbers only, with a
no-op NLP engine and local public suffix data; no names or business logic are
detected. The secret scanner is regex-based and can miss secrets. The local
policy is a demonstration, not an integration with SkillLayer Core. Policy
precedence: incomplete scan or detected secret blocks; restricted material stays
local; supported PII is transformed and scanned again; otherwise allow.

Observed with Python 3.13 and the versions above: clean `ALLOW`, email/phone
`TRANSFORM`, secret `BLOCK`, restricted `LOCAL_ONLY`. Repeated exact PII values
now share a placeholder within a request; distinct values receive different
placeholders. This does not resolve aliases or identity across requests.
The fixed 20-case matrix matched 20 expected decisions; one local run took
1.29 seconds including the first import. These examples are not a statistical
sample. A separate fake-transport probe sent the clean and transformed values
to an in-memory list and made zero simulated sends for blocked/local-only cases.
There is no real transport or interception in that experiment.
Full suite, real customer data, Gitleaks integration, network interception,
latency, and real-world false-positive/false-negative rates were not evaluated.
