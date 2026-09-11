---
name: plan
description: Turn requirements into docs/SPEC.md and vertical-slice tickets with acceptance criteria, then stop for human approval. Usage - /plan <file path, Jira key, or pasted text>
disable-model-invocation: true
---

Requirements source: $ARGUMENTS
If it is empty, use docs/REQUIREMENTS.md. If it is a Jira key and a Jira MCP server is connected, fetch the issue and its children first and save the text to docs/REQUIREMENTS.md so the planner can read it.

1. Delegate to the planner subagent. Pass it the requirements file path and tell it to produce docs/SPEC.md and tasks/T-00X.md files per docs/TICKET_TEMPLATE.md.
2. When it returns, read docs/SPEC.md and every tasks/*.md and sanity check: each ticket is a vertical slice, each AC is testable, interfaces are specified for every ticket, dependency order is valid.
3. Print a one-screen summary: ticket IDs, titles, AC counts, dependency order, and any assumptions the planner recorded.
4. STOP. Do not implement anything. Ask the human to approve the tickets or tell you what to change.
