#!/usr/bin/env bash
# PreToolUse guard for Edit/Write. Allows writes only under the path prefixes given as arguments,
# relative to the project root. Exit 2 blocks the tool call and feeds the message back to the agent.
# Used by: planner (docs/ tasks/), test-writer (tests/)
# Parse a dotted key out of the JSON that Claude Code pipes to hooks on stdin (python3 instead of jq, so no extra install).
jget() { python3 -c 'import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d=d.get(k) if isinstance(d,dict) else None
print("" if d is None else (str(d).lower() if isinstance(d,bool) else d))' "$1"; }
INPUT=$(cat)
FILE=$(printf '%s' "$INPUT" | jget tool_input.file_path)
[ -n "$FILE" ] || exit 0
ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
REL="${FILE#"$ROOT"/}"
for PREFIX in "$@"; do
  case "$REL" in "$PREFIX"*) exit 0 ;; esac
done
echo "BLOCKED: this agent may only write under: $*   (attempted: $REL)" >&2
exit 2
