"""Synthetic regression cases for the reversible pseudonymization probe."""
import hashlib
import unittest

from experiments.context_pseudonymize import (
    Decision, Entity, Vault, _inn_ok, _luhn_ok, _only_digits, _snils_ok, canonical_dump,
    pseudonymize, restore,
)

KEY = b"synthetic-test-key-0123456789abcdef"
CLIENT = (Entity("client_a", ("Agrobot", "Агробот")),)
GH = "ghp_A1b2C3d4E5f6G7h8J9k0L1m2N3o4P5q6R7s8T9u0"  # synthetic, not a credential

SOURCE = f'''# Agrobot billing, see AGRO-BOT notes
AGROBOT_TOKEN = "{GH}"


class AgrobotInvoice:
    """Sent to ivan.petrov@agrobot.example, +7 (912) 345-67-89."""

    def agrobot_total(self, items, client="Agrobot"):
        if client == "Agrobot" and items:
            return sum(items) * 0.9
        token = request.headers.get("Authorization")
        return f"{{client}}: {{sum(items)}} agro_bot"
'''


def run(text, *, python=False, key=KEY, **kw):
    vault = Vault(key)
    return pseudonymize(text, vault=vault, entities=CLIENT, python=python, **kw), vault


class PythonContextTests(unittest.TestCase):
    def test_names_personal_data_and_secret_are_replaced_and_restored(self):
        result, vault = run(SOURCE, python=True)
        self.assertEqual(result.decision, Decision.TRANSFORM, result.reasons)
        self.assertTrue(result.structure_checked)
        for leaked in ("grobot", "GROBOT", "agro_bot", "AGRO-BOT", "ivan.petrov", "912", GH):
            self.assertNotIn(leaked, result.output)
        self.assertEqual(restore(result.output, vault), (SOURCE, []))
        compile(result.output, "<pseudonymized>", "exec")

    def test_equal_values_stay_equal_and_different_spellings_stay_apart(self):
        result, _ = run('a = "Agrobot"\nb = "Agrobot"\nc = "agrobot"\n', python=True)
        values = [line.split(" = ")[1] for line in result.output.splitlines()]
        self.assertEqual(values[0], values[1])
        self.assertNotEqual(values[0], values[2])

    def test_code_reading_a_secret_is_not_a_secret(self):
        result, _ = run('token = request.headers.get("Authorization")\n', python=True)
        self.assertEqual(result.decision, Decision.ALLOW)

    def test_sensitive_value_outside_strings_blocks(self):
        result, _ = run(f"x = {GH}\n", python=True)
        self.assertEqual((result.decision, result.reasons),
                         (Decision.BLOCK, ["sensitive_value_outside_string_or_comment"]))

    def test_unparseable_python_blocks(self):
        self.assertEqual(run("def broken(:\n", python=True)[0].reasons, ["python_parse_failed"])

    def test_renaming_a_keyword_or_constant_is_caught_by_the_structure_check(self):
        for alias, source in (("return", "def f():\n    return 1\n"), ("None", "x = None\n")):
            result = pseudonymize(source, vault=Vault(KEY), entities=(Entity("bad", (alias,)),), python=True)
            self.assertEqual((result.decision, result.reasons), (Decision.BLOCK, ["python_structure_changed"]))

    def test_structure_check_tells_renaming_from_logic_change(self):
        self.assertEqual(canonical_dump("def f(a):\n    return a + 1\n"),
                         canonical_dump("def g(b):\n    return b + 1\n"))
        self.assertNotEqual(canonical_dump("def f(a):\n    return a + 1\n"),
                            canonical_dump("def f(a):\n    return a - 1\n"))
        # A many-to-one renaming changes which names are equal, so it is not accepted.
        self.assertNotEqual(canonical_dump("x = a\ny = b\n"), canonical_dump("x = a\ny = a\n"))


class NameMatchingTests(unittest.TestCase):
    def test_spellings_separators_and_scripts(self):
        text = "Agrobot AGROBOT agro_bot agro-bot Agro.bot AgroBot Агробот Аgrobot AgrobotClient"
        result, _ = run(text)
        self.assertEqual(result.replaced, {"name": 9})

    def test_parts_of_other_words_are_left_alone(self):
        result, _ = run("agronomy robotics agrobotanical")
        self.assertEqual((result.decision, result.output), (Decision.ALLOW, "agronomy robotics agrobotanical"))

    def test_invisible_characters_block(self):
        self.assertEqual(run("Agro​bot")[0].reasons, ["invisible_or_bidi_characters"])


