#!/bin/bash
set -eu
TASK_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
exec /bin/bash "$TASK_DIR/dist/ATrust Split.app/Contents/Resources/install.command" "$@"
