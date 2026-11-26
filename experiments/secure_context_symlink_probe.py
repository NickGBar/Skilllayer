"""Synthetic demonstration of why lexical path matching is not enforcement."""
from pathlib import Path
from tempfile import TemporaryDirectory

from experiments.secure_context_path_policy import decide_path


def main():
    with TemporaryDirectory(prefix="context-path-probe-") as directory:
        root = Path(directory)
        (root / "public").mkdir()
        (root / "private").mkdir()
        (root / "private" / "example.py").write_text("synthetic = True\n", encoding="utf-8")
        (root / "public" / "link").symlink_to(root / "private", target_is_directory=True)
        label = "public/link/example.py"
        lexical = decide_path(label, allowed=("public",), forbidden=("private",))
        real = (root / label).resolve()
        escaped = not real.is_relative_to((root / "public").resolve())
        print(f"lexical_decision={lexical.value} resolves_outside_allowed={escaped}")
        if lexical.value != "ALLOW" or not escaped:
            raise SystemExit("synthetic example did not demonstrate expected limitation")


if __name__ == "__main__":
    main()
