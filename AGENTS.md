# Echo agent notes

Read `CLAUDE.md`, `BUILD_SPEC.md`, and `PROGRESS.md` before planning or writing
code. This file is the short operator overlay, not a second source of truth.

## Production shell interpreter

Railway's Nixpacks image installs Echo's Python dependencies into `/opt/venv`.
The service start command (`railway.json`) already uses `/opt/venv/bin/python`.

Ad-hoc shells do **not**. `railway ssh -- python ...` (and a bare `python` inside
the container) is Nix's default interpreter, which has none of `requirements.txt`
(httpx, slack_bolt, …). That looks like a data-sync or module failure and is not.

Always run production shell commands as:

```
/opt/venv/bin/python -m agent <command>
```

`nixpacks.toml` does not prepend `/opt/venv/bin` to `PATH`. Nixpacks `[variables]`
apply at **build and runtime**; changing `PATH` there can break the toolchain
before the venv exists. Do not set PATH in Railway service config for this either.

See also `docs/STAGE2_RUNBOOK.md` and `docs/INTAKE_DEPLOY.md`.
