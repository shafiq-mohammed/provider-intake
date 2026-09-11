---
name: implement
description: Implement one ticket end to end - test-first, deterministic checks, independent review by a read-only reviewer subagent, then a PR. Stops before merge. Usage - /implement T-001
disable-model-invocation: true
---

Ticket: $ARGUMENTS
Read tasks/$ARGUMENTS.md and the matching section of docs/SPEC.md before doing anything else.

Phase 0 - Branch
- git checkout main && git pull --ff-only (skip pull if there is no remote), then git checkout -b feat/<ticket-id>-<slug>
- Set the ticket's Status to in_progress.

Phase 1 - RED (tests first)
- Delegate to the test-writer subagent with the ticket path. Wait for its report.
- Run the new tests yourself and confirm they fail. If any pass, send it back with the reason.
- Commit: test(<ticket-id>): failing tests for <title>
- From this point you must not modify files under tests/. If a test has a syntax error or contradicts SPEC.md, fix it in its own commit and say exactly what you changed and why.

Phase 2 - GREEN (implement)
- Delegate to the coder subagent with the ticket path. It implements under src/ only, runs the full suite until green, and commits feat(<ticket-id>): <title>.
- If it reports a conflict between SPEC.md and the tests, or a test it believes is wrong, STOP and show the human. Do not edit tests yourself.
- Confirm green yourself: python -m pytest -q

Phase 3 - INDEPENDENT REVIEW (max 2 rounds)
- Round N:
  a. Delegate to the reviewer subagent with the ticket path and base branch main. It re-runs the tests itself and returns a structured verdict.
- If the verdict is APPROVE, or every remaining finding is non-blocking, go to Phase 4.
- Otherwise fix the BLOCKING items only, commit fix(<ticket-id>): address review round N, and run one more round.
- After 2 rounds with blocking findings still open, STOP and escalate: list the unresolved findings, what you tried, and why you believe the reviewer is wrong (if you do), and ask the human to decide.

Phase 4 - PR
- git push -u origin HEAD
- gh pr create with a body that contains: the ticket goal; an AC checklist mapping each AC to its test name; the reviewer verdict with the number of rounds; the exact pytest command and result; a Follow-ups section for anything out of scope you noticed.
- Set the ticket's Status to in_review and commit that change on the branch.
- Print the PR URL and STOP. A human merges. Never merge. Never force push.
