# Updating SkillLayer

Updates are explicit and read-only until you approve the command. SkillLayer never changes installation methods automatically. Preserve the current environment until the replacement passes doctor and an MCP smoke test.

## Repository-local `.venv`

From the SkillLayer checkout:

```bash
git pull --ff-only
.venv/bin/python -m pip install --upgrade -e .
.venv/bin/python -m skilllayer doctor
```

Restart the MCP client after updating. Project `.skilllayer/` state and unrelated MCP entries remain unchanged. Rollback is only guaranteed to a commit or tag you have retained locally.

## Pip installation from GitHub

Use the same interpreter that owns the current installation:

```bash
python -m pip install --upgrade git+https://github.com/NickGBar/Skilllayer.git
python -m skilllayer doctor
```

Restart the MCP client. Project state is preserved. Keep the prior environment or package version until verification succeeds.

## Editable developer install

From the checkout, retain editable mode explicitly:

```bash
python -m pip install --upgrade -e .
python -m skilllayer doctor
```

Rollback requires checking out a known commit and reinstalling editable mode. No automatic rollback is performed.

## Low-risk replacement pattern

For important work, create a new environment, install the target version there, verify doctor and MCP, then switch the project-scoped MCP command. Remove the old environment only after successful verification.

## Uninstall

Use `skilllayer uninstall --dry-run` first. The default uninstall removes only the SkillLayer MCP entry and preserves `.skilllayer/` memory, source files, and unrelated MCP servers. See [SUPPORT.md](SUPPORT.md) and the installer scripts for explicit removal options.
