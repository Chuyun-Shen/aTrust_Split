ARG BASE_IMAGE
FROM ${BASE_IMAGE}
RUN rm -f /etc/apt/sources.list.d/* && \
    printf 'deb https://deb.debian.org/debian bookworm main\ndeb https://deb.debian.org/debian-security bookworm-security main\n' > /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends \
        chromium python3 socat desktop-file-utils ca-certificates && \
    rm -rf /var/lib/apt/lists/*
COPY container/start.sh /usr/local/bin/start.sh
COPY container/xdg-open /usr/bin/xdg-open
COPY container/atrust-browser.desktop /usr/share/applications/atrust-browser.desktop
COPY container/chromium-policy.json /etc/chromium/policies/managed/atrust-split.json
RUN chmod 755 /usr/local/bin/start.sh /usr/bin/xdg-open
CMD ["/usr/local/bin/start.sh"]
