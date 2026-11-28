"""Reversible, verified cleaning of context before it is sent to a model.

Replaces listed client and project names, a few personal-data formats and secrets with
keyed tokens, checks the result, and restores the originals in the model's answer. For
Python it also proves that the program changed only by a consistent, one-to-one renaming
of names and string values. Anything unexpected blocks.

Not a security boundary and not a general-purpose anonymizer: docs/CONTEXT_CLEAN.md lists
what is and is not covered. The key and the token table never leave the machine.
"""
from __future__ import annotations

import ast
import hashlib
import hmac
import io
import json
import os
import re
import secrets as _secrets
import tempfile
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

from .runner.core import _SECRET_PATTERNS, build_detect_secrets_artifacts

MAX_INPUT_CHARS = 1_000_000
ALLOW, TRANSFORM, BLOCK = "ALLOW", "TRANSFORM", "BLOCK"

# ---------------------------------------------------------------- vault

_LABEL = re.compile(r"[a-z][a-z0-9_]{0,15}")


class Vault:
    """Keyed, injective token table. The key and the table never leave the machine.

    A token is an HMAC of the original under a local secret key, so the same value
    always gets the same token (relations in the code survive) while nobody without
    the key can test a guessed name against it, as they could with a plain hash.
    """

    def __init__(self, key: bytes):
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("key must be at least 32 random bytes")
        self._key = key
        self._forward: dict[tuple[str, str], str] = {}
        self.reverse: dict[str, str] = {}

    def token(self, kind: str, original: str, render) -> str:
        known = self._forward.get((kind, original))
        if known is not None:
            return known
        for attempt in range(32):
            digest = hmac.new(self._key, f"{kind}\0{attempt}\0{original}".encode(), hashlib.sha256).digest()
            candidate = render(digest, original)
            if candidate not in self.reverse and candidate != original:
                self._forward[(kind, original)] = candidate
                self.reverse[candidate] = original
                return candidate
        raise ValueError("no collision-free token")

    def entries(self) -> list[dict[str, str]]:
        """The table, for saving next to the key: kind, original, token."""
        return [{"kind": kind, "original": original, "token": token}
                for (kind, original), token in self._forward.items()]

    def load(self, entries: list[dict[str, str]]) -> None:
        """Restore a saved table. Raises ValueError for a table that is not one-to-one."""
        for entry in entries:
            kind, original, token = entry["kind"], entry["original"], entry["token"]
            if not all(isinstance(v, str) and v for v in (kind, original, token)):
                raise ValueError("vault entry fields must be non-empty strings")
            if self.reverse.get(token, original) != original or self._forward.get((kind, original), token) != token:
                raise ValueError("vault entries are not one-to-one")
            self._forward[(kind, original)] = token
            self.reverse[token] = original


def _hex8(digest: bytes) -> str:
    return digest.hex()[:8]


def _digits(digest: bytes, count: int) -> list[int]:
    stream = int.from_bytes(digest, "big")
    out = []
    for _ in range(count):
        stream, digit = divmod(stream, 10)
        out.append(digit)
    return out


def _reformat(original: str, new_digits: list[int]) -> str:
    """Put new digits into the original's layout: separators and length stay."""
    it = iter(new_digits)
    return "".join(str(next(it)) if ch.isdigit() else ch for ch in original)


# ---------------------------------------------------------------- checksums

def _inn_ok(d: list[int]) -> bool:
    def check(ws, ds):
        return sum(w * x for w, x in zip(ws, ds)) % 11 % 10
    if len(d) == 10:
        return check([2, 4, 10, 3, 5, 9, 4, 6, 8], d) == d[9]
    if len(d) == 12:
        return (check([7, 2, 4, 10, 3, 5, 9, 4, 6, 8], d) == d[10]
                and check([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8], d) == d[11])
    return False


def _inn_fix(d: list[int]) -> list[int]:
    def check(ws, ds):
        return sum(w * x for w, x in zip(ws, ds)) % 11 % 10
    if len(d) == 10:
        return d[:9] + [check([2, 4, 10, 3, 5, 9, 4, 6, 8], d)]
    d = d[:10] + [check([7, 2, 4, 10, 3, 5, 9, 4, 6, 8], d)]
    return d + [check([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8], d)]


