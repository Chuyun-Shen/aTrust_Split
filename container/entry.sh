#!/bin/bash
set -eu
: "${PASSWORD:?VNC password must be generated locally}"
: "${AI_TARGETS:?Explicit target addresses are required}"
: "${AI_VPN_INTERFACE:?Expected aTrust tunnel is required}"
xdg-mime default atrust-browser.desktop x-scheme-handler/atrust
language_file=/usr/share/sangfor/.aTrust/var/conf/localConfig.json
if [ ! -e "$language_file" ]; then
    mkdir -p "$(dirname "$language_file")"
    printf '%s\n' '{"lang":"zh-CN"}' > "$language_file"
fi
exec /usr/local/bin/start.sh
