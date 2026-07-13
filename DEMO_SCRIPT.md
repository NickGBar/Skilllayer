# SkillLayer demo script (60–120 seconds)

Use the committed `skilllayer-tester-sandbox`. Do not claim external-user
evidence or show a production repository.

## 0–10 seconds — the request

Screen: clean sandbox and an MCP-enabled coding agent.

Prompt:

> Help me make this change safely. Add `farewell(name)` as described in TASK.md.

Highlight: `Safe Code Change`, the bounded plan, and `executed_by: host_agent`.
Hide: terminal setup, long repository scans, and absolute paths.

## 10–30 seconds — plan, edit, validate

Screen: SkillLayer identifies `app.py` and `tests/test_app.py`; the host agent
makes only those two edits.

Prompt:

> Validate the change now.

Highlight: changed files, selected `.venv` Python, and “tests did not start.”
Do not present an incomplete validation as a failed code change.

## 30–50 seconds — truthful remediation

Screen: the environment result.

Highlight these lines:

- selected target Python;
- `pytest_not_installed_in_selected_environment` or equivalent;
- the command based on `requirements-test.txt`;
- “SkillLayer did not run this command.”

Say: “The user approves the one environment change.” Cut the package-install
wait. Then rerun validation and show tests passing plus `CHANGE_VALIDATED`.

## 50–70 seconds — release readiness

Prompt:

> Is this repository ready for careful external testing?

Highlight the bounded verdict, blockers versus warnings, and incomplete checks.
Say: “This is not a security certification.” Hide raw JSON and long check lists.

## 70–100 seconds — resume work

Prompt:

> Save the current objective, constraints, completed work, and next action.

Highlight the disclosed `.skilllayer` written paths. Cut to a fresh MCP/client
session, then prompt:

> What was I working on, what constraints matter, and what should I do next?

Highlight recovered objective, environment constraint, and next action.

## Final frame

Text: “Try the safe sandbox. SkillLayer Professional Beta — $49 one-time.”

Show the GitHub sandbox link and Beta Interest issue link. Do not show private
paths, source code, terminal history, usernames, tokens, or unrelated tabs.
