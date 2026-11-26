"""Show one synthetic file before and after pseudonymization, and the restored answer."""
from experiments.context_pseudonymize import Entity, Vault, pseudonymize, restore

DEMO_KEY = b"synthetic-demo-key-not-for-real-use"  # real use: 32+ random bytes kept locally

SOURCE = '''# Agrobot billing
AGROBOT_TOKEN = "ghp_A1b2C3d4E5f6G7h8J9k0L1m2N3o4P5q6R7s8T9u0"  # synthetic


class AgrobotInvoice:
    """Sent to ivan.petrov@agrobot.example, +7 (912) 345-67-89, ИНН 7707083893."""

    def agrobot_total(self, items, client="Agrobot"):
        if client == "Agrobot":
            return sum(items) * 0.9
        return sum(items)
'''


def main():
    vault = Vault(DEMO_KEY)
    result = pseudonymize(SOURCE, vault=vault, entities=(Entity("client_a", ("Agrobot",)),), python=True)
    print(f"decision={result.decision.value} replaced={result.replaced} "
          f"structure_checked={result.structure_checked} reasons={result.reasons}")
    if result.output is None:
        raise SystemExit(1)
    print("\n--- sent to the model ---\n" + result.output)
    answer = result.output.replace("* 0.9", "* 0.85")  # stand-in for the model's edit
    restored, unknown = restore(answer, vault)
    print("--- model's answer, restored locally ---\n" + restored)
    print(f"unknown tokens: {unknown}")


if __name__ == "__main__":
    main()
