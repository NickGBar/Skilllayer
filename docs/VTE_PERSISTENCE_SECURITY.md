# VTE Persistence — Security Notes (Internal)

> Internal document for the `skilllayer.tasks.persistence` foundation module
> (VTE Foundation A). Not user-facing marketing; describes the trust model,
> data handling, and known limitations of the task-record persistence layer
> shipped in this milestone. See
> [`VERIFIED_TASK_EXECUTION_DESIGN.md`](VERIFIED_TASK_EXECUTION_DESIGN.md) for
> the full Verified Task Execution product design this foundation supports.

## Scope

This document covers `src/skilllayer/tasks/persistence.py` and Foundation C's
task-local checkpoint records: deterministic
task IDs, `.skilllayer/tasks/<task-id>/` directory confinement, the
redaction/rejection gate, consent enforcement, atomic write-once/latest-wins
records, and recovery-safe reads. It does **not** cover scope-contract
enforcement, real test/execution evidence, freshness/drift comparison,
`summary.md` generation, routing, or MCP exposure — those remain design-only
per `VERIFIED_TASK_EXECUTION_DESIGN.md` and are not implemented here.

## Trust model

- The module trusts **the filesystem it directly inspects** (via `os.stat`/
  `Path.is_symlink`/`Path.exists`) and nothing else. It never trusts a
  caller's claim about what a path is.
- It trusts its own internally-computed values (a sha256 fingerprint of the
  resolved project root, `git rev-parse HEAD` output validated against a strict
  hex-shape regex) more than any caller-supplied string.
- It does **not** trust caller-supplied free text at all by default — every
  string value is either structurally validated (paths, enums, hashes) or
  passed through the redaction/rejection gate before it may reach disk.
- Consent is a capability, not an ambient permission: a `TaskConsent` object is
  bound to one `task_id` + one project root (via a fingerprint, not a raw
  path) and only authorizes the record types it explicitly declares.

## Accepted data classes

Three field policies (finalized in
[`VERIFIED_TASK_EXECUTION_DECISIONS.md`](VERIFIED_TASK_EXECUTION_DECISIONS.md)):

| Class | Examples in this schema | Handling |
|---|---|---|
| `SAFE_STRUCTURED` | `task_id`, `allowed_paths`, `forbidden_paths`, `expected_checks`, `verdict`, `status`, `written_by_component`, `baseline_git_head` | Exact shape/allowlist match required; a known secret pattern embedded inside still rejects the whole value (never redacted in place — redacting inside a structured value would corrupt its meaning). |
| `REDACTABLE_TEXT` | `objective_label`, constraint `detail_label`, `verdict_reasons` items, `exact_next_action_label` | Known high-confidence secret patterns reject the whole field; diagnostic PII (home paths, email addresses) is redacted in place; anything still high-entropy after redaction is rejected as low-confidence-sensitive. Bounded length per field (e.g. `objective_label` ≤ 80 chars). |
| `FORBIDDEN_FREE_TEXT` | (not used by any current field — reserved) | Rejected unconditionally, regardless of content. Exists as a hard stop for any future field that must never accept prose (e.g. a raw prompt or exception body). |

`repository_identity` is **never caller-supplied** — it is computed internally
as `sha256:<16-hex>` of the resolved project root path, so it cannot leak a
real path or become an injection vector.

## Rejected data classes (never persisted)

- Any of: Anthropic/OpenAI/GitHub/GitLab/Google/Slack/AWS API keys or tokens,
  private-key PEM headers, JWT-shaped strings, bearer tokens, credential-bearing
  database/HTTP URLs, Slack/generic webhook URLs, SSH or credential-bearing git
  remotes, `.env`-style `KEY=value` assignments naming a secret-like variable,
  credential-bearing CLI flags (`--password=...` etc).
- Any string over its field's bounded length (rejected, **never silently
  truncated** — truncating could itself leak a secret's prefix while hiding
  that anything was cut).
- Any control character other than `\t`.
- Any long (≥32 char), unbroken, digit+letter-mixed run with no recognized
  shape — treated as a low-confidence secret and rejected rather than guessed
  at ("any low-confidence sensitive field must be rejected"). **Exempted from
  this fallback** (added after independent verification found these being
  rejected purely for length/mixed-alnum, which would have made the module
  unable to describe its own subject matter): git SHAs (short or full), sha1/
  sha256/sha384/sha512 hex digests, UUIDs, pip/npm-style package integrity
  hashes, and this system's own `task_id` format. These are structurally
  verifiable and non-secret by convention (a git commit hash or checksum
  exists to be shared). The exemption narrows only this last-resort fallback —
  every specific high-confidence secret pattern above still runs first and
  unconditionally, so a real API key or token is caught regardless.
