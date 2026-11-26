"""Synthetic lexical path policy, not a filesystem access-control boundary."""
from experiments.secure_context_policy_demo import Decision


def _valid(path: object) -> bool:
    if (not isinstance(path, str) or not path or path.startswith("/")
            or "\\" in path or ":" in path or any(ord(char) < 32 or ord(char) == 127 for char in path)):
        return False
    parts = path.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def decide_path(path: str, *, allowed: tuple[str, ...], forbidden: tuple[str, ...]) -> Decision:
    """Match exact file or path segment prefixes; forbidden takes precedence.

    All paths are POSIX-style relative identifiers. This does not inspect disk,
    resolve symlinks or authenticate who supplied the path.
    """
    if not _valid(path) or not isinstance(allowed, tuple) or not isinstance(forbidden, tuple):
        return Decision.BLOCK
    if not all(_valid(item) for item in (*allowed, *forbidden)):
        return Decision.BLOCK

    def matches(prefix: str) -> bool:
        return path == prefix or path.startswith(prefix + "/")

    if any(matches(item) for item in forbidden):
        return Decision.BLOCK
    return Decision.ALLOW if any(matches(item) for item in allowed) else Decision.BLOCK


def evaluate_file(text: str, binary: str, *, path: str, source: str, raw_policy: str,
                  allowed: tuple[str, ...], forbidden: tuple[str, ...]) -> tuple[Decision, str | None]:
    """Compose lexical path and content policy for a synthetic caller."""
    if decide_path(path, allowed=allowed, forbidden=forbidden) != Decision.ALLOW:
        return Decision.BLOCK, None
    from experiments.secure_context_policy_config import evaluate_with_policy
    return evaluate_with_policy(text, binary, source, raw_policy)


def evaluate_bundle(files: tuple[tuple[str, str], ...], binary: str, *, source: str,
                    raw_policy: str, allowed: tuple[str, ...],
                    forbidden: tuple[str, ...]) -> tuple[Decision, tuple[str, ...] | None]:
    """Synthetic atomic bundle; never return a partial allowed output."""
    if (not isinstance(files, tuple) or not 1 <= len(files) <= 20
            or any(not isinstance(item, tuple) or len(item) != 2
                   or not isinstance(item[0], str) or not isinstance(item[1], str) for item in files)):
        return Decision.BLOCK, None
    if len({path for path, _ in files}) != len(files):
        return Decision.BLOCK, None
    if any(decide_path(path, allowed=allowed, forbidden=forbidden) != Decision.ALLOW for path, _ in files):
        return Decision.BLOCK, None
    outputs = []
    transformed = False
    for path, text in files:
        decision, output = evaluate_file(text, binary, path=path, source=source, raw_policy=raw_policy,
                                         allowed=allowed, forbidden=forbidden)
        if decision in (Decision.BLOCK, Decision.LOCAL_ONLY):
            return decision, None
        if decision not in (Decision.ALLOW, Decision.TRANSFORM) or not isinstance(output, str):
            return Decision.BLOCK, None
        transformed |= decision == Decision.TRANSFORM
        outputs.append(output)
    # Aliases currently have per-file scope; they can collide across files.
    if transformed and len(files) > 1:
        return Decision.BLOCK, None
    return (Decision.TRANSFORM if transformed else Decision.ALLOW), tuple(outputs)
