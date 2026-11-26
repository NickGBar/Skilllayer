# Synthetic hybrid policy probe

This experiment adds Gitleaks 8.30.1 before the existing local policy probe and
rescans transformed output. An unchanged `ALLOW` reuses the original scan; an
unexpected change with `ALLOW` blocks. No customer data or production integration is in
scope. The local probe uses Presidio's email/US/RU-phone patterns and the
SkillLayer Core regex scanner.

Gitleaks is passed input on stdin, not in command arguments. Its output is
redacted and stored in a temporary directory; stdout and stderr are suppressed.
The subprocess receives only a minimal PATH and locale, not the parent shell's
credentials or scanner configuration. It ignores inline
`gitleaks:allow` comments. A missing executable, timeout, malformed report,
unexpected exit status, or inconsistent report blocks. Successful scans do not
prove that the input contains no sensitive information. The Core scanner still
writes input into an OS temporary directory, so use synthetic data only.
The probe requires the Core scanner to report one scanned file and the exact
UTF-8 input byte count; oversized (>1 MiB) and binary inputs block in the
observed scanner implementation. This is a completeness check for this probe,
not proof that the scanner's patterns cover the contents. The hybrid entry
point also blocks NUL bytes, invalid Unicode and inputs over 1 MiB before
starting Gitleaks, so these inputs do not consume detector subprocess time.

Use the pinned Presidio dependencies from `secure_context_day1` and the official
Gitleaks 8.30.1 binary; verify its release checksum. For this run the Darwin ARM64
archive SHA-256 was `b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5`.

```sh
SECURE_CONTEXT_GITLEAKS=/absolute/path/to/gitleaks PYTHONPATH=.:src \
  python -m experiments.secure_context_hybrid_matrix
SECURE_CONTEXT_GITLEAKS=/absolute/path/to/gitleaks PYTHONPATH=.:src \
  python -m experiments.secure_context_utility_probe
SECURE_CONTEXT_GITLEAKS=/absolute/path/to/gitleaks PYTHONPATH=.:src \
  python -m experiments.secure_context_coverage_probe
SECURE_CONTEXT_GITLEAKS=/absolute/path/to/gitleaks PYTHONPATH=.:src \
  python -m experiments.secure_context_privacy_probe
SECURE_CONTEXT_GITLEAKS=/absolute/path/to/gitleaks PYTHONPATH=.:src \
  python -m experiments.secure_context_audit_probe
SECURE_CONTEXT_GITLEAKS=/absolute/path/to/gitleaks PYTHONPATH=.:src \
  python -m experiments.secure_context_hybrid_route_probe
SECURE_CONTEXT_GITLEAKS=/absolute/path/to/gitleaks PYTHONPATH=.:src \
  python -m experiments.secure_context_policy_route_probe
PYTHONPATH=.:src python -m experiments.secure_context_phone_probe
PYTHONPATH=.:src python -m experiments.secure_context_rules_probe
SECURE_CONTEXT_GITLEAKS=/absolute/path/to/gitleaks PYTHONPATH=.:src \
  python -m experiments.secure_context_latency_probe
SECURE_CONTEXT_GITLEAKS=/absolute/path/to/gitleaks PYTHONPATH=.:src \
  python -m experiments.secure_context_secret_probe
SECURE_CONTEXT_GITLEAKS=/absolute/path/to/gitleaks PYTHONPATH=.:src \
  python -m experiments.secure_context_size_probe
SECURE_CONTEXT_GITLEAKS=/absolute/path/to/gitleaks PYTHONPATH=.:src \
  python -m unittest discover -s tests -p 'test_secure_context*.py' -v
```

On the local macOS Python 3.13 run, 20/20 expected synthetic policy decisions
matched. End-to-end elapsed time was 10.98 seconds for 20 calls, including
initial imports. The earlier policy-only run took 1.29 seconds, but these are
single unpaired runs rather than a robust benchmark. Starting Gitleaks twice per
allowed/transformed request is a measurable cost and must be evaluated in the
customer's actual environment. In a six-pair alternating warm-run comparison,
the original hybrid path medians were 572.1 ms (clean), 565.7 ms (email), and
315.7 ms (secret). Skipping the duplicate scan only for byte-identical `ALLOW`
gave 319.0 ms, 561.3 ms, and 290.9 ms respectively in another six-pair run.
These are local exploratory timings, not production latency estimates.

On a separate run, synthetic requests of 1 KiB, 16 KiB, 64 KiB and 256 KiB
were allowed by the narrow scanner checks; a 1,048,577-byte input was blocked
before scanning. Warm cases in this one run took roughly 0.27–0.41 seconds;
the first took about 30 seconds while another Python process imported the same
dependencies. These single observations are not a concurrency or latency
benchmark, and the allowed inputs are not proven safe.

Five synthetic Python snippets containing email addresses matched their exact
manually specified masked output and remained parseable. This establishes only
the transformations in those examples, not general semantic preservation. An
exploratory coverage probe passed a person's name, a company name, an internal
hostname and business logic through unchanged with `ALLOW`. Initially it also
passed an `example.test` email unchanged; after adding explicit local suffixes,
that example is masked. This confirms that `ALLOW` is only the result of narrow supported
patterns and is unsuitable as permission to send real data.

