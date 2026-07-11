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

## Anonymous Export

Generate a sanitized local export with:

```bash
python -m skilllayer telemetry-export
```

The export command writes a JSON file under:

```text
runs/skilllayer_exports/
```

Review the generated file before sharing it.

## Default Sanitization

By default, telemetry export removes or redacts:

- raw task text
- normalized task text that may contain user text
- repository paths
- local absolute paths
- usernames in local paths
- emails
- secrets
- API keys
- browser URLs
- raw diffs
- screenshots

The anonymous export includes aggregate CLI activity counts such as
`command_counts`, `successful_command_counts`, `failed_command_counts`,
`total_cli_commands`, and `last_activity_at`. It does not include raw CLI
arguments by default.

Redacted placeholders include:

```text
[REDACTED_TASK]
[REDACTED_PATH]
[REDACTED_EMAIL]
[REDACTED_SECRET]
```

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
- unreviewed telemetry exports

## Optional Unsafe Flags

The export command has optional flags for local debugging:

```bash
python -m skilllayer telemetry-export --include-raw-tasks
python -m skilllayer telemetry-export --include-local-paths
python -m skilllayer telemetry-export --allow-unsafe
```

Do not use these flags for tester sharing unless you have manually reviewed the
output and are certain it contains nothing private.

## Bundle Review

Maintainers can review multiple anonymous exports locally:

```bash
python -m skilllayer review-bundle exports/
```

Bundle review performs local aggregation only. It does not upload anything and
does not generate roadmap recommendations.
