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
