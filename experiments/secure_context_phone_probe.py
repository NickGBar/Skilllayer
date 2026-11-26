"""Compare Presidio phone regions on synthetic data; print no input values."""
from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import NoOpNlpEngine
from presidio_analyzer.predefined_recognizers import PhoneRecognizer


def analyzer(regions):
    registry = RecognizerRegistry(supported_languages=["en"])
    registry.add_recognizer(PhoneRecognizer(supported_regions=regions))
    return AnalyzerEngine(registry=registry, nlp_engine=NoOpNlpEngine(
        models=[{"lang_code": "en", "model_name": "no-op"}]), supported_languages=["en"])


def main():
    detectors = {"us": analyzer(["US"]), "us_ru": analyzer(["US", "RU"])}
    cases = {
        "international_ru": "Телефон +7 999 123-45-67",
        "national_ru": "Телефон 8 (999) 123-45-67",
        "national_ru_no_format": "Позвонить 89991234567",
        "international_us": "Phone +1-202-555-0147",
        "national_us": "Phone (202) 555-0147",
        "clean_date": "Date 2026-11-26",
        "clean_number": "Build 12345678",
    }
    for label, value in cases.items():
        counts = {name: len(det.analyze(text=value, entities=["PHONE_NUMBER"], language="en"))
                  for name, det in detectors.items()}
        print(f"{label}: us={counts['us']} us_ru={counts['us_ru']}")


if __name__ == "__main__":
    main()