def _snils_check(d: list[int]) -> int:
    total = sum(x * (9 - i) for i, x in enumerate(d[:9]))
    if total > 101:
        total %= 101
    return 0 if total in (100, 101) else total


def _snils_ok(d: list[int]) -> bool:
    return len(d) == 11 and _snils_check(d) == d[9] * 10 + d[10]


def _luhn_ok(d: list[int]) -> bool:
    total = 0
    for i, x in enumerate(reversed(d)):
        if i % 2:
            x *= 2
            x -= 9 if x > 9 else 0
        total += x
    return total % 10 == 0


def _luhn_fix(d: list[int]) -> list[int]:
    for last in range(10):
        if _luhn_ok(d[:-1] + [last]):
            return d[:-1] + [last]
    raise AssertionError("unreachable")


def _only_digits(text: str) -> list[int]:
    return [int(ch) for ch in text if ch.isdigit()]


# ---------------------------------------------------------------- renderers

def _render_secret(digest, _original):
    return f"SECRET_{_hex8(digest)}"


def _render_email(digest, _original):
    return f"user_{_hex8(digest)}@example.invalid"


def _render_phone(digest, original):
    # Keep the country prefix, zero the area code, fill the rest from the digest.
    prefix = 1 if original.lstrip().startswith(("+7", "+1", "8")) else 0
    count = len(_only_digits(original))
    new = _only_digits(original)[:prefix] + [0, 0, 0] + _digits(digest, count - prefix - 3)
    return _reformat(original, new)


def _render_inn(digest, original):
    return _reformat(original, _inn_fix(_digits(digest, len(_only_digits(original)))))


