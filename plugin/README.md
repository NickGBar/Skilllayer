# skilllayer-verify

A Claude Code plugin that checks the agent's work instead of trusting it. When the agent tries
to finish, a Stop hook runs the project's tests itself and reads the real git state; failing
tests or a touched protected path send the agent back to work with the observed facts.

It adds two hooks (`UserPromptSubmit`, `Stop`) and one short skill, and needs the `skilllayer`
command on your `PATH` (or in `$SKILLLAYER_BIN` or `~/.local/bin`):

```bash
pipx install git+https://github.com/NickGBar/Skilllayer.git
claude plugin marketplace add NickGBar/Skilllayer
claude plugin install skilllayer-verify@skilllayer
```

If the command is missing, every Stop says the work was **not** verified rather than passing
silently. Full documentation — verdicts, what is and is not verified, policy, privacy, limits:
[docs/VERIFY.md](../docs/VERIFY.md). To watch it work without a model:
`examples/verify-demo/run_demo.sh`.

- Optional: `SKILLLAYER_TEST_COMMAND="make test"` replaces test auto-detection. It is read from
  your environment, never from the repository being judged.
- Everything stays on your machine; receipts live under `~/.skilllayer/verify/`.
