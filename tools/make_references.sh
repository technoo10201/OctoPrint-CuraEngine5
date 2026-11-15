#!/bin/sh
# Regenerates tests/reference/*.reference.json with Uranium (see tools/README.md).
# Usage: tools/make_references.sh <python-3.12-with-PyQt6> <extracted-cura-appdir>
set -e
PY="$1"; APPDIR="$2"
HERE=$(cd "$(dirname "$0")" && pwd)
for scenario in "$HERE"/../tests/reference/*.scenario.json; do
    out="${scenario%.scenario.json}.reference.json"
    log=$(mktemp)
    if "$PY" -W ignore "$HERE/uranium_reference.py" --appdir "$APPDIR" -o "$out" "$scenario" >"$log" 2>&1; then
        echo "ok    $(basename "$out")"
    else
        echo "FAIL  $(basename "$scenario")"; tail -5 "$log"
    fi
    rm -f "$log"
done