- Non-`str`/`int`/`bool`/`None`/`list`/`dict` values (e.g. bytes) — unsupported
  type, rejected.
- Nested containers deeper than 6 levels, lists longer than 200 items, dicts
  with more than 100 keys, or dict keys outside `[a-z][a-z0-9_]{0,60}`.

A rejection at any nested leaf **fails the entire value closed** — there is no
partially-sanitized result; `sanitized_value` is `None` whenever
`accepted=False`, and the raw offending text is never echoed back in the
rejection reason (only a pattern *name*).

## Redaction policy

Redaction (replace-in-place, keep the field) is reserved for **diagnostic PII
that is not itself a credential**: Unix/Windows home-directory paths and email
addresses. The replacement token names the pattern
(`«redacted:home_path_unix»`, `«redacted:email_address»`) so a human reviewing
a record can see that something was removed and why, without recovering the
original value. Everything else that looks sensitive is rejected, not
redacted — the design principle is "prefer not storing a value over
attempting uncertain redaction."

## Path confinement and symlink safety

Every write and read resolves the project root and confirms the task
directory is a strict descendant of
`<resolved-root>/.skilllayer/tasks/`. Additionally:

- A symlink at `.skilllayer`, `.skilllayer/tasks`, or the task directory
  itself is refused, never followed (`symlink_escape`).
- A record file (`contract.json`/`result.json`/`checkpoint.json`) that is
  itself a symlink is refused at both read and write time
  (`symlink_not_permitted`) — a file replaced by a symlink after the fact is
  never read through.
- The project root argument itself being a symlink (e.g. a symlinked checkout)
  is a normal, supported case: it resolves to the real underlying directory,
  exactly like any other filesystem tool would.
- `task_id` is validated against a strict regex (`\d{8}T\d{6}Z-<slug>-<hex
  suffix>`) before it is ever used to build a path — this alone blocks `../`,
  absolute paths, null bytes, backslashes, non-ASCII/Unicode separator tricks,
  and case-collision (the format is lowercase-only by construction).
- No suspicious symlink is ever deleted or replaced automatically — the
  operation is refused and reported; cleanup is left to the user.

## Consent behavior

- Read operations (`read_task_contract`/`read_task_result`/
  `read_task_checkpoint`) are **never** consent-gated — they cannot mutate
  anything, so gating them would add friction with no safety benefit.
- Every write (`create_task_contract`/`write_task_result`/
  `write_task_checkpoint`) requires a `TaskConsent` that:
  - was granted (`granted=True`, non-empty `granted_at`),
  - matches the exact `task_id` being written,
  - matches the exact project root (via a fingerprint, so the raw path is
    never compared or logged),
  - declares the specific record type (`"contract"`/`"result"`/`"checkpoint"`)
    being written.
- Without valid consent: **no** `.skilllayer` directory, no lock file, no
  temporary file is created. `planned_paths` (the paths that *would* be
  written) are still computed and returned for display — this is a pure,
  read-only path computation, not a write.
- Changing the `task_id` or the project root invalidates a consent object;
  a fresh `grant_task_consent(...)` call is required.

## Atomicity and concurrency

Every write reuses the existing memory subsystem's primitives verbatim:
`atomic_write_json` (temp file in the same directory, `fsync`, `os.replace`)
and `memory_lock` (a process-local `RLock` plus, on POSIX, `fcntl.flock` over
`.skilllayer/.memory.lock`). No second locking mechanism was introduced.

- `contract.json` is write-once: existence is checked once before the lock
  (fast path) and again *inside* the lock (race guard), so two concurrent
  creators produce exactly one `WRITE_COMPLETED` and one
  `RECORD_ALREADY_EXISTS` — never two successful writes, never a corrupted
  merge.
- `result.json`/`checkpoint.json` are latest-wins but still atomic and locked;
  `checkpoint_version` must strictly increase, checked against the existing
  file read under the same lock scope as the write.
- A write that fails after validation (e.g. a simulated `OSError` from
  `atomic_write_json`) leaves the previous record byte-for-byte untouched —
  `atomic_write_text`'s temp-file-then-`os.replace` design means a failure
  never leaves a partially-written destination file.

**Known limitation:** the lock is scoped to the whole `.skilllayer/` store
(the existing, reused granularity), not to an individual task. Two tasks in
the same repository serialize their writes through the same lock. This is
accepted for the foundation milestone rather than building task-scoped
locking as a second mechanism.

