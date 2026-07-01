# SkillLayer Telemetry Privacy

SkillLayer telemetry is local by default.

SkillLayer does not automatically upload telemetry, code, logs, screenshots, or
task data. Sharing telemetry is explicit and opt-in.

Normal CLI commands may create local aggregate activity records in:

```text
runs/skilllayer_telemetry/cli_activity.jsonl
```

These records include command name, success/failure, timestamp, duration, and
client. They do not include raw command arguments, raw task text, repository
paths, file paths, diffs, screenshots, or browser URLs.

## What Not To Send

Do not send:

- private source code
- raw repository data
- secrets
- credentials
- API keys
- `.env` files
- private diffs
- screenshots
- browser captures
