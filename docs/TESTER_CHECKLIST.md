# SkillLayer Tester Checklist

Use this checklist for a first SkillLayer tester session.

- [ ] Repo cloned.
- [ ] Python virtual environment created.
- [ ] Package installed with `python -m pip install -e . --no-build-isolation`.
- [ ] `python -m skilllayer tester-check` completed.
- [ ] `python -m skilllayer doctor --json` passed or produced a useful error.
- [ ] `python -m skilllayer workflows --json` listed workflows.
- [ ] `python -m skilllayer skills --json` listed macros and primitive tools.
- [ ] MCP tools are visible, if using Codex, Cursor, or Claude Code.
- [ ] One dry-run workflow completed.
- [ ] One real workflow completed or failed with a useful error.
- [ ] Feedback submitted through `FEEDBACK_TEMPLATE.md` or GitHub issue.

Please do not share private code, raw task text, secrets, screenshots, or
private diffs.
