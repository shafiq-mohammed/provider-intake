#!/usr/bin/env bash
# Preflight for the demo machine. Run: bash scripts/check_env.sh
echo "== tools =="
for t in claude gh git python3 ruff; do
  if command -v "$t" >/dev/null 2>&1; then printf '  %-7s %s\n' "$t" "$("$t" --version 2>&1 | head -1)"; else printf '  %-7s MISSING\n' "$t"; fi
done
echo "== python deps (in the current env) =="
python3 - <<'PY'
mods = ["pytest", "fastapi", "httpx", "pydantic"]
for m in mods:
    try:
        __import__(m); print(f"  {m:8s} ok")
    except Exception:
        print(f"  {m:8s} MISSING  -> uv pip install -e '.[dev]'  (or pip)")
PY
echo "== auth =="
gh auth status 2>&1 | head -3 | sed 's/^/  /'
echo
echo "Need: claude >= 2.1.154 for dynamic workflows, gh authenticated, hooks use python3 to parse their JSON input, so nothing else to install."
