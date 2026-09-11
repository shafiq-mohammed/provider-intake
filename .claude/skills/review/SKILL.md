---
name: review
description: Run the independent review loop on the current branch without implementing anything. Usage - /review tasks/T-001.md [base-branch]
disable-model-invocation: true
---

Arguments: $ARGUMENTS   (ticket file, then optional base branch, default main)

1. Delegate to the reviewer subagent with the ticket path and base branch.
2. Print its verdict verbatim, then a one-line summary of which findings are blocking.
3. Do not change any code. STOP and let the human decide what to do with the findings.
