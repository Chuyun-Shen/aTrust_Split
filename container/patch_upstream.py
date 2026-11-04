from pathlib import Path
import sys


def patch(content):
    replacements = {
        "start_danted &\n": "open_port 1080\npython3 /usr/local/lib/atrust-split/socks_guard.py &\ndisown\n",
        "start_tinyproxy &\n": "",
        "    rm -rf /root/.config/chromium/\n    bash -c \"chromium --no-sandbox\" &\n":
            "    rm -f /root/.config/chromium/SingletonLock /root/.config/chromium/SingletonCookie /root/.config/chromium/SingletonSocket\n"
            "    (\n"
            "        for attempt in {1..80}; do\n"
            "            xdpyinfo -display \"$DISPLAY\" >/dev/null 2>&1 && break\n"
            "            sleep 0.25\n"
            "        done\n"
            "        exec chromium --no-sandbox --no-first-run --disable-dev-shm-usage\n"
            "    ) &\n",
    }
    for original, replacement in replacements.items():
        if content.count(original) != 1:
            raise ValueError("Unsupported upstream start.sh; review the pinned base image")
        content = content.replace(original, replacement, 1)
    return content


if __name__ == "__main__":
    path = Path(sys.argv[1])
    path.write_text(patch(path.read_text()))
