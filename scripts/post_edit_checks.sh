#!/usr/bin/env bash
# PostToolUse hook on Edit|Write. Deterministic, fast, and unbiased: format the touched Python file and lint it.
# Exit 2 sends the lint output straight back to the agent as a blocking message; it fixes it before moving on.
# Tests deliberately do not run here: during the RED phase failing tests are the goal. The Stop gate runs them.
# Parse a dotted key out of the JSON that Claude Code pipes to hooks on stdin (python3 instead of jq, so no extra install).
jget() { python3 -c 'import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d=d.get(k) if isinstance(d,dict) else None
print("" if d is None else (str(d).lower() if isinstance(d,bool) else d))' "$1"; }
INPUT=$(cat)
FILE=$(printf '%s' "$INPUT" | jget tool_input.file_path)
[ -n "$FILE" ] || exit 0
case "$FILE" in *.py) ;; *) exit 0 ;; esac
[ -f "$FILE" ] || exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
RUFF="ruff"; command -v ruff >/dev/null 2>&1 || RUFF="python3 -m ruff"
$RUFF format "$FILE" >/dev/null 2>&1 || true
if ! OUT=$($RUFF check "$FILE" 2>&1); then
  { echo "LINT FAILED for $FILE (fix before continuing):"; echo "$OUT" | tail -40; } >&2
  exit 2
fi
exit 0
