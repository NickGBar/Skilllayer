# Reversible context pseudonymization (synthetic probe)

**Status:** research probe on synthetic input. Not wired into Core, the gate or any
model route. Not a security boundary.

## What it does

Before context leaves the machine, `experiments/context_pseudonymize.py` replaces:

| What | How it is found | Replaced with |
|---|---|---|
| Client and project names you list | runs of word parts, so `Agrobot`, `AGROBOT`, `agro_bot`, `agro-bot`, `Agro.bot`, `AgrobotClient` all match; Cyrillic look-alike letters fold to Latin | `client_a_3f9c21b0` (your label + keyed hash) |
| Secrets | Core's secret patterns; only the value is replaced, not `token =` | `SECRET_5e0d77a1` |
| Email | pattern | `user_1b788a54@example.invalid` |
| Phone (+7 / 8, +1) | pattern | same layout, area code `000` |
| ИНН (after the word ИНН/INN), СНИЛС, card number | pattern **and** a valid checksum | same layout, new digits, **valid checksum** |
| Public IPv4 | Core's pattern, octets ≤ 255 | `10.x.y.z` |

Then it checks its own work and answers `ALLOW` (nothing to replace), `TRANSFORM`, or
`BLOCK` with reasons. The checks:

1. **Nothing sensitive is left:** the output is scanned again with the same detectors.
2. **Exact reversibility:** restoring the output gives back the input byte for byte.
3. **Python logic unchanged:** both versions are parsed, and every identifier and string
   value is replaced by its first-seen index. The two trees must be identical. That holds
   exactly when the change was a consistent, one-to-one renaming: same structure, same
   operators and numbers, same equalities between names and strings. Renaming a keyword
   or `None` is caught.
4. **Independent second opinion (text only):** Core's secret scanner must find nothing in
   the output. It reads `token = expr` in code as a secret, so it is not used for Python.
5. **Fail closed:** invisible or bidi characters, unparseable Python, a secret or personal
   value outside a string or comment in Python, an unterminated private key, or a token
   that already occurs in the input → `BLOCK`.

`restore()` puts the originals back into the model's answer, including inside new names
the model built from a token (`client_a_3f9c21b0_total_v2` → `agrobot_total_v2`), and
lists token-shaped strings it never issued.

## Why a keyed hash, and why not encryption with a public key

The model never needs to decrypt anything. It needs two properties: the same value always
becomes the same token, so relations in the code survive, and the token says nothing about
the value. A keyed HMAC gives both. The key and the token table stay on the machine.

A plain hash of `Agrobot` would not do: anyone can hash a list of candidate company names
and compare. A public key given to the model is useless to it: it could only encrypt, and
ciphertext is long, unreadable and breaks identifiers.

## What it does not cover

- **Code logic itself.** An algorithm stays recognizable after renaming. Out of scope by
  decision (27 Nov 2026).
- **Names nobody listed.** People's names and addresses in free text need a language model
  to find, which would make the check non-deterministic. Only listed names are replaced.
- **Other formats:** phones other than +7/8 and +1, passports, bank accounts, IBAN, IPv6.
  Bare 10- or 12-digit numbers without the word ИНН/INN are left alone on purpose: one in
  ten random numbers passes the ИНН checksum.
- **Ordering and lengths.** Equality between values is preserved; alphabetical order and
  string lengths are not. Numeric literals in code are never changed.
- **Structure proof for other languages.** Text mode replaces and round-trips, but only
  Python gets the structure check.
- **Spellings beyond the folding rules:** separators other than `_ - . space`, homoglyphs
  outside the small Cyrillic table, deliberate obfuscation.
- **Each entity spelling gets its own token** (`Agrobot` and `AGROBOT` differ) so restore
  is exact; the shared label shows they are one client.

## Run

```sh
PYTHONPATH=src:. python -m pytest -q tests/test_context_pseudonymize.py
PYTHONPATH=src:. python experiments/context_pseudonymize_demo.py
```

The key in tests and the demo is a fixed synthetic value. Real use needs 32+ random bytes
kept locally and reused, so tokens stay stable across requests.
