#!/usr/bin/env bash
# PreToolUse guard for Bash. Blocks shell commands that could mutate files under the
# forbidden path prefixes given as arguments (e.g. tests/ for the coder). Exit 2 blocks.
jget() { python3 -c 'import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d=d.get(k) if isinstance(d,dict) else None
print("" if d is None else d)' "$1"; }
CMD=$(cat | jget tool_input.command)
[ -n "$CMD" ] || exit 0
for P in "$@"; do
  case "$CMD" in *"$P"*)
    case "$CMD" in
      *"sed -i"*|*">"*|*"tee "*|*"mv "*|*"cp "*|*"rm "*|*"touch "*|*"python -c"*|*"python3 -c"*|*"cat >"*|*"echo "*|*"printf "*)
        echo "BLOCKED: shell command may mutate $P (use Edit/Write, which are path-guarded): $CMD" >&2; exit 2 ;;
    esac ;;
  esac
done
exit 0
