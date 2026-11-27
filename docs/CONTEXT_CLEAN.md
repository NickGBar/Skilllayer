# Context cleaning

`skilllayer context clean` replaces listed client and project names, personal data and secrets
with keyed tokens before context goes to a model, checks its own result, and prints the cleaned
text. `skilllayer context restore` puts the originals back into the model's answer. Everything
stays on the machine. It is a convenience with checks, not a security boundary.

```bash
skilllayer context clean --in billing.py --entity client_a=Agrobot,Агробот > billing.clean.py
# … send billing.clean.py to the model, save its answer as answer.txt …
skilllayer context restore --in answer.txt > answer.restored.txt
```

A team can keep its list in a file: `--entities names.json` with
`{"entities": [{"label": "client_a", "aliases": ["Agrobot", "Агробот"]}]}`.

Exit codes: `0` cleaned (or nothing to clean), `2` blocked — nothing was written, the reasons are
on standard error — and `1` for bad input or configuration. `--json` prints the decision, the
counts and the output as JSON.

## What it does

Before context leaves the machine, the command replaces:

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
   that already occurs in the input → `BLOCK`. A `.py` file given with `--in` is treated as
   Python; `--python` and `--text` override that.

`context restore` puts the originals back into the model's answer, including inside new names
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

- **Code logic itself.** An algorithm stays recognizable after renaming.
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

## Key and token table

The first run creates a random 32-byte key in `~/.skilllayer/context/key`
(`$SKILLLAYER_CONTEXT_DIR` or `--home` to move it), readable by the owner only. The token table,
`vault.json` next to it, holds the originals and is also owner-only; it is written only after a
run passes all checks. Keep both if you want tokens to stay the same across requests; delete both
to start over. Neither is ever sent anywhere.
