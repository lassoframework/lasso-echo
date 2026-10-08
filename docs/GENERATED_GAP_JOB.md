# Generated infographic gap cron job (default OFF)

`agent/generated_infographic_gap_job.py` is a thin, finite cron entrypoint for
the existing generated infographic gap owner scan. It adds **no** owner path,
generation, publishing or scheduling logic of its own.

## What it does

- Armed only by `AGENT_GENERATED_GAP_CRON=true` (default **OFF**). OFF prints a
  held receipt and exits 0 without opening any database, journal or network
  resource.
- Requires an explicit cron tenant allowlist in `AGENT_GENERATED_GAP_TENANTS`
  (comma-separated, max 32, same shape as the owner allowlist). Missing,
  empty, malformed, or any cron tenant not present in the owner allowlist
  `AGENT_FORWARD_MEDIA_OWNER_TENANTS` holds with a static reason code and exit
  2 — no scan runs.
- When armed and valid, it narrows `AGENT_FORWARD_MEDIA_OWNER_TENANTS` in the
  process environment to exactly the cron allowlist (restoring the original
  value afterwards) and runs exactly one pass of
  `python -m agent.generated_infographic_runtime --scan`, which is the sole
  owner execution: `generated_infographic_gap_owner.run_pending`. Owner
  identity, forward-media guard, durable journal, photo-first holds, client
  approval, source authority, binding and outcome recording all remain inside
  that runtime; this wrapper cannot bypass them and grants itself no DB access.
- The runtime closes its own owner connection in a `finally` block and the
  process terminates; there is no loop. Overlapping executions are fenced by
  the owner's SQLite journal phase plus SQL locks, not by this wrapper.
- Exit codes: 0 when OFF or the scan completed; 2 on any hold while armed. The
  runtime's JSON receipt (or hold summary) is the only output; no source
  bytes, credentials or private mapping are logged.

## Railway cron service config

`railway.generated-gap.json` is a **draft, separate-service** deploy config:

- `startCommand` uses `/opt/venv/bin/python -m agent.generated_infographic_gap_job`
  (the Nixpacks venv interpreter; PATH is not modified).
- `deploy.cronSchedule` (`13 3 * * *`) is per-service deploy config per the
  official Railway docs.

**This file alone activates nothing.** It only applies if a Railway service is
explicitly pointed at this config (and the separate owner role/DSN, forward
media guard, runtime flag, cron flag and allowlists are provisioned for that
service by the release owner). No service provisioning, deploy, migration,
feature flag or provider send is established by this file or this document.

## Activation prerequisites (unchanged from the runtime lane)

This job is only as schedulable as the underlying owner runtime. Per
`docs/GENERATED_INFOGRAPHIC_RUNTIME_20261007.md`, the owner is not a
provisioned production process: production services currently violate the
owner-isolation contract, the draft gap dispatch SQL is unapplied, the
canonical owner prerequisite (original bytes + authenticated approval
receipts) is unmet, and the approved-copy capability accepts only verbatim
current approved source words. This wrapper does not close any of those gaps
and holds whenever the runtime holds.

## Checks

`tests/test_generated_infographic_gap_job.py` covers: OFF/no-DB/no-scan,
allowlist narrowing for the scoped `--scan` call (with env restore), cron
tenant outside the owner allowlist, invalid/missing allowlists, pass-through of
the runtime receipt and exit code, the Railway config shape (venv interpreter,
cron schedule, no PATH change), and exit 0 with the runtime's own OFF receipt
when the runtime flag is off. All tests are offline with mocked `runtime.main`;
no owner DB, journal or provider is touched.
