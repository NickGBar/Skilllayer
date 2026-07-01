# SkillLayer Security Checklist

Use this checklist before installing SkillLayer on a work machine or running it
against a repository you care about.

## Before Installing

- [ ] I understand SkillLayer is local software.
- [ ] I understand SkillLayer is not an LLM and not a full autonomous coding
      agent.
- [ ] I reviewed `SECURITY_REVIEW.md`.
- [ ] I reviewed `pyproject.toml`.
- [ ] I reviewed `scripts/install.sh`.
- [ ] I reviewed the MCP command that would be configured for my client.
- [ ] I understand that no formal third-party security audit has been performed.

## Before Running Workflows

- [ ] I understand which workflows are read-only.
- [ ] I understand which workflows can modify files.
- [ ] I tested on a disposable repository first.
- [ ] I used a git branch or copied repository for modifying workflows.
- [ ] I used `--dry-run` before a modifying workflow where available.
- [ ] I know where local logs and telemetry are written: `runs/`.

## Before Enabling MCP

- [ ] I trust the MCP client I am connecting to SkillLayer.
- [ ] I verified the MCP command uses the intended Python executable.
- [ ] I verified the MCP command uses the intended working directory.
- [ ] I listed tools with `python -m skilllayer.mcp_server --list-tools`.
- [ ] I tested read-only tools first.

## Before Sharing Telemetry

- [ ] I generated telemetry export explicitly.
- [ ] I reviewed the exported JSON before sharing.
- [ ] I did not include raw repository code.
- [ ] I did not include raw diffs.
- [ ] I did not include screenshots with private content.
- [ ] I did not include secrets, credentials, or local paths.

## Work Machine Caution

- [ ] I did not run SkillLayer on confidential code without permission.
- [ ] I followed my organization's policy for installing local developer tools.
- [ ] I can explain what SkillLayer does and does not do to a reviewer.
