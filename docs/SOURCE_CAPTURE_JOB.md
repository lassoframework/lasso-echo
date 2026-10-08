# Source capture cron job (draft, default OFF)

`agent.source_brand_capture_job` is a finite, single-run CLI lane that wires the
reviewed owner composition — `initialize_source_capture` +
`SourceBrandCaptureRunner.capture` — onto a scheduled run. It adds no capture
logic of its own: it enforces an explicit tenant allowlist, derives a
deterministic durable request ID per approved gym per scheduled UTC day, bounds
per-run work, isolates per-gym failures, prints a concise receipt, and exits.
There is no scheduler or long-running loop inside the process; the process must
terminate so it is safe as a cron `startCommand`.

## Gates (all fail-closed, all default OFF)

| Env | Required | Meaning |
| --- | --- | --- |
| `ECHO_SOURCE_CAPTURE_JOB_ENABLED` | `true` | Master job gate. Anything else → the CLI prints `{"state": "off"}` and exits 0 with **no I/O at all** (no journals, no files, no network). |
| `ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST` | yes | Comma-separated exact canonical gym UUIDs. Missing, empty, malformed, duplicate, or non-UUID entries **hold the whole run** (`tenant_allowlist_required`). There is no fallback to "all mapped gyms" or any other discovery — only allowlisted gyms are captured, so no cross-tenant work is possible. An allowlisted gym that is not in the approved mapping holds (`gym_not_in_approved_mapping`). |
| `ECHO_SOURCE_CAPTURE_JOB_MAX_GYMS` | no | Per-run gym bound, default 50, clamped 1–50 (sufficient for the current 14-gym global rollout). Fail-closed capacity gate: if more gyms are selected than the limit, the **whole run holds** with `allowlist_exceeds_limit` — there is no prefix capture and no silent `deferred` tail that would starve later gyms forever. |
| `ECHO_SOURCE_CAPTURE_JOB_DAY` | no | Scheduled UTC day `YYYY-MM-DD`. Pins the durable request ID so a retried invocation reuses the same receipt binding. Unset → current UTC date. Invalid format holds (`scheduled_day_invalid`). |

When the job gate passes, the runner's own startup gates apply unchanged
(`ECHO_SOURCE_CAPTURE_RUNNER_ENABLED`, `ECHO_SOURCE_COLLECTOR_ENABLED`,
`ECHO_SOURCE_CAPTURE_INGEST_ENABLED`, private operator-owned
`ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE`, private
`ECHO_SOURCE_CAPTURE_JOURNAL_DIR`, collector Supabase config, `ZERNIO_API_KEY`).
Any startup hold (missing/invalid mapping, journal, credential, provider
identity, portal readback) is reported as a `held` receipt with the exact hold
code and the run performs no fallback to untrusted data.

## Durable request ID

`source-capture:{gym_uuid}:{scheduled_utc_day}` — stable per (gym, scheduled
day). A retried cron run for the same day re-enters the same durable journal
receipt binding instead of claiming a new request.

## Receipt

The printed receipt contains only counts, per-gym statuses, request IDs and
hold/error codes. Raw source bytes, credentials, and private mapping contents
are never logged. Unexpected per-gym exceptions are recorded by exception type
name only; one gym's hold or error never sinks the other gyms or the run.

## Railway config (`railway.source-capture.json`)

The file exists as the deploy config **template** for a future dedicated cron
service. Per official Railway docs, `cronSchedule` is per-service deploy
config: this file has no effect until a Railway service is created and pointed
at it (the existing service uses the default `railway.json`). The start command
uses `/opt/venv/bin/python` explicitly — ad-hoc/container-default `python` is
Nix's interpreter without Echo's dependencies, and `PATH` must not be changed
to compensate (see `AGENTS.md`). No service was created or scheduled by this
repo file; nothing here is live wiring.

## Held contract gaps (not bypassed)

- Real operator-installed approved mapping receipts (none ship in this repo);
  dedicated collector Supabase credentials and a durable private volume for
  the journals; portal #793's draft SQL/RPC contract; an explicitly approved
  activation of the three runner flags plus this job flag.
- Until those exist, an armed run holds at startup with the corresponding hold
  code (e.g. `private_mapping_authority_required`) and captures nothing.
- Tests are offline synthetic evidence only; no production call, migration,
  credential provisioning, or provider send was performed.

## Exit codes

- `0` — job OFF (`{"state": "off"}`, no I/O), or a genuinely complete run with no capture errors.
- Nonzero — any armed hold after all selected gyms are reported (startup gate, invalid/missing allowlist, `scheduled_day_invalid`, `allowlist_exceeds_limit`, startup `CaptureIngestError`), or any per-gym capture error in a completed run. A nonzero exit never skips the per-gym receipt lines.

`--gym` (repeatable) may only **narrow** the configured `ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST` — it never replaces or widens it. A `--gym` outside the configured allowlist, an invalid/duplicate UUID, or a `--gym` with no configured allowlist is rejected with a nonzero exit before the job runs.

## Manual use

```
python -m agent.source_brand_capture_job                 # OFF -> {"state": "off"}, exit 0
python -m agent.source_brand_capture_job --day 2026-10-08 --gym <uuid>   # smoke test once gated
```
