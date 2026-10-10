# GBP offline composition evidence

Frozen starting source: `c0943d059d032c089ef660fe0a59c7a77cbc03e8`
(#373 after empty mentions fix). The initial reproduction ran in isolated
`work/echo-gbp-composition-20261009`; the accepted selected caption repair,
both PG test files and this note were integrated in
`work/echo-inventory-intake-20261008`. No production SQL, configuration
change, live network evidence, GHL work or paid review.

## Command and result

From this worktree:

```sh
/Users/blakeruff/Documents/Codex/2026-09-23/plug-into-the-fixer-project-and/work/echo-remote-drive-cas-venv-20261008/bin/python -m pytest tests/test_gbp_image_authority_composed_pg.py -q
```

Initial result: **5 passed in 5.86s**. `git diff --check` passed. Reused installed PG17
and an existing Python environment; no dependencies installed. Each test uses a
private socket-only disposable PG17 cluster and teardown removes it. Measured
free space before work was 20 GiB; after work 19 GiB (rounded `df`, concurrent
work exists, not attributable reclaimed/consumed bytes). Worktree measured 161 MiB.
First run: 3 passed / 1 failed because hosted-byte observation correctly refused
unavailable readback. Added an offline exact-object transport; rerun passed.
After independent review, the caption reproduction was changed to use the
actual claimed asset without an invented claim suffix. Root reran the same
five cases: **5 passed in 6.24s**; `git diff --check` passed.
After the selected caption fix and integration, root ran the six-suite
selection including both new PG files, journal, GBP planner, calendar store,
and copy gate: **278 passed in 14.04s**. A fresh nonediting integrated review
reran the two new PG files: **8 passed in 8.37s**, no P0-P2 findings.

## Proven composition

The test imports the existing narrow `test_remote_drive_use_pg.pg` fixture.
It installs the real remote CAS draft with the real predecessor inventory lock,
generation and caller checks; fixture comments explain the minimal prerequisite
schema. It does not reproduce the complete production schema or install all
global drafts.

The following are actual code, not authority fakes: GBP row builder, proposal
journal preparation, landing settlement and recovery; canonical byte claim and
selector eligibility/source checks; durable SQLite journal/attempt bookkeeping;
`DriveUseAuthority`, its real dedicated writer connection, and remote PG CAS and
receipt RPC. A PG transport adapter stores and reads full synthetic calendar,
asset and source rows. Hosted-object reads use an in-memory transport; the real
GBP crop, strict recipe replay and observation producer run against original and
rendered JPEG bytes. The caption sample is synthetic approved text.

- Full `to_jsonb` PG readback with UUIDs, timestamps, safe nullable fields,
  `variant_status=active`, and `mentions=[]` confirms the exact landed proposal.
- Landing -> remote CAS -> exact persisted remote receipt -> claim completion
  runs through the actual caller. Usage reaches one and the receipt count stays
  one across replay. The same claim and next-date picker cannot reoffer it.
- Tenant, date and derivative-hash alterations to the frozen remote identity
  are refused. This proves remote use-identity guards, not global cross-tenant
  visual occupancy or perceptual similarity.
- Lost acknowledgment **after actual PG commit** holds `consumption_pending`.
  Recovery uses a new PG connection and the same durable UUID/receipt, settles
  the claim and leaves one receipt/one use. This models caller reconstruction;
  it is not an OS process-kill or power-loss test.
- Missing landed rows and zero readback stay unknown and retain the claim;
  conflicting caption, candidate visibility, or extra authority columns hold
  before consumption. Exact active-row readback can later settle.
- Original and 1200x900 crop bytes have distinct MD5/length lineage. Strict
  replay and hosted readback produce **unverified candidate observations**;
  corrupted hosted derivative bytes return no evidence.
- Both the Python remote-use switch and installed PG remote CAS control default
  OFF, with no remote receipt/use created under the unarmed control.

## Selected caption repair and held global composition boundary

This composed test first reproduced a caption mismatch: GBP froze the raw
proposal before the calendar store normalized it, so an actual PG insert
remained at `write_intent` with no remote receipt. The selected local repair
extracts `prepare_calendar_caption_payload` in `portal_calendar_store.py` and
calls it from `gbp_planner.py` before journal preparation. The store reuses the
same idempotent helper at insertion. The composed test now asserts exact
normalized landing and one remote receipt; the separate
`tests/test_gbp_caption_payload_pg.py` covers long opening hooks and protected
URL semicolon holds. Independent nonediting review found no P0-P2 in this
bounded repair and passed 252 focused/adjacent tests. No production activation
or full staged-writer proof follows from this local correction.

**Global staged writer not certified:** the actual calendar store marks armed
reservation candidates at `portal_calendar_store.py:4793`; journal omitted
visibility defaults only allow `active` at `gbp_drive_use_journal.py:386`.
The armed reservation path skips `visual_writer_prepare` at
`portal_calendar_store.py:4950-4952`; ordinary nonreservation visual
preparation can add authority metadata. The stage/finalizer contract permits
only `source_media_asset_id` and `render_manifest_digest` to change from the
staged snapshot; `visual_group_key` must stay exact. The adverse test records
that candidate visibility and unexpected authority columns cannot settle as
an ordinary active row. It does **not** reproduce successful isolated
preparation or finalization through all migrations. Candidate rows must not
authorize remote use merely by loosening the journal whitelist.

## Remaining proof

This is focused composed **landing/consumption** evidence. It does not execute
`plan_gbp_month` selection/download/hosting end to end, the production
`SupabaseCalendarStore.insert_rows` path, isolated signed owner/corpus clearance,
visual attester, global schedule reservation/finalization, GBP publishing worker,
provider-attempt fence or provider send in the same cluster. It does not prove
live Drive identity, authenticated object access, global once-only original and
derivative occupancy, near-frame similarity, current fleet historical coverage,
production schema/ACL readiness, activation or provider delivery.

The essential full-stack acceptance requested remains **PARTIAL**. PG setup was
feasible; the blocker is missing assembled caller contract and evidence, not an
installation failure. The existing separate authority/reservation/send fixtures
cannot be relabeled as this composition's proof. Finish the normalized journal
and staged-finalization contract first, then run a fixture that installs the
entire frozen migration order and uses the actual calendar store transport,
isolated owner/attester/finalizer, worker and a provider transport stub. No
production enablement is justified by these five passing tests.
