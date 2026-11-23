#!/bin/sh
set -eu

script_path="$0"
if command -v python3 >/dev/null 2>&1; then
  script_dir=$(python3 -c 'import os, sys; print(os.path.dirname(os.path.realpath(sys.argv[1])))' "$script_path")
else
  script_dir=$(CDPATH= cd -P -- "$(dirname -- "$script_path")" && pwd)
fi
repo_root=$(CDPATH= cd -P -- "$script_dir/.." && pwd)

py=$(command -v python3)
export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$py" -m aisys --data "$repo_root/data" mcp
