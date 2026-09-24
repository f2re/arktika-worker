#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
export PYTHONUTF8=1
if [ ! -x .venv/bin/python ]; then
  echo 'Сначала выполните: sh install.sh' >&2
  exit 2
fi
exec .venv/bin/python server.py "$@"
