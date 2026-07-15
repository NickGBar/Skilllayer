# Diagnostics

Generate a local report with:

```bash
skilllayer diagnostics
skilllayer diagnostics --json
skilllayer diagnostics --output diagnostics.json
```

Diagnostics include product/runtime information, a compact doctor summary, selected-project MCP status, available tool count, and professional-skill availability. They exclude source code, prompts, memory content, environment variables, tokens, private remotes, unrelated MCP entries, and telemetry.

Reports are local only and are never uploaded. Review and redact any report before sharing. `--output` refuses to overwrite an existing file; use `--force` only when you explicitly want to replace that path.
