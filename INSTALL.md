# SkillLayer Install Guide

This guide is for first-time local installation from GitHub.

SkillLayer is a local CLI/MCP workflow layer. It does not start a network
service, does not upload telemetry, and does not require paid LLM APIs for the
basic tester flow.

## Prerequisites

- **Python >= 3.10 required**
- `git`
- `python -m venv` support
- `pip`

Recommended:

- macOS for the verified first tester flow
- Linux and Windows PowerShell are unverified runtime paths
- pytest installed only if you want pytest-based smoke tests or pytest-only project validation
- MCP extra installed if you want Codex/Cursor/Claude Code MCP integration
- Playwright only if you want `BrowserSmokeWorkflow`

## Minimal Path

This is a manual, step-by-step alternative for contributors who want an
editable install. Most testers should use **One-Command Setup** below
instead — it is the verified, hardened path.

**Python >= 3.10 required.**

```bash
git clone https://github.com/NickGBar/Skilllayer.git
cd Skilllayer
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[mcp]" --no-build-isolation
python -m skilllayer tester-check
```

Recommended first workflow path after install:

```text
install -> verify_install -> inspect_repo -> find function -> run tests
```

Run SkillLayer commands from the activated `.venv`. If the environment is not
active, use the repository-local interpreter explicitly:

```bash
.venv/bin/python -m skilllayer tester-check
```

## One-Command Setup

From the repository root:

```bash
./scripts/install.sh
```

The script:

- creates `.venv` if missing
- upgrades `pip`, `setuptools`, and `wheel` inside `.venv`
- installs SkillLayer (non-editable) with the required MCP runtime extra
- fails immediately (exit 1) if that MCP-extra install fails — it does not
  silently fall back to a reduced CLI-only install
- runs `python -m skilllayer doctor --json` to verify the install
- prints next steps

It does not require `sudo` and does not overwrite user files without warning.

## Install Options

Editable install for testers and contributors:

```bash
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[mcp]" --no-build-isolation
```

Normal local install:

```bash
python -m pip install . --no-build-isolation
```

Optional extras:

```bash
python -m pip install -e ".[mcp]" --no-build-isolation
python -m pip install -e ".[browser]" --no-build-isolation
python -m pip install -e ".[dev]" --no-build-isolation
```

Base install (`python -m pip install -e .`) is enough for CLI workflows.
MCP clients require the MCP extra. If `doctor` reports `mcp_sdk_available` as
an optional warning, run:

```bash
python -m pip install -e ".[mcp]" --no-build-isolation
```

Browser smoke checks require Playwright browser assets:

```bash
python -m playwright install chromium
```

## Verify Installation

```bash
./scripts/verify_install.sh
```

Or run the commands manually:

```bash
python -m skilllayer tester-check
python -m skilllayer workflows
python -m skilllayer doctor
```

If `python -m skilllayer tester-check` reports missing dependencies even after
installation, verify that you are using `.venv/bin/python` or that
`source .venv/bin/activate` is active in the current shell:

```bash
.venv/bin/python -m skilllayer tester-check
.venv/bin/python -m skilllayer workflows
.venv/bin/python -m skilllayer doctor
```

## Generate MCP Config

```bash
python scripts/generate_mcp_config.py
```

This prints ready-to-copy JSON snippets for Codex, generic MCP clients, Claude
Code, and Cursor. Codex and Claude Code have been validated end-to-end,
including real stdio protocol handshake, `tools/list` discovery, and cleanup
after project-scoped `.mcp.json` removal. Cursor is partially validated from
the SkillLayer side: config generation, local MCP server startup, and tool
schemas are checked, but Cursor UI discovery still needs a manual client check.

Cursor setup guide:

```text
docs/CURSOR_SETUP.md
```

## macOS Notes

- Use `python3` if `python` points to Python 2 or an older Python.
- If shell activation fails, run `. .venv/bin/activate`.
- If Playwright Chromium is missing, browser smoke checks may fall back to a
  static backend.

## Linux Notes

- Install `python3-venv` if `python3 -m venv .venv` fails.
- Use the repository-local `.venv/bin/python` when in doubt.

## Windows Notes

**Windows runtime: UNVERIFIED. Scripts: STATICALLY_REVIEWED_ONLY.**
The PowerShell scripts have not been executed in this release-candidate
environment; do not treat the steps below as a runtime verification claim.

Windows testers should use PowerShell from the repository root:

```powershell
.\scripts\install.ps1
.\scripts\verify_install.ps1
```

SkillLayer requires Python 3.10 or newer. The installer validates the created
`.venv` and fails immediately if Windows selected an older interpreter.

Manual PowerShell equivalent:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[mcp]" --no-build-isolation
python -m skilllayer tester-check
```

If `py -3` selects Python 3.9, use an explicit supported launcher instead:

```powershell
py -3.11 -m venv .venv
# or
py -3.10 -m venv .venv
```

No admin rights are required. MCP command paths in generated examples may need
manual adjustment on Windows.

If the MCP extra fails but CLI workflows install successfully, SkillLayer can
still run local CLI workflows. MCP validation needs:

```powershell
python -m pip install -e ".[mcp]" --no-build-isolation
```

## Troubleshooting

See:

```text
docs/TROUBLESHOOTING.md
```

Common checks:

```bash
python --version
python -m pip --version
python -m skilllayer doctor --json
python -m skilllayer workflows --json
```
