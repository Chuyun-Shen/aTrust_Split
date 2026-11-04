#!/bin/bash
set -eu
APP_PATH="$(cd -- "$(dirname -- "$0")/../.." && pwd)"
SETTINGS="${1:-}"
if [ -z "$SETTINGS" ] && [ ! -f '/Library/Application Support/ATrustSplit/settings.json' ]; then
    printf '请输入已填写的 config.local.json 绝对路径（不要上传此文件）：\n'
    IFS= read -r SETTINGS
    if [ -z "$SETTINGS" ]; then
        printf '未提供配置，未安装。\n' >&2
        exit 1
    fi
fi
ARGS=()
if [ -n "$SETTINGS" ]; then
    ARGS=(--settings "$SETTINGS")
fi
echo '将安装 aTrust 分流后台服务和菜单栏应用，需要管理员授权。'
echo '机构资料只复制到本机 root 管理的 settings.json；默认不连接。'
/usr/bin/sudo "$APP_PATH/Contents/Resources/runtime/atrust-splitd" install \
    --app "$APP_PATH" --uid "$(id -u)" --home "$HOME" "${ARGS[@]}"
echo '完成。此窗口可以关闭。'
