#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -P -- "$script_dir/../../../.." && pwd)

if [ -x "$repo_root/.venv/bin/python" ]; then
  py="$repo_root/.venv/bin/python"
else
  py=$(command -v python3)
fi

export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$py" -m aisys --data "$repo_root/data" mcp
