# Contributing

Use Python 3.11+:

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
atrust-split --config config.example.toml check
atrust-split --config config.dual.example.toml check
```

Keep the default aTrust/Clash flow small. The second aTrust is optional.
Routing changes must preserve ownership and recovery; test them with mocks,
not privileged commands or real institutional accounts in CI.

For bug reports, include OS/client versions, Clash mode, expected behavior and
a minimal redacted configuration. Keep credentials, browser profiles, subscription
files and login volumes out of issues and commits. Preserve third-party notices.

Desktop source lives in `desktop/`. Run its independent suite without installing
services or using real accounts:

```sh
python -m pip install PyYAML==6.0.3
(cd desktop && python -m unittest discover -s tests -v)
```

For an Apple Silicon build, follow the desktop section in the README, then run:

```sh
"desktop/dist/ATrust Split.app/Contents/MacOS/ATrustSplit" --ui-smoke
```

The UI smoke uses synthetic profiles and real menus without starting connection
helpers. Unit tests must not depend on downloaded vendor binaries, installed user
profiles or active VPN sessions. Keep local JSON settings, environment files,
logs, HAR captures, backups and generated apps out of commits.
