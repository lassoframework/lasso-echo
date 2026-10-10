# Source capture dedicated startup (draft, OFF)

`agent.source_brand_startup.initialize_source_capture` is an explicit composition
entry point for a dedicated collector owner. It returns `None` with the runner
flag absent. Importing it makes no network calls and starts no task. It is not
connected to the existing publishing service or scheduler.

All three flags must explicitly equal `true` before construction:
`ECHO_SOURCE_CAPTURE_RUNNER_ENABLED`, `ECHO_SOURCE_COLLECTOR_ENABLED`, and
`ECHO_SOURCE_CAPTURE_INGEST_ENABLED`. They have no enabled defaults.

The operator installs `ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE` as an absolute
path to private server state. The supported method is the container-side CLI
`python -m agent.source_brand_approval_install` (use `/opt/venv/bin/python -m ...`
inside the Railway container per the shell interpreter note in AGENTS.md): the
operator-reviewed mapping JSON is piped on stdin only (never CLI arguments,
environment values, or log output) and the target path is read from
`ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE`. The installer stages bytes to a
fresh 0600 file in the authority directory, validates the staged file with the
real `load_approved_mappings` loader, then atomically renames that verified file
over the target. A failure before replacement leaves the previous authority
untouched; it also creates `ECHO_SOURCE_CAPTURE_JOURNAL_DIR`
0700 when absent. Railway remote stdin forwarding for `railway run`/`ssh` pipes
could not be proven, so this is documented as a container-side method run with an
attached stdin (for example via an interactive container shell), not a claimed
working remote one-liner. Installation permission is still not proof that an
approval is true; receipts require independent review as below. The installed state is an
owner-only directory and file (0700 / 0600), current
OS owner, regular file, no symlink components or hard links. The bounded JSON
schema is `{"schema_version":1,"approved_mappings":[...]}`. Each mapping has exact
`gym_id`, `echo_account_key`, `website_urls`, `domain_evidence`, `approval_receipt`
and `valid_until`. Optional fields are `instagram_handle`, `instagram_owner_id`,
`owner_id_evidence`, `owner_id_evidence_source`, as defined by `ServerMapping`.
Unknown or duplicate fields, empty authority, duplicate gyms, expired approvals,
invalid UUIDs and unsafe website URLs hold. Installation permission is not proof
that an approval is true: the integration owner must independently review every
approval receipt and exact domain mapping. Browser data and request bodies must
never become this file. No Swift River approval is shipped in this package.

Construction validates dedicated collector Supabase configuration and the
existing Zernio credential, then uses `build_capture_runner` with the private
persistent `ECHO_SOURCE_CAPTURE_JOURNAL_DIR`. The directory must already exist
with private ownership/permissions. Preserve both SQLite databases across
restarts. Use one dedicated owner and one durable volume; separately journaled
hosts are unsupported. Construction creates journals but does not fetch or
insert source bytes. The owner later invokes `runner.capture(canonical_gym_uuid,
stable_request_id)` after release approval. Never expose this as an owner input.

Each capture resolves the exact current portal key and Zernio profile, verifies
a complete authenticated provider listing and its confirmed portal attestation,
and executes the existing SSRF-safe exact website response verifier. Social
capture additionally requires the separate OFF-by-default social flag, reviewed
Apify actor and bounded charge configuration. Existing private receipt/readback
checks bind bytes, tenant, mapping and request through retries. A provider lookup
older than 15 minutes holds before ingestion.

Release remains blocked on real mapping approval receipts, operator-installed
private authority and dedicated credentials/volume, acceptance and deployment
of portal #793's draft SQL/RPC contract, exact provider response contract/live
readback, and an explicitly owned scheduling/startup integration. No production
migration, credential provisioning, provider send, activation or fact approval
was performed. The fixture checks are offline evidence only.
