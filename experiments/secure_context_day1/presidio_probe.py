"""Offline synthetic probe of Presidio email/US-phone recognition and masking.

This intentionally uses only pattern recognizers and Presidio's NoOp NLP
engine. It does not evaluate names, free-form business logic, or other NER.

Run in an isolated environment with pinned Presidio packages:
    python experiments/secure_context_day1/presidio_probe.py
"""

from __future__ import annotations

import tldextract

# Use the packaged public-suffix snapshot. The probe is fully local and does
# not fetch the public suffix list at runtime.
tldextract.extract = tldextract.TLDExtract(suffix_list_urls=[], cache_dir=None)

from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import NoOpNlpEngine
from presidio_analyzer.predefined_recognizers import EmailRecognizer, PhoneRecognizer
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig


def main() -> int:
    registry = RecognizerRegistry(supported_languages=["en"])
    registry.add_recognizer(EmailRecognizer())
    registry.add_recognizer(PhoneRecognizer(supported_regions=["US"]))
    engine = NoOpNlpEngine(models=[{"lang_code": "en", "model_name": "no-op"}])
    analyzer = AnalyzerEngine(
        registry=registry,
        nlp_engine=engine,
        supported_languages=["en"],
    )

    original = (
        "email=irina.smirnova@example.com "
        "phone=+1-202-555-0147 value=03/04/2026"
    )
    findings = analyzer.analyze(
        text=original,
        entities=["EMAIL_ADDRESS", "PHONE_NUMBER"],
        language="en",
    )
    operators = {
        "EMAIL_ADDRESS": OperatorConfig("replace", {"new_value": "<EMAIL_1>"}),
        "PHONE_NUMBER": OperatorConfig("replace", {"new_value": "<PHONE_1>"}),
    }
    transformed = AnonymizerEngine().anonymize(
        text=original,
        analyzer_results=findings,
        operators=operators,
    ).text

    entity_types = sorted({finding.entity_type for finding in findings})
    original_values_absent = (
        "irina.smirnova@example.com" not in transformed
        and "+1-202-555-0147" not in transformed
    )
    unrelated_value_preserved = "value=03/04/2026" in transformed
    clean_findings = analyzer.analyze(
        text="request_id=run-2048 status=ready date=2026-11-25",
        entities=["EMAIL_ADDRESS", "PHONE_NUMBER"],
        language="en",
    )
    clean_control_clear = len(clean_findings) == 0
    print(f"presidio_entities={','.join(entity_types)}")
    print(f"presidio_findings={len(findings)}")
    print(f"original_values_absent={original_values_absent}")
    print(f"unrelated_value_preserved={unrelated_value_preserved}")
    print(f"clean_control_findings={len(clean_findings)}")
    print(f"transformed_context={transformed}")

    return 0 if original_values_absent and unrelated_value_preserved and clean_control_clear else 1


if __name__ == "__main__":
    raise SystemExit(main())
