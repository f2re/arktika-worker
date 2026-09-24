#!/bin/sh
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)" || exit 1
exec sh start.sh "$@"
