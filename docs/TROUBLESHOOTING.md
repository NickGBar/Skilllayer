# SkillLayer Troubleshooting

## Wrong Python Version

SkillLayer requires Python 3.10 or newer.

Check:

```bash
python --version
python3 --version
```

Use `python3` if `python` points to an old interpreter.

## Virtual Environment Not Activated

Symptoms:

- `python -m skilllayer` says package not found
- commands work in one terminal but not another

Fix:

```bash
source .venv/bin/activate
python -m skilllayer tester-check
```

Or run with the venv Python directly:

```bash
.venv/bin/python -m skilllayer tester-check
```

## Package Not Found

Install from the repository root:

```bash
python -m pip install -e . --no-build-isolation
```

Then verify:

```bash
python -m skilllayer workflows --json
```

## Missing Command

If `skilllayer` is not on your shell path, use:

```bash
python -m skilllayer ...
```

The module command is the most reliable first-tester path.

## MCP Server Looks Frozen

MCP servers often wait silently for a client over stdio.

This can look like a frozen terminal, but it may be normal. To inspect schemas
without starting a long-running stdio session, run:

```bash
python -m skilllayer.mcp_server --list-tools
```

## MCP Config Path Problems

Generate fresh config snippets from the current checkout:

```bash
python scripts/generate_mcp_config.py
```

Make sure the config uses absolute paths to:

- `.venv/bin/python`
- the repository root as `cwd`

## Playwright Unavailable

`BrowserSmokeWorkflow` can use Playwright when installed:

```bash
python -m pip install -e ".[browser]"
python -m playwright install chromium
```

If Playwright or Chromium is unavailable, browser smoke may fall back to a
static backend for limited checks. The report should say which backend was used.

## Browser Backend Falls Back To Static

This means SkillLayer could not run a real browser check. Common causes:

- Playwright package missing
- Chromium browser assets missing
- target page unavailable
- local dev server not running

Fix the browser setup or run the target app, then try again.

## Permission Issues

SkillLayer does not require `sudo`.

If `.venv` or `runs/` is not writable:

```bash
ls -ld . .venv runs
python -m skilllayer doctor --json
```

Use a checkout owned by your user account.

## Still Stuck

Run:

```bash
python -m skilllayer tester-check --json
python -m skilllayer doctor --json
```

Open a bug report using:

```text
.github/ISSUE_TEMPLATE/bug_report.md
```

Do not include secrets, private code, raw diffs, screenshots, or unreviewed
telemetry exports.
