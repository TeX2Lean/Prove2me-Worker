#!/usr/bin/env bash
# Runs exclusively on a disposable isolated build worker.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 ci/check.py preflight
export PATH="$HOME/.elan/bin:$PATH"
if ! command -v elan >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 -fsSL https://elan.lean-lang.org/elan-init.sh | sh -s -- -y --default-toolchain none
fi
# No controller dependency registry or token. Only pinned Mathlib is fetched.
# Mathlib's pinned post-update hook runs an unscoped cache get by default.
# Disable only that AUTOMATIC full-cache fetch; never clear an existing cache.
MATHLIB_NO_CACHE_ON_UPDATE=1 lake update
# Calculate the minimal DIRECT Mathlib roots from the approved source tree.
# The pinned Mathlib cache utility fetches their transitive dependencies.
# Use a temporary file instead of process substitution so an audit failure
# cannot be silently treated as an empty import list.
imports_file="$(mktemp)"
trap 'rm -f "$imports_file"' EXIT
python3 ci/check.py imports > "$imports_file"
mapfile -t mathlib_imports < "$imports_file"
if ((${#mathlib_imports[@]})); then
  printf '[worker] Targeted Mathlib roots: %s\n' "${mathlib_imports[@]}"
  lake exe cache get "${mathlib_imports[@]}"
else
  echo '[worker] No Mathlib imports in selected approved module closure.'
fi
python3 - <<'PY'
import json,subprocess
from pathlib import Path
r=json.loads(Path('.build/request.json').read_text())
a=subprocess.check_output(['git','-C','.lake/packages/mathlib','rev-parse','HEAD'],text=True).strip()
if a != r['mathlib']: raise SystemExit('Mathlib revision mismatch')
PY
