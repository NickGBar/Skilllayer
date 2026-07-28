# Natural Dogfood Mode

`NATURAL_DOGFOOD` is the default mode for a Skill Opportunity Audit session
(`skilllayer.tasks.skill_audit`). It exists to keep an adoption experiment
honest: if the tool itself nudges the agent toward using SkillLayer during
the session, the resulting "adoption" data no longer reflects natural
behavior — it reflects the nudge.

## What NATURAL_DOGFOOD guarantees

- SkillLayer tools remain entirely optional throughout the session.
- No applicability suggestion is shown while work is happening —
  `get_assisted_suggestion` (and `skilllayer_audit_session`'s equivalent
  data) is inert in this mode; it returns `suggestion: null` with
  `reason: "not_in_assisted_mode"`.
- No tool is auto-invoked on the agent's behalf.
- No ordinary action is blocked or delayed.
- Only the end-of-session audit (`skilllayer_audit_session`) produces
  output — and even that is a pull, not a push: nothing surfaces until the
  host asks for it.

This mode exists specifically **to avoid contaminating adoption
experiments.** Run natural sessions in this mode when you want to measure
what an agent does when left alone.

## ASSISTED mode, for contrast

`ASSISTED` mode may surface **one** concise, bounded suggestion
(`get_assisted_suggestion`) drawn from the same high-confidence rules used
for `APPLICABLE_BUT_SKIPPED` classification — never more than one, and
never anything below HIGH confidence. Even in this mode, the system still:

- never auto-starts a Verified Task Execution task or any other capability;
- never writes anything without the same explicit consent every other VTE
  record requires;
- never blocks an ordinary tool.

Use `ASSISTED` mode when you've decided the product should actively nudge
adoption and want to measure that nudge's effect — not as a default.

## ENFORCED mode

Not implemented in this milestone. Nothing in `skill_audit.py` blocks,
delays, or requires confirmation for an ordinary action in any mode; there
is no code path that could be mistaken for enforcement.

## Choosing a mode per session

```
skilllayer_audit_record_operation(repo_path, session_id, operation="FILE_EDIT",
                                   mode="NATURAL_DOGFOOD")
```

`mode` is only read on the *first* call for a given `session_id` (session
creation); later calls for the same session ignore it. Call
`skilllayer_audit_status` to confirm which mode a session is actually
running in.
