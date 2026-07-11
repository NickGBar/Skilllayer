#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from skilllayer.mcp_config import build_config, render_config
from skilllayer.mcp_server import mcp_tool_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate ready-to-copy SkillLayer MCP config snippets.")
    parser.add_argument("--output", type=Path, default=None, help="Optional path to write the generated JSON.")
    args = parser.parse_args(argv)

    config = build_config(tool_count=mcp_tool_count())
    text = render_config(config)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
