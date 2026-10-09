# Feed staged Drive composition evidence, 2026-10-09

Base HEAD: `f4f500eeca902f310add1882af925a851f1d29d8` (#380).
This evidence concerns `tests/test_feed_staged_drive_recovery_composed_pg.py`
and the draft still-v2 authority. It is local, disposable PostgreSQL 17
composition evidence. Draft migrations remain unapplied in production and all
production flags remain unchanged. No providers or publishing are exercised.

## Actual composition

The ordinary Instagram photo feed lane calls real `feed_drive_use.freeze_pick`,
`stage_callback` and `SupabaseCalendarStore.insert_rows`. A small synthetic REST
adapter executes its supported PostgREST requests against real disposable PG17
rows as `service_role`; it never invents terminal authority or remote-use
receipts. The test imports the frozen, hash-checked portal 0611 fixture and
remaining draft migration installer from the GBP composition harness.

Real source verification reads synthetic Drive and hosted bytes. Genuine
Ed25519 certificates bind the exact raw media owner, asset, URLs, SHA/MD5,
recipe, row/content and logical post. Real staged owner preparation, trusted
attestation, prospective admission and the calendar admission guard run on PG.
The real finalizer worker runs in a separate OS process with listener SQLite
access forbidden. The original listener journal is unchanged by finalization.
A restarted listener then resolves terminal authority and exact active member
readback before the real remote-use CAS settles one use.

## Coverage

- Lost stage acknowledgment followed by exact retry preserves one staged batch
  and deterministic members, with no premature remote consumption.
- Instagram feed plus a Facebook mirror with distinct rendered bytes finalize
  under one logical post and settle exactly one Drive source use.
- Staged pending rows hold. Eight forged/stale snapshot and proof fields,
  missing proof and proof timeouts hold before landing or CAS.
- Exact discovery and readback discipline, admission identity immutability,
  direct settlement refusal, unrelated tenant progress, restarted recovery and
  receipt replay are exercised. Source use count and remote receipt count stay
  one; scheduling remains pending and unpublished.
- Consumed original bytes reject another tenant and another date. Different
  source bytes equal to the consumed Facebook rendition also reject at owner
  preparation, proving the derivative ancestry fence rather than only exact
  original-source matching.
- A genuine raw alias owner maps to a different canonical batch tenant,
  receives exact staged preparation and admission, finalizes in the separate
  worker and settles once on the listener. Still-v2 admission preserves signed
  raw row ownership and raw source receipts; the alias exemption additionally
  requires the owner-controlled mapping and exact authorized immutable staged
  member. Trusted attestations and lineage remain canonical.
- A newly signed certificate relabeling that source to a different alias of
  the same canonical tenant rejects on its exact durable source receipt.
  Cross-tenant attestation IDs also reject before admission or consumption.
  These checks preserve strict ownership rather than trusting matching alias
  strings supplied by a caller.

## Local commands and results

PostgreSQL 17 binaries: `/opt/homebrew/opt/postgresql@17/bin`.
Python environment: existing `/tmp/feed_pg_venv`.
Measured free space before checks: 12 GiB on the working volume (`df -h /tmp`).
Each harness creates and destroys its own Unix-socket-only temporary cluster,
requires more than 5 GiB free and asserts server major version 17.

```sh
PG17_BIN=/opt/homebrew/opt/postgresql@17/bin /tmp/feed_pg_venv/bin/python -m pytest -q tests/test_feed_staged_drive_recovery_composed_pg.py

PG17_BIN=/opt/homebrew/opt/postgresql@17/bin /tmp/feed_pg_venv/bin/python -m pytest -q tests/test_forward_media_prospective_still_v2_pg.py tests/test_calendar_admission_guard_pg.py tests/test_forward_media_prospective_v2_owner_runtime_pg.py tests/test_forward_media_prospective_still_v2_prepare.py tests/test_gbp_staged_drive_recovery_composed_pg.py
```

Final combined execution of the six files above: **9 passed in 16.41 seconds**. The feed file
alone passed **2 tests in 5.82 seconds**; the five neighboring files passed
**7 tests in 11.93 seconds** before the final combined run. The previous claim
that this host could not run these checks has been removed: PG17 and the
existing Python environment successfully execute them.

## Remaining limits

The REST adapter is not deployed PostgREST/Supabase: actual JWT/RLS/network and
PostgREST error semantics require separate evidence. Drive, hosted bytes,
moderation, signing enrollment and history reviews are fixtures. No Google,
Meta, GHL, Slack or storage-provider delivery is asserted. The held portal 0611
fixture is test-only and does not authorize applying that production migration.

The composed batches cover two same-logical members and a single alias member.
Mixed video batches, resource/budget exhaustion, multi-tenant load, crashes,
concurrent finalizers/listeners, lost finalization acknowledgment and production
journal volume identity are not composed here. Separate local focused tests
may cover parts of these; they are not implied by this evidence. Full-suite CI,
production migration approval, deployment, provider readback and activation
remain separate gates. No commit, push or production action was performed.
