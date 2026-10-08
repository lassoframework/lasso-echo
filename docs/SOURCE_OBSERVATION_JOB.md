# Source observation cron job (draft, default OFF)

`agent.source_brand_observation_job` is a finite, single-run CLI lane that
wires the reviewed owner composition — `initialize_source_capture` +
`TrustedSourceObservationProducer` — onto a scheduled semantic revalidation
run. It adds no observation logic of its own: it enforces an explicit tenant
allowlist, bounds per-run work, isolates per-gym failures, prints a concise
sanitized receipt, and exits. There is no scheduler or long-running loop
inside the process; the process must terminate so it is safe as a cron
`startCommand`.

The producer is composed from the **same trusted startup** as the capture
lane: `initialize_source_capture(environ)` builds the runner, and the
producer reuses `runner.collector._resolve` (the operator-approved mapping
resolver) and `runner.collector.authenticate_capture` (the durable capture
receipt journal). No fresh in-memory capture is ever invented; the approved
mapping authority stays exactly where the capture job left it.

## Gates (all fail-closed, all default OFF)

| Env | Required | Meaning |
| --- | --- | --- |
| `ECHO_SOURCE_OBSERVATION_JOB_ENABLED` | `true` | Master job gate. Anything else → the CLI prints `{"state": "off"}` and exits 0 with **no I/O at all** (no journals, no files, no network, no env reads beyond the flag). |
| `ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST` | yes | Comma-separated exact canonical gym UUIDs. Missing, empty, malformed, duplicate, or non-UUID entries **hold the whole run** (`tenant_allowlist_required`). There is no fallback to "all mapped gyms" — only allowlisted gyms are observed, so no cross-tenant work is possible. An allowlisted gym that is not in the approved mapping holds per-gym (`gym_not_in_approved_mapping`). |
| `ECHO_SOURCE_OBSERVATION_JOB_MAX_GYMS` | no | Per-run gym bound, default 50, hard-clamped 1–50. Fail-closed capacity gate: if more gyms are selected than the limit, the **whole run holds** with `allowlist_exceeds_limit` — never a silent prefix truncation. |

When the job gate passes, the producer's own gate applies unchanged inside
`observe()`: `ECHO_SOURCE_OBSERVATION_ENABLED` must be `true` or every gym
holds with `source_observation_disabled` — an **armed hold**, so the process
exits nonzero. The producer's fail-closed holds (missing assessor
credentials, unavailable store, stale/mismatched capture, uncertain
semantic verdict, provider-status drift) all surface per-gym with their
fixed hold codes.

A startup `CaptureIngestError` from `initialize_source_capture` (missing
approved-mapping authority, journal, credential, provider identity, portal
readback) holds the **whole run** with the exact startup hold code — no
fallback to untrusted data.

## Per-gym failure isolation

One gym's hold or error never sinks the run: independent gyms are still
observed and every gym gets its own receipt entry. `ObservationHold` → a
`held` entry with the fixed hold code; any other exception → an `error`
entry carrying the exception **type name only**. Any armed hold or error
makes the process exit nonzero after all gyms are reported.

## Receipt (sanitized)

Receipts contain counts, gym UUIDs, statuses, hold codes, `observation_id`
and `content_sha256` only — **never** report bodies, raw source bytes,
credentials, or mapping contents.

- `{"state": "off"}` — job gate off; no I/O occurred.
- `{"state": "held", "hold": <code>, "gyms": [], "observed": 0, "held": 1, "errors": 0}` — whole-run hold (invalid allowlist, oversize selection, startup `CaptureIngestError`).
- `{"state": "complete", "limit": N, "observed": n, "held": n, "errors": n, "gyms": [...]}` — per-gym entries: `{"gym_id", "status": "observed" | "held" | "error", ...}`; verified observed entries carry `state`, `observation_id`, and `content_sha256`. Held semantic results appear as `status: "held"` with hold code `observation_state_held` and sanitized observation identity.

## Exit codes

- `0` — job OFF (`{"state": "off"}`, no I/O), or a genuinely complete run with zero holds and zero errors.
- `1` — any armed hold (whole-run or per-gym, including `source_observation_disabled` and held/negative semantic observations) or any per-gym error.
- `2` — bad CLI arguments: `--gym` (repeatable) may only **narrow** the configured `ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST`; an invalid/duplicate UUID or a `--gym` outside the configured allowlist is rejected before the job runs.

## Railway config (`railway.source-observation.json`) — NON-DEPLOYABLE

The file exists as the historical deploy config **template** for a dedicated
cron service, mirroring `railway.source-capture.json`. Per official Railway
docs, `cronSchedule` is per-service deploy config: this file has no effect
until a Railway service is created and pointed at it (the existing service
uses the default `railway.json`).

**This separate-service topology is NON-DEPLOYABLE as-is.** The capture
receipt journal is a local SQLite file under
`ECHO_SOURCE_CAPTURE_JOURNAL_DIR` on a service's private volume. Two
independent Railway services get two independent volumes — **never infer
that they share data**. A dedicated observation service would see an empty
journal, so every observation would hold on an unverifiable capture. This
template must not be deployed without a verified shared authenticated
journal topology (operator-provisioned and proven, not assumed).

The SUPPORTED topology is the single-service sequential pipeline:
`agent.source_brand_pipeline_job` (see `railway.source-pipeline.json`) runs
capture then observation in ONE process on ONE service, so both phases share
ONE private durable journal volume via the same
`ECHO_SOURCE_CAPTURE_JOURNAL_DIR`. Observation there never runs after a
capture hold/error, both phases require exact matching allowlists and
explicit enablement, and any hold/error exits nonzero. The start command
uses `/opt/venv/bin/python` explicitly — ad-hoc/container-default `python`
is Nix's interpreter without Echo's dependencies, and `PATH` must not be
changed to compensate (see `AGENTS.md`). All enable flags default OFF; no
service was created or scheduled by either repo file.

## Held contract gaps (not bypassed)

- Real operator-installed approved mapping receipts; dedicated collector
  Supabase credentials and a durable private volume for the capture receipt
  journal; the portal's observation SQL/RPC contract
  (`echo_source_brand_configuration`, `echo_source_brand_current_policy`,
  `echo_source_brand_revalidate`, `echo_source_brand_active`) is **not
  provisioned** by this repo; the semantic assessor credential
  (`ECHO_SOURCE_OBSERVATION_OPENAI_API_KEY`) is not provisioned either.
- Until those exist, an armed run holds at startup or per-gym with the
  corresponding hold code and observes nothing.
- The last-resort infographic path is **not** globally complete: this job
  only produces verified/held semantic observations. It never generates a
  graphic, never mutates an approval, never publishes or sends a post, and
  the PR stays draft/OFF.
- Tests are offline synthetic evidence only; no production call, migration,
  credential provisioning, or provider send was performed.

## Manual use

```
python -m agent.source_brand_observation_job                 # OFF -> {"state": "off"}, exit 0
python -m agent.source_brand_observation_job --gym <uuid>    # smoke test once gated (narrows the configured allowlist only)
```