def _render_snils(digest, original):
    body = _digits(digest, 9)
    check = _snils_check(body + [0, 0])
    return _reformat(original, body + [check // 10, check % 10])


def _render_card(digest, original):
    count = len(_only_digits(original))
    return _reformat(original, _luhn_fix([0] + _digits(digest, count - 1)))


def _render_ip(digest, _original):
    a, b, c = digest[0], digest[1], max(1, min(254, digest[2]))
    return f"10.{a}.{b}.{c}"


def _name_renderer(label):
    return lambda digest, _original: f"{label}_{_hex8(digest)}"


_RENDERERS = {"secret": _render_secret, "email": _render_email, "phone": _render_phone,
              "inn": _render_inn, "snils": _render_snils, "card": _render_card, "ip": _render_ip}

# ---------------------------------------------------------------- detectors

_EMAIL = re.compile(r"(?<![\w.+%-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
_PHONE = re.compile(
    r"(?<![\w+])(?:"
    r"(?:\+7[ \-]?|8[ \-]|8(?=\())\(?\d{3}\)?[ \-]?\d{3}[ \-]?\d{2}[ \-]?\d{2}"
    r"|\+1[ \-]?\(?\d{3}\)?[ \-]?\d{3}[ \-]?\d{4}"
    r")(?!\d)")
_INN = re.compile(r"(?i)(?:инн|\binn)\D{0,12}?(?<!\d)(\d{12}|\d{10})(?!\d)")
_SNILS = re.compile(r"(?<![\d-])(\d{3}-\d{3}-\d{3}[ -]\d{2})(?![\d-])")
_CARD = re.compile(r"(?<![\d-])(\d{4}(?:[ -]\d{4}){3})(?![\d-])")
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿]")

_ASSIGNED_VALUE_RULES = {"aws_secret_key", "generic_api_key", "generic_token"}
_KEY_END = re.compile(r"-----END[^\n-]*PRIVATE KEY-----")


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    kind: str          # "secret", "email", ..., or "name:<label>"
    priority: int      # 0 secret, 1 personal data, 2 configured name


def _secret_spans(text: str, python: bool = False) -> tuple[list[Span], list[str]]:
    """Secret values to replace. In Python, an unquoted `token = expr` is code, not a value."""
    spans, problems = [], []
    for rule in _SECRET_PATTERNS:
        name = rule["name"]
        for m in rule["pattern"].finditer(text):
            s, e = m.span()
            if name == "ip_address":
                if all(0 <= int(octet) <= 255 for octet in m.group(0).split(".")):
                    spans.append(Span(s, e, "ip", 1))
                continue
            if name in _ASSIGNED_VALUE_RULES:
                if python and not m.group(1):
                    continue
                s = text.index("=", s, e) + 1
                while text[s] in " \t":
                    s += 1
                if m.group(1):
                    s, e = s + 1, e - 1
            elif name == "bearer_token":
                s = re.compile(r"Bearer\s+").match(text, s).end()
            elif name == "database_url":
                s, e = text.index("://", s, e) + 3, e - 1
            elif name == "private_key_block":
                end = _KEY_END.search(text, e)
                if end is None:
                    problems.append("unterminated_private_key")
                    continue
                e = end.end()
            spans.append(Span(s, e, "secret", 0))
    return spans, problems


def _personal_spans(text: str) -> list[Span]:
    spans = [Span(*m.span(), "email", 1) for m in _EMAIL.finditer(text)]
    spans += [Span(*m.span(), "phone", 1) for m in _PHONE.finditer(text)]
    spans += [Span(*m.span(1), "inn", 1) for m in _INN.finditer(text) if _inn_ok(_only_digits(m.group(1)))]
    spans += [Span(*m.span(1), "snils", 1) for m in _SNILS.finditer(text) if _snils_ok(_only_digits(m.group(1)))]
    spans += [Span(*m.span(1), "card", 1) for m in _CARD.finditer(text) if _luhn_ok(_only_digits(m.group(1)))]
    return spans


# Latin look-alikes of Cyrillic letters, folded only for comparison: a name typed
# with one swapped letter still matches, and spans stay character-aligned.
_FOLD = str.maketrans("аеорсухкмтвнАЕОРСУХКМТВН", "aeopcyxkmtbhaeopcyxkmtbh")
_PART = re.compile(r"[A-ZА-ЯЁ]+(?![a-zа-яё])|[A-ZА-ЯЁ]?[a-zа-яё]+|[0-9]+")
_GAP = re.compile(r"[_\-\s.]?")


def _fold(text: str) -> str:
    return text.translate(_FOLD).lower()


@dataclass(frozen=True)
class Entity:
    label: str
    aliases: tuple[str, ...]


def _alias_key(alias: str) -> str:
    return "".join(_fold(p) for p in _PART.findall(alias))


def _name_spans(text: str, offset: int, entities: tuple[Entity, ...]) -> list[Span]:
    """Configured names as runs of word parts: snake, kebab, camel, spaced, dotted."""
    parts = [(m.start(), m.end(), _fold(m.group())) for m in _PART.finditer(text)]
    keys = sorted(((_alias_key(a), ent.label) for ent in entities for a in ent.aliases),
                  key=lambda kv: -len(kv[0]))
    spans, i = [], 0
    while i < len(parts):
        matched = False
        for key, label in keys:
            joined, j = "", i
            while j < len(parts) and len(joined) < len(key):
                if j > i and _GAP.fullmatch(text[parts[j - 1][1]:parts[j][0]]) is None:
                    break
                joined += parts[j][2]
                j += 1
            if joined == key:
                spans.append(Span(offset + parts[i][0], offset + parts[j - 1][1], f"name:{label}", 2))
                i, matched = j, True
                break
        if not matched:
            i += 1
    return spans


# ---------------------------------------------------------------- python structure

def _python_layout(src: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """(string/comment regions, identifier spans outside them), as character offsets."""
    starts = [0]
    for line in src.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))

    def at(pos):
        return starts[pos[0] - 1] + pos[1]

    fstart = getattr(tokenize, "FSTRING_START", None)
    fend = getattr(tokenize, "FSTRING_END", None)
    regions, names, depth, open_at = [], [], 0, 0
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == fstart:
            if depth == 0:
                open_at = at(tok.start)
            depth += 1
        elif tok.type == fend:
            depth -= 1
            if depth == 0:
                regions.append((open_at, at(tok.end)))
        elif depth:
            continue  # inside an f-string: the whole literal is one region
        elif tok.type in (tokenize.STRING, tokenize.COMMENT):
            regions.append((at(tok.start), at(tok.end)))
        elif tok.type == tokenize.NAME:
            names.append((at(tok.start), at(tok.end)))
    return regions, names


def canonical_dump(src: str) -> str:
    """AST with every identifier and string value replaced by its first-seen index.

    Two programs have equal canonical dumps exactly when one is the other under a
    consistent, one-to-one renaming of names and string values: same structure,
    same operators, same numbers, same equalities between names and strings.
    """
    tree = ast.parse(src)
    seen: dict[str, str] = {}

    def canon(value: str) -> str:
        return seen.setdefault(value, f"v{len(seen)}")

    for node in ast.walk(tree):
        for name, value in ast.iter_fields(node):
            if isinstance(node, ast.Constant) and name == "kind":
                continue
            if isinstance(value, str):
                setattr(node, name, canon(value))
            elif isinstance(value, bytes):
                setattr(node, name, canon("bytes:" + value.hex()))
            elif isinstance(value, list) and value and all(isinstance(v, str) for v in value):
                setattr(node, name, [canon(v) for v in value])
    return ast.dump(tree, include_attributes=False)


# ---------------------------------------------------------------- pipeline

@dataclass
class Result:
    decision: str
    output: str | None
    replaced: dict[str, int] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    structure_checked: bool = False


def _choose(spans: list[Span]) -> list[Span]:
    chosen: list[Span] = []
    for span in sorted(spans, key=lambda s: (s.priority, s.start, -s.end)):
        if all(span.end <= c.start or span.start >= c.end for c in chosen):
            chosen.append(span)
    return sorted(chosen, key=lambda s: s.start)


def _render(kind: str):
    return _name_renderer(kind.split(":", 1)[1]) if kind.startswith("name:") else _RENDERERS[kind]


def _leftovers(text: str, vault: Vault, entities: tuple[Entity, ...], python: bool) -> list[str]:
    secrets, problems = _secret_spans(text, python)
    found = [s.kind for s in secrets + _personal_spans(text) if text[s.start:s.end] not in vault.reverse]
    found += [s.kind for s in _name_spans(text, 0, entities)]
    return found + problems


def _core_scanner_clean(text: str) -> bool:
    with tempfile.TemporaryDirectory(prefix="pseudonymize-") as directory:
        Path(directory, "context.txt").write_text(text, encoding="utf-8")
        result = build_detect_secrets_artifacts(Path(directory))
    # Octet-checked IPs are this probe's own business; Core flags any dotted quad.
    findings = result.get("findings")
    if not isinstance(findings, list):
        return False
    others = [f for f in findings if not (isinstance(f, dict) and f.get("pattern_name") == "ip_address")]
    return (result.get("success") is True and result.get("scan_complete") is True
            and others == [] and result.get("scanned_files") == 1)


def pseudonymize(text: str, *, vault: Vault, entities: tuple[Entity, ...], python: bool = False,
                 secrets: str = "transform") -> Result:
    """Replace, verify, and return ALLOW / TRANSFORM, or BLOCK with reasons."""
    def block(*reasons):
        return Result(BLOCK, None, reasons=list(reasons))

    if (not isinstance(text, str) or len(text) > MAX_INPUT_CHARS or secrets not in ("transform", "block")
            or not all(isinstance(e, Entity) and _LABEL.fullmatch(e.label)
                       and all(len(_alias_key(a)) >= 3 for a in e.aliases) for e in entities)):
        return block("invalid_input_or_configuration")
    if _INVISIBLE.search(text):
        return block("invisible_or_bidi_characters")

    secret_spans, problems = _secret_spans(text, python)
    if problems:
        return block(*problems)
    candidates = secret_spans + _personal_spans(text)
    if secrets == "block" and secret_spans:
        return block("secret_present")

    if python:
        try:
            before = canonical_dump(text)
            regions, names = _python_layout(text)
        except (SyntaxError, tokenize.TokenError, ValueError):
            return block("python_parse_failed")
        inside = lambda s: any(a <= s.start and s.end <= b for a, b in regions)  # noqa: E731
        if not all(inside(s) for s in candidates):
            return block("sensitive_value_outside_string_or_comment")
        for a, b in regions + names:
            candidates += _name_spans(text[a:b], a, entities)
    else:
        candidates += _name_spans(text, 0, entities)

    spans = _choose(candidates)
    out, last, replaced, used = [], 0, {}, []
    try:
        for s in spans:
            token = vault.token(s.kind, text[s.start:s.end], _render(s.kind))
            out += [text[last:s.start], token]
            last = s.end
            used.append(token)
            kind = s.kind.split(":", 1)[0]
            replaced[kind] = replaced.get(kind, 0) + 1
    except ValueError:
        return block("token_collision")
    output = "".join(out) + text[last:]

    reasons = []
    if any(token in text for token in used):
        reasons.append("token_already_in_input")
    if _leftovers(output, vault, entities, python):
        reasons.append("sensitive_value_left_after_transform")
    # The Core scanner reads `token = expr` in code as a secret, so as an independent
    # second opinion it is used for text only; Python relies on the re-scan above.
    if not python and not _core_scanner_clean(output):
        reasons.append("core_scanner_flags_output")
    if restore(output, vault)[0] != text:
        reasons.append("round_trip_mismatch")
    structure_checked = False
    if python:
        try:
            structure_checked = canonical_dump(output) == before
        except SyntaxError:
            pass
        if not structure_checked:
            reasons.append("python_structure_changed")
    if reasons:
        return block(*reasons)
    return Result(TRANSFORM if spans else ALLOW, output, replaced,
                  structure_checked=structure_checked)


_TOKEN_SHAPE = re.compile(r"\b(?:[a-z][a-z0-9_]{0,15}_[0-9a-f]{8}\b|SECRET_[0-9a-f]{8}\b"
                          r"|user_[0-9a-f]{8}@example\.invalid)")


def restore(text: str, vault: Vault) -> tuple[str, list[str]]:
    """Put originals back into a model's answer; report token-shaped strings we never issued."""
    if not vault.reverse:
        return text, sorted({m.group() for m in _TOKEN_SHAPE.finditer(text)})
    alternatives = []
    for token in sorted(vault.reverse, key=len, reverse=True):
        escaped = re.escape(token)
        # Format-preserving numbers must not match inside longer digit runs.
        alternatives.append(rf"(?<![\d.]){escaped}(?![\d])" if token[0].isdigit() or token[0] == "+" else escaped)
    pattern = re.compile("|".join(alternatives))
    unknown = sorted({m.group() for m in _TOKEN_SHAPE.finditer(pattern.sub(" ", text))})
    return pattern.sub(lambda m: vault.reverse[m.group()], text), unknown


# ---------------------------------------------------------------- local storage

CONTEXT_DIR_ENV = "SKILLLAYER_CONTEXT_DIR"
_KEY_FILE, _VAULT_FILE = "key", "vault.json"


def context_dir() -> Path:
    """Per-user directory for the key and the token table, outside any repository."""
    return Path(os.environ[CONTEXT_DIR_ENV]) if os.environ.get(CONTEXT_DIR_ENV) else Path.home() / ".skilllayer" / "context"


def load_vault(directory: Path, *, create: bool) -> Vault:
    """The vault under ``directory``; with ``create``, a new random key on first use (mode 0600)."""
    key_path = directory / _KEY_FILE
    if not key_path.exists():
        if not create:
            raise ValueError(f"no key in {directory}: nothing was cleaned with it yet")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_secrets.token_hex(32) + "\n")
    vault = Vault(bytes.fromhex(key_path.read_text(encoding="utf-8").strip()))
    table = directory / _VAULT_FILE
    if table.exists():
        data = json.loads(table.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("entries"), list):
            raise ValueError(f"{table} is not a token table this version can read")
        vault.load(data["entries"])
    return vault


def save_vault(directory: Path, vault: Vault) -> None:
    """Write the token table atomically, readable by the owner only. It holds the originals."""
    tmp = directory / (_VAULT_FILE + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "entries": vault.entries()}, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, directory / _VAULT_FILE)


def parse_entity(spec: str) -> Entity:
    """``label=Name[,Other name]`` from the command line."""
    label, _, names = spec.partition("=")
    return _entity(label.strip(), [n.strip() for n in names.split(",") if n.strip()])


def load_entities(path: Path) -> tuple[Entity, ...]:
    """``{"entities": [{"label": "client_a", "aliases": ["Northwind", "Нортвинд"]}]}``."""
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("entities") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"{path}: expected an object with an 'entities' list")
    return tuple(_entity(item.get("label"), item.get("aliases")) for item in items if isinstance(item, dict))


def _entity(label, aliases) -> Entity:
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise ValueError(f"label {label!r}: use 1-16 lowercase letters, digits or _ , starting with a letter")
    if (not isinstance(aliases, list) or not aliases
            or not all(isinstance(a, str) and len(_alias_key(a)) >= 3 for a in aliases)):
        raise ValueError(f"label {label!r}: give at least one name of 3 or more letters or digits")
    return Entity(label, tuple(aliases))
