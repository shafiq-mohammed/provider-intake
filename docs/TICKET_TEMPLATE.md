# T-00X: <short title>

Status: todo | in_progress | in_review | done
Depends on: none | T-00Y

## Goal
One sentence: the user-visible outcome this slice delivers.

## Scope
In:
- ...
Out:
- ... (name the obvious adjacent work you are NOT doing)

## Interface (copied from docs/SPEC.md so the ticket is self-contained)
- Module: src/app/<module>.py
- Signatures / endpoints / request and response shapes / error responses

## Acceptance criteria
AC1. Given <precondition> When <action> Then <observable result>
AC2. ...
AC3. ...
(each AC must be verifiable by an automated test through the public interface)

## Test plan
- Per AC: happy path, one edge case, one failure or error path
- Anything that needs a fixture (clock, repository) and how it is injected
