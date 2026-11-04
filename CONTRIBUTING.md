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
