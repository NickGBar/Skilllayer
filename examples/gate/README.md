# skilllayer gate examples

- `pre-push` — the gate as a git pre-push hook: nothing reaches the remote unless the checks pass.
- `gitlab-ci.yml` — a merge-request job that keeps the receipts as artifacts, plus a manual job a
  person runs to approve a change to a protected path.
- `github-actions.yml` — the same for pull requests.
- `run_demo.sh` — four canned agent change sets through the gate, then a tampered receipt
  (`DEMO_LANG=ru` for Russian).

See [docs/GATE.md](../../docs/GATE.md).