class PersonalDataTests(unittest.TestCase):
    def test_replacements_keep_format_and_checksums(self):
        text = ("ИНН 7707083893; INN: 500100732259; СНИЛС 112-233-445 95; "
                "card 4111 1111 1111 1111; tel 8 (912) 345-67-89; ip 8.8.8.8")
        result, vault = run(text)
        self.assertEqual(result.decision, Decision.TRANSFORM, result.reasons)
        self.assertEqual(result.replaced, {"inn": 2, "snils": 1, "card": 1, "phone": 1, "ip": 1})
        out = {orig: tok for tok, orig in vault.reverse.items()}
        self.assertTrue(_inn_ok(_only_digits(out["7707083893"])))
        self.assertTrue(_inn_ok(_only_digits(out["500100732259"])))
        self.assertTrue(_snils_ok(_only_digits(out["112-233-445 95"])))
        self.assertTrue(_luhn_ok(_only_digits(out["4111 1111 1111 1111"])))
        self.assertRegex(out["8 (912) 345-67-89"], r"^8 \(000\) \d{3}-\d{2}-\d{2}$")
        self.assertTrue(out["8.8.8.8"].startswith("10."))
        self.assertEqual(restore(result.output, vault)[0], text)

    def test_numbers_without_context_or_valid_checksum_are_left_alone(self):
        text = ("id 7707083893; ИНН 7707083894; snils-like 112-233-445 96; "
                "card 4111 1111 1111 1112; version 999.1.1.1")
        self.assertEqual(run(text)[0].decision, Decision.ALLOW)

    def test_restore_does_not_touch_longer_numbers(self):
        result, vault = run("ip 8.8.8.8")
        token = result.output.split("ip ")[1]
        answer = f"{token} vs 1{token} vs {token}5"
        self.assertEqual(restore(answer, vault)[0], f"8.8.8.8 vs 1{token} vs {token}5")


class SecretTests(unittest.TestCase):
    def test_secret_value_replaced_and_core_scanner_clean(self):
        result, vault = run(f'log: password="{GH}" via Bearer abcdefghijklmnopqrstuvwxyz0123')
        self.assertEqual(result.decision, Decision.TRANSFORM, result.reasons)
        self.assertEqual(result.replaced, {"secret": 2})
        self.assertIn('password="SECRET_', result.output)

    def test_block_policy_and_unterminated_key(self):
        self.assertEqual(run(GH, secrets="block")[0].reasons, ["secret_present"])
        self.assertEqual(run("-----BEGIN RSA PRIVATE KEY-----\nabc")[0].reasons, ["unterminated_private_key"])


class VaultTests(unittest.TestCase):
    def test_deterministic_per_key_and_not_a_plain_hash(self):
        a, b = run("Agrobot")[0].output, run("Agrobot")[0].output
        c = run("Agrobot", key=b"another-synthetic-key-0123456789ab")[0].output
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotIn(hashlib.sha256(b"Agrobot").hexdigest()[:8], a)

    def test_short_key_rejected(self):
        with self.assertRaises(ValueError):
            Vault(b"short")

    def test_token_already_in_input_blocks(self):
        vault = Vault(KEY)
        first = pseudonymize("Agrobot", vault=vault, entities=CLIENT).output
        result = pseudonymize(f"Agrobot and {first}", vault=Vault(KEY), entities=CLIENT)
        self.assertEqual(result.decision, Decision.BLOCK)
        self.assertIn("token_already_in_input", result.reasons)

    def test_restore_model_answer_and_report_invented_tokens(self):
        result, vault = run("def agrobot_total(): ...", python=True)
        token = result.output.split("def ")[1].split("_total")[0]
        answer = f"Rename to {token}_total_v2; also see client_a_deadbeef."
        self.assertEqual(restore(answer, vault),
                         ("Rename to agrobot_total_v2; also see client_a_deadbeef.", ["client_a_deadbeef"]))


if __name__ == "__main__":
    unittest.main()
