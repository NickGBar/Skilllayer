---
name: verified-work
description: How to work in a repository where skilllayer verifies completions — what a blocked stop means and what not to do about it. Use when a message starting with "skilllayer:" blocks you from finishing, or before reporting a task as done here.
---

# Working under skilllayer verification

When you try to finish, a Stop hook runs this project's tests itself and reads the git state.
Your own account of the work is not an input: only what the run observed counts.

- **Before saying "done"**, run the tests yourself and report what you actually saw. Fixing a
  failure now is cheaper than being blocked at the end.
- **If a stop is blocked**, the message lists facts the hook observed (failing tests, protected
  paths touched). Fix the cause in the code, then finish again.
- **Do not make tests pass by changing the tests.** Weakened, skipped or deleted test files are
  recorded in the receipt and shown to the user.
- **Do not edit `.skilllayer-policy.yml`** or any path it protects. If a protected path really
  must change, stop and ask the user.
- **UNVERIFIED verdicts** mean the tests were not observed to pass (none found, environment
  problem, timeout). Do not claim they passed — say what was not verified and why.
- To check without finishing: `skilllayer verify`.
