ARG BASE_IMAGE
FROM ${BASE_IMAGE}
RUN rm -f /etc/apt/sources.list.d/* && \
    printf 'deb https://deb.debian.org/debian bookworm main\ndeb https://deb.debian.org/debian-security bookworm-security main\n' > /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends chromium python3 && \
    rm -rf /var/lib/apt/lists/*
COPY container/socks_guard.py container/patch_upstream.py /usr/local/lib/atrust-split/
RUN python3 /usr/local/lib/atrust-split/patch_upstream.py /usr/local/bin/start.sh
COPY container/xdg-open /usr/bin/xdg-open
COPY container/atrust-browser.desktop /usr/share/applications/atrust-browser.desktop
COPY container/entry.sh /usr/local/bin/isolator-entry.sh
RUN chmod 755 /usr/bin/xdg-open /usr/local/bin/isolator-entry.sh
CMD ["/usr/local/bin/isolator-entry.sh"]
