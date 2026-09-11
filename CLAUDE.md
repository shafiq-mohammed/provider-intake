# Project memory

## What this repo is
An agentic delivery pipeline: plan -> test-first (test-writer, then coder) -> deterministic checks -> independent review (read-only reviewer subagent) -> PR -> human merge. The application code lives under src/app/. The pipeline itself lives in .claude/ and scripts/.

## Stack and commands
- Python 3.11+, FastAPI, pydantic v2, pytest, ruff. Dependencies in pyproject.toml (uv preferred, pip is fine).
- Tests:  python -m pytest -q
- Lint:   ruff check . && ruff format .
- Run:    uvicorn app.main:app --reload   (from an env where src/ is on the path)

## Layout
- src/app/        application code
- tests/          pytest tests, one file per ticket: tests/test_T-00X_<slug>.py
- docs/SPEC.md    architecture, conventions, and the public interface for every ticket
- tasks/T-00X.md  one ticket per vertical slice, format in docs/TICKET_TEMPLATE.md
- .claude/agents  planner, test-writer, reviewer (fresh context, restricted tools)
- .claude/skills  /plan, /implement, /review (the orchestration)
- scripts/        hook scripts

## Rules for every agent
- Work one ticket at a time on a feature branch (feat/T-00X-slug). Never commit to main. Never merge. Never force push.
- Tests are derived from the ticket's acceptance criteria and written before the implementation. Never weaken, skip, delete, or over-mock a test to get to green. If a test is wrong, say so explicitly and fix it in a separate commit.
- Follow the interfaces in docs/SPEC.md exactly. If the spec is wrong, update the spec in the same PR and explain why in the PR body.
- Hooks lint every edit and run the test suite before you can stop. Their output is ground truth; your belief that something works is not.
- Keep the diff scoped to the ticket. Out-of-scope improvements go in a "Follow-ups" note in the PR body, not in the diff.
- Never read or write .env or anything under secrets/.
- When you delegate to a subagent, pass it file paths, not summaries. Subagents start with empty context.

## Definition of done for a ticket
1. Failing tests committed first, then implementation, suite green.
2. reviewer subagent verdict APPROVE (or only non-blocking findings). Max 2 automated review rounds; if blocking findings remain after that, stop and escalate to the human with the verdict and your reasoning rather than looping again.
3. PR opened with an AC -> test mapping and the review verdicts. A human merges.