Foundation C additionally writes immutable checkpoint history and an atomic
latest pointer only under the same task directory, after a declared
`checkpoint` consent. It validates sequence/predecessor links and record
digests before reuse. `ownership.json` is an explicit, short-lived task-local
lease under separately declared `ownership` consent; no background heartbeat
or global state is introduced.

## Recovery semantics

Reading never repairs or rewrites what it finds, however malformed:
- Missing file → `RECORD_NOT_FOUND`.
- Symlink at the record path → `PERSISTENCE_BLOCKED` / `symlink_not_permitted`,
  not followed.
- Invalid JSON, or JSON that isn't an object → `PERSISTENCE_BLOCKED` /
  `malformed_json`.
- `schema_version` different from the version this code understands →
  `PERSISTENCE_BLOCKED` / `unsupported_schema_version` (never guessed at, even
  if the version is *lower* than expected — a future migration path can add
  explicit upgrade logic, but silently reinterpreting an unknown shape is
  unsafe).
- The record's own `task_id` field not matching the directory it was found
  in → `PERSISTENCE_BLOCKED` / `task_id_mismatch`.
- Unexpected *extra* fields (forward-compatible case) → tolerated; the record
  is still read successfully with the extra field intact.
- A write that would need to overwrite an existing-but-corrupt
  `baseline.json`, `scope_amendments.json`, `result.json`/`checkpoint.json` while auto-deriving a value from it (attempt
  number, checkpoint version) refuses rather than silently overwriting
  possible forensic evidence of the corruption; passing the value explicitly
  bypasses this guard (a deliberate overwrite decision).

## Known limitations (accepted for this foundation)

- **A raw secret rendered as hex or as a UUID is indistinguishable from a
  legitimate git SHA/hash/UUID by shape alone.** The safe-entropy-shape
  exemption (above) trades a small amount of last-resort defense-in-depth for
  eliminating false positives on extremely common, legitimate identifiers.
  Every specific high-confidence pattern (API keys, tokens, private keys,
  credential-bearing URLs, etc.) is unaffected and still catches such secrets
  regardless of this exemption.
- **Long, safe, hyphenated filenames or descriptive identifiers without a
  recognized structural shape are still rejected** by the entropy fallback
  when they appear in free text. This is a deliberate scope boundary, not an
  oversight: filenames belong in the `SAFE_STRUCTURED` path fields
  (`allowed_paths`/`forbidden_paths`/`expected_checks`) in this schema, not in
  free-text fields — real usage should never need to describe a long filename
  inside `objective_label`/`verdict_reasons`/etc.
- **Lock granularity** is store-wide, not per-task (see above).
- **No scope-contract enforcement yet** — `allowed_paths`/`forbidden_paths`
  are stored and structurally validated but not yet checked against a real
  diff (deferred to the next VTE milestone, along with real test/execution
  evidence, freshness comparison, and `summary.md`).
- **No cross-process advisory locking beyond POSIX `fcntl`** — matches the
  existing memory subsystem's own honestly-disclosed limitation
  (`MEMORY_LOCK_CROSS_PROCESS_SUPPORTED`).
- **The redaction pattern list is not exhaustive.** It targets the specific
  high-confidence shapes enumerated in this milestone's brief (major provider
  API key formats, JWTs, credential-bearing URLs, private-key headers,
  webhook URLs, home paths, emails, `.env`-style assignments, credential CLI
  flags) plus a low-confidence high-entropy fallback. A sufficiently unusual
  or novel secret format could still slip through the `SAFE_STRUCTURED`/
  `REDACTABLE_TEXT` gate if it does not match any of these patterns and is not
  long/mixed enough to trip the entropy heuristic. Treat this as
  defense-in-depth, not a certification.
- **No public workflow, routing, or MCP tool** — this module is intentionally
  unreachable from chat until a later milestone deliberately wires it up.

## Foundation D (orchestrator) integration

`src/skilllayer/tasks/orchestrator.py` reuses every primitive documented
above directly — `TaskConsent`, `atomic_write_json`/`memory_lock`, path
confinement, and `sanitize_persisted_value`/`_contains_rejected_secret` — for
its own additional on-disk records (`state.json`, `transitions/`,
`evidence/`). It introduces no second persistence or locking mechanism, no
new redaction patterns, and no changes to this module. All Foundation D
paths are computed as children of the already symlink-checked `task_dir`
this module returns from `task_record_paths`, with the same additional
per-path symlink checks Foundation C's `checkpoints_dir` already established
before any write. See
[`VTE_FOUNDATION_D_ORCHESTRATOR.md`](VTE_FOUNDATION_D_ORCHESTRATOR.md) for
the full write-up, including the one new small index file
(`.skilllayer/tasks/.idempotency_index.json`) `create_task` uses for
idempotent retries.
