#!/usr/bin/env bash
#
# Regenerate the Blender extensions repository index (index.json + index.html).
#
# Run this every time you add, update, or remove a .zip in docs/.
# It re-reads every extension zip, recomputes hashes/sizes, and rewrites the index.
#
# Usage:
#   ./build.sh
#
set -euo pipefail
cd "$(dirname "$0")"

# --- Locate Blender (override with: BLENDER=/path/to/blender ./build.sh) ---
BLENDER="${BLENDER:-}"
if [[ -z "$BLENDER" ]]; then
  for cand in \
    "/Applications/Blender.app/Contents/MacOS/Blender" \
    "/Applications/Blender 4.5.app/Contents/MacOS/Blender" \
    "/Applications/Blender 4.2.app/Contents/MacOS/Blender" \
    "$(command -v blender || true)"; do
    if [[ -x "$cand" ]]; then BLENDER="$cand"; break; fi
  done
fi
if [[ -z "$BLENDER" || ! -x "$BLENDER" ]]; then
  echo "ERROR: Blender not found. Set BLENDER=/path/to/blender and retry." >&2
  exit 1
fi

echo "Using Blender: $BLENDER"
echo "Generating repo index in: $(pwd)/docs"
"$BLENDER" --command extension server-generate --repo-dir "./docs" --html

echo
echo "Done. Files in docs/:"
ls -1 docs/
echo
echo "Publish the entire repo/ folder. Users add this URL in Blender:"
echo "  <your-host>/index.json"
