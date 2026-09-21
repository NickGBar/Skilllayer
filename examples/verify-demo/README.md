# verify demo

Two ways to see `skilllayer verify` work. Background: [docs/VERIFY.md](../../docs/VERIFY.md).

## 1. Scripted — no model, about ten seconds

```bash
examples/verify-demo/run_demo.sh          # DEMO_PAUSE=1 slows it down for a recording
```

A canned "agent" works on a copy of `template/` (a small cart module whose last test fails). The
edits are scripted; the hook calls are the real ones the plugin makes, and every verdict comes from
actually running the tests. Three acts:

1. The agent says "Done, all tests pass" after a wrong fix → the stop is blocked with the observed
   assertion; the agent fixes it → allowed.
2. The agent makes the test pass by rewriting the test → allowed (the tests do pass), and you are told
   that test files changed.
3. The tests pass, but the agent edited a migration the repository's policy protects → blocked.

## 2. Live — Claude Code with a real model

```bash
cp -R examples/verify-demo/template /tmp/cart-demo && cd /tmp/cart-demo
git init -q && git add -A && git commit -qm init
claude            # with the skilllayer-verify plugin enabled (see docs/VERIFY.md)
```

Then ask, for example:

> The last test in tests/test_cart.py fails. Fix `total` in src/cart.py. Don't run the tests yourself;
> tell me when you're done.

A capable model often gets this right the first time, in which case nothing is blocked — the hook adds
no noise when the work is sound. To provoke a block, ask for a change that breaks the suite:

> Change `total` to return integer cents instead of a float. Don't touch the tests.

Afterwards, `skilllayer verify --stats` inside the repository shows what was recorded. Everything
stays on your machine.
