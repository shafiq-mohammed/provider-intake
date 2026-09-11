#!/usr/bin/env bash
# Stop hook: the agent may not declare itself done while the working tree is dirty and the suite is red.
# stop_hook_active is true when the agent is already continuing because of this hook, so we let it stop
# then (it has explained itself, e.g. "this is the intended RED phase"). This prevents infinite loops.
# Parse a dotted key out of the JSON that Claude Code pipes to hooks on stdin (python3 instead of jq, so no extra install).
jget() { python3 -c 'import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d=d.get(k) if isinstance(d,dict) else None
print("" if d is None else (str(d).lower() if isinstance(d,bool) else d))' "$1"; }
INPUT=$(cat)
ACTIVE=$(printf '%s' "$INPUT" | jget stop_hook_active)
[ "$ACTIVE" = "true" ] && exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
ls tests/test_*.py >/dev/null 2>&1 || exit 0
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  [ -z "$(git status --porcelain)" ] && exit 0   # clean tree (no modified or untracked files): nothing to gate
fi
PY=python; command -v python >/dev/null 2>&1 || PY=python3
$PY -c "import pytest" 2>/dev/null || { echo "stop_gate: pytest not importable in this environment, gate skipped" >&2; exit 0; }
OUT=$($PY -m pytest -q -x 2>&1); RC=$?
if [ $RC -ne 0 ]; then
  {
    echo "STOP GATE: the test suite is red and the tree has uncommitted changes."
    echo "Either fix the failures, or, if this is the intended RED phase of TDD, commit the tests and say so explicitly."
    echo "$OUT" | tail -30
  } >&2
  exit 2
fi
exit 0
