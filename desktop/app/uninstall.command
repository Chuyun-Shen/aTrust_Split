#!/bin/bash
set -eu
APP_PATH="$(cd -- "$(dirname -- "$0")/../.." && pwd)"
echo '将停止后台分流，恢复网络配置，并移除后台与菜单栏的自动启动。应用及日志保留。'
/usr/bin/sudo "$APP_PATH/Contents/Resources/runtime/atrust-splitd" uninstall --uid "$(id -u)" --home "$HOME"
