"""Local synthetic-only detector-to-policy demonstration; no network transport."""
from __future__ import annotations

import tempfile
from copy import copy
from pathlib import Path

from experiments.secure_context_policy_demo import Decision, decide
from skilllayer.runner.core import build_detect_secrets_artifacts


def analyze(text: str, *, restricted: bool = False) -> tuple[Decision, str | None]:
    """Return decision and transformed text only when local checks complete.

    This probe is not a security boundary or a general-purpose code sanitizer.
    """
    try:
        import tldextract
        tldextract.extract = tldextract.TLDExtract(suffix_list_urls=[], cache_dir=None)
        from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
        from presidio_analyzer.nlp_engine import NoOpNlpEngine
        from presidio_analyzer.predefined_recognizers import EmailRecognizer, PhoneRecognizer
        from presidio_anonymizer import AnonymizerEngine
        from presidio_anonymizer.entities import OperatorConfig

        registry = RecognizerRegistry(supported_languages=["en"])
        registry.add_recognizer(EmailRecognizer())
        registry.add_recognizer(PhoneRecognizer(supported_regions=["US"]))
        analyzer = AnalyzerEngine(registry=registry, nlp_engine=NoOpNlpEngine(
            models=[{"lang_code": "en", "model_name": "no-op"}]), supported_languages=["en"])

        def secrets(value: str) -> tuple[bool, bool]:
            with tempfile.TemporaryDirectory(prefix="secure-context-") as directory:
                Path(directory, "input.txt").write_text(value, encoding="utf-8")
                result = build_detect_secrets_artifacts(Path(directory))
                if (result.get("success") is not True or result.get("scan_complete") is not True
                        or not isinstance(result.get("findings"), list)
                        or result.get("scanned_files") != 1
                        or result.get("scanned_bytes") != len(value.encode("utf-8"))):
                    return False, False
                return True, bool(result["findings"])

        entities = ["EMAIL_ADDRESS", "PHONE_NUMBER"]
        complete, secret = secrets(text)
        findings = analyzer.analyze(text=text, entities=entities, language="en")
        decision = decide(detector_complete=complete, secret=secret, pii=bool(findings),
                          restricted=restricted, transformable=True)
        if decision != Decision.TRANSFORM:
            return decision, text if decision == Decision.ALLOW else None

        aliases: dict[tuple[str, str], str] = {}
        counts: dict[str, int] = {}
        operators = {}
        rewritten = []
        occupied: list[tuple[int, int]] = []
        for finding in findings:
            if not (0 <= finding.start < finding.end <= len(text)) or finding.entity_type not in entities:
                return Decision.BLOCK, None
            if any(finding.start < end and start < finding.end for start, end in occupied):
                return Decision.BLOCK, None
            occupied.append((finding.start, finding.end))
            key = (finding.entity_type, text[finding.start:finding.end])
            if key not in aliases:
                counts[finding.entity_type] = counts.get(finding.entity_type, 0) + 1
                aliases[key] = f"{finding.entity_type}_{counts[finding.entity_type]}"
            label = aliases[key]
            if f"<{label}>" in text:
                return Decision.BLOCK, None
            replacement = copy(finding)
            replacement.entity_type = label
            rewritten.append(replacement)
            operators[label] = OperatorConfig("replace", {"new_value": f"<{label}>"})
        transformed = AnonymizerEngine().anonymize(text=text, analyzer_results=rewritten, operators=operators).text
        complete, secret = secrets(transformed)
        remaining = analyzer.analyze(text=transformed, entities=entities, language="en")
        if not complete or secret or remaining:
            return Decision.BLOCK, None
        return Decision.TRANSFORM, transformed
    except Exception:
        # Do not expose detector error details, which may include input content.
        return Decision.BLOCK, None


def main() -> None:
    cases = {
        "clean": ("Calculate the total from three numbers.", False),
        "pii": ("Contact irina.smirnova@example.com or +1-202-555-0147.", False),
        "secret": ('api_key = "' + "abcXYZ0123456789" * 3 + '"', False),
        "restricted": ("Private business implementation", True),
    }
    for name, (text, restricted) in cases.items():
        decision, _ = analyze(text, restricted=restricted)
        print(f"{name}: {decision.value}")


if __name__ == "__main__":
    main()
