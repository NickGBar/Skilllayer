# Support

SkillLayer is early-access software. Early-access support is provided on a best-effort basis; no SLA is promised.

1. Run `skilllayer doctor`.
2. Run `skilllayer diagnostics --output skilllayer-diagnostics.md`.
3. Review the file and remove anything you do not want to share.
4. Check [KNOWN_ISSUES.md](KNOWN_ISSUES.md).
5. Open the public GitHub bug report with sanitized output only.

Never attach source code, `.env` files, credentials, unrelated MCP configuration, or project-memory contents.

## Severity

- `SAFETY_INCIDENT`: unexpected writes, access outside the selected repository, automatic dependency installation, sensitive-data exposure, destructive commands, orphaned processes, or unrelated MCP changes.
- `INSTALLATION_BLOCKER`: installation or MCP setup cannot complete.
- `INCORRECT_VERDICT`: a workflow reports a materially wrong result.
- `WORKFLOW_FAILURE`: an intended workflow cannot complete.
- `UPDATE_OR_UNINSTALL_FAILURE`: maintenance operation does not respect its documented boundaries.
- `DOCUMENTATION_OR_USABILITY`: unclear or incorrect instructions.
- `FEATURE_REQUEST`: a request for new behavior.
