#!/usr/bin/env bash
# PreToolUse guard for Bash, used by the reviewer. The reviewer has no Edit/Write tools, but Bash could
# still mutate the tree (sed -i, redirects, git commit). Block the common mutation shapes. Best-effort,
# not a sandbox: the point is that the reviewer cannot quietly fix code to make its own review pass.
# Parse a dotted key out of the JSON that Claude Code pipes to hooks on stdin (python3 instead of jq, so no extra install).
jget() { python3 -c 'import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d=d.get(k) if isinstance(d,dict) else None
print("" if d is None else (str(d).lower() if isinstance(d,bool) else d))' "$1"; }
INPUT=$(cat)
CMD=$(printf '%s' "$INPUT" | jget tool_input.command)
[ -n "$CMD" ] || exit 0
if printf '%s' "$CMD" | grep -qE '(^|[;&|[:space:]])(git[[:space:]]+(add|commit|push|checkout|switch|reset|rebase|merge|stash|rm|mv|restore|clean)|rm|mv|cp|sed[[:space:]]+-i|tee|chmod|pip[[:space:]]+install|uv[[:space:]]+pip[[:space:]]+install)([[:space:]]|$)|>>?[[:space:]]*[^&/]'; then
  case "$CMD" in *">/dev/null"*|*"> /dev/null"*|*"2>&1"*) ;; *)
    echo "BLOCKED: reviewer is read-only. Report findings instead of changing anything. (command: $CMD)" >&2
    exit 2 ;;
  esac
fi
exit 0