On ten further hand-selected privacy examples, the original configuration
transformed two `.com` email forms, one international Russian phone example
and one US phone example. It allowed English and Russian names, a fictional
`.test` email, postal address, date of birth, and an internal customer ID
unchanged. The updated configuration also masks the `.test` example; all other
listed gaps remain. The
international phone observation does not establish coverage of Russian phone
formats. That initial observation used a US-only region configuration; the
current probe configures US and RU.
These examples expose gaps; they are not a measured real-world recall rate.

On seven phone-specific synthetic inputs, US+RU detected two national Russian
formats that US-only missed; both configurations detected the same three
international/US examples and returned no matches for the two clean controls.
This is a narrow fixture comparison, not a production false-positive estimate.

The email recognizer uses a local public suffix snapshot plus the explicit
synthetic suffixes `.test`, `.internal`, and `.local`; three corresponding
synthetic addresses are now covered by targeted tests. Other private suffixes
and false positives remain unmeasured.

In six additional synthetic secret-shaped examples, Gitleaks missed a plain
bearer header that the Core scanner blocked. A high-entropy fake value under
an unusual variable name passed both scanners. An encoded fake value was
blocked in this particular configuration. These observations illustrate why a
successful scan cannot classify arbitrary opaque strings as safe.

The current demonstration uses a consistent placeholder for each exact matched
value within a request, so two distinct values receive different placeholders.
Aliases are not stable between requests, semantic co-reference is not resolved,
and no reverse mapping is persisted. The probe does not measure task usefulness.
Overlapping detector findings and an input already containing a generated
placeholder block rather than silently merging identities. A fake transport
also blocks malformed allowed output; it never sends blocked/local-only data.
Business logic and implementation-sensitive code can remain exposed. No network
egress is intercepted. A fake in-memory transport does not verify an agent or
gateway integration. Customer-specific rules, actual gateway, acceptance
criteria, and real-world false negatives remain unknown.

The synthetic hybrid route probe forwards clean and masked samples into an
in-memory list, and forwards nothing for unknown sources, named restricted
terms, restricted inputs, or detected secrets. It makes zero network calls and
does not implement or validate interception of a real AI client.

The audit probe emits a fixed metadata-only event: decision, policy version,
synthetic scope, and explicit `network_attempted=false` and
`safety_verified=false`. It does not record raw input, output, matched values,
or claim an unknown cause for a block. It is an in-memory demonstration, not
a durable audit log or evidence of a production control.

The separate policy-event probe uses bounded reason codes for invalid policy,
unknown source, source block/local-only, transformation, or narrow checks
clearing. A content/detector block is deliberately generic: the experiment
does not yet provide reliable detector-specific explanations. Neither raw
input nor the supplied source identifier is included in the event.

`analyze_guarded` is a separate experiment entry point that blocks an unknown
source by default and requires explicit `source_authorized=True` before an
allow/transform path. A caller-provided boolean is only a simulation; a real
integration needs a trustworthy, independently managed source policy and an
enforced route. The lower-level `analyze_with_gitleaks` and matrix intentionally
measure detector behavior without that source policy and must not be used as an
authorization decision.

The guarded experiment also accepts an explicit tuple of restricted strings:
it blocks exact case-insensitive substrings before detector execution, and
rejects malformed or empty rules. This covers only exact configured strings;
it does not understand synonyms, encoded names, service architecture, or
business logic. The list and authorization flag are caller-controlled and are
not a trusted corporate policy in this experiment.

The path policy experiment accepts explicit relative POSIX path prefixes,
blocks unknown paths and `..`, absolute paths and malformed rules, and gives
forbidden prefixes priority. An allowed path still goes through source and
content checks. This is lexical matching on a caller-supplied string: it does
not resolve symlinks, authenticate the source or bind a path to file contents.
It is not a filesystem security boundary and must not authorize real egress.

The synthetic bundle probe never returns partial output if a path or content
check blocks. A one-file transformed bundle is supported; a multi-file bundle
that requires transformation is blocked because the current aliases are
scoped to each file and may collide. Clean multi-file examples may pass only
the narrow configured checks, which still cannot establish confidentiality.

The JSON policy probe has a fixed schema and rejects duplicate keys, unknown
fields, invalid actions and empty restricted terms. Unknown sources are
blocked. The synthetic policy example covers allow, local-only and block, and
an allowed source must still pass detector checks. Policy text and source IDs
are controlled by the caller; there is no integrity protection, authenticated
source identity or enforcement of the policy outside this local experiment.

An adversarial literal-rule probe uses a stubbed downstream detector so that
only this rule is measured: exact and uppercase spellings blocked, while five
variants using punctuation, whitespace, percent encoding, a Unicode hyphen,
or a zero-width character did not match. This deliberately demonstrates that
literal matching does not conceal the service or establish confidentiality.
