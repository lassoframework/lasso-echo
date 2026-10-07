# Generated infographic runtime integration

This lane is a default OFF code candidate. No provider was called, credential added, migration applied, calendar changed or sender activated.

## Implemented path

- Existing media scheduler: `client_media_sync._maybe_infographic_fill`. `AGENT_GENERATED_INFOGRAPHIC_RUNTIME=true` selects the fresh runtime and prevents legacy `client_infographic_fill.fill_gaps` from running, including after a hold. OFF preserves existing behavior.
- Existing-row owner runner: `python -m agent.generated_infographic_runtime --gym <exact Echo base> --account <exact base_ig or base_fb> --row <persisted UUID>`. Uses `ForwardMediaOwnerPersistence.connect_from_environment`, the existing Astra original provider, separate Astra reviewer and R2 original-byte host. `AGENT_FORWARD_MEDIA_GUARD` must also be enabled. The existing owner-worker settings require an explicit tenant allowlist and reject publisher credentials. It never uses publisher/service-role credentials as an owner fallback.
- Trusted loader: dedicated B DB snapshot with exact gym/date/logical UUID and separate visual group; exact IG/FB feed account; complete photo inventory and zero eligible photos; current controlled gym palette file with source notes/approval and content revision; a verbatim current approved same-gym source caption. Rephrased captions hold pending an approved copy receipt rather than being inferred approved. Complete delivered visual history is checked before paid generation. SQL-issued immutable SHA/pHash cache proofs are reused only from the dedicated owner snapshot; unresolved URLs are observed once per URL. B independently reloads authenticated cache proof and observes unresolved bytes before reservation.
- A strict candidate validation checks job, original bytes, storage key and readback before B reservation. The owner reloads copy/palette/photo/history facts after generation. B retains final SQL locks and source reservation.
- `/data/generated-infographic-jobs.sqlite` is required on a mounted durable `/data` volume. `AGENT_GENERATED_INFOGRAPHIC_JOURNAL` may select a file beneath the same volume; ephemeral paths hold. The A journal fences generation, and a separate runtime row-to-job table binds identity before the first provider call and preserves it across history-spine changes, incomplete review and ambiguous responses. A durable `committing` marker holds unknown DB commits for independent reconciliation; no new provider job or automatic commit retry follows uncertainty.
- Successful reservation leaves normal calendar status/approval/holds intact. It adds no coach-review category, legacy client-safe-review suffix, direct send or automatic approval. Existing client approval gates govern publication.
- Calendar publication validates current palette before the calendar claim. The existing `forward_media_publish.authorize` repeats current approved source metadata, exact source copy, palette and job bindings before forward-media claims and every lower provider revalidation. DB lineage and final publication claims remain mandatory. A local journal entry grants no publisher authority.

## Draft gap dispatch and owner binding

`run_scheduled` uses the current read-only photo status and empty upcoming calendar dates, then calls only `fixer_generated_gap_dispatch_20261007` through the existing publisher REST client. The RPC accepts request UUID, exact Echo gym base, local date, platform and feed format. It enqueues a bounded immutable request; it receives no source/copy/palette/approval assertions and creates no calendar row. The scheduler never opens the owner DSN, calls `insert_rows` or uses the source observation bridge.

`python -m agent.generated_infographic_runtime --scan` makes one finite discovery pass in the existing isolated owner execution. `generated_infographic_gap_owner.GapOwnerTransport` calls owner-only pending/bind/record RPCs from `migrations/DRAFT_fixer_generated_gap_dispatch_20261007.sql`. The owner verifies the configured tenant allowlist, local upcoming date and local account mapping, selects an exact current approved source, and reads the controlled gym palette. The bind takes existing graph and photo-census locks, then slot and request locks. It refuses conflicting active feed rows (including legacy platform aliases), creates exactly one null-image **pending** row, and rechecks complete zero-photo inventory plus B's sealed history under those locks. It stores immutable caption, approved source metadata revision and palette evidence/digest/revision refs. A replay must match the original refs and still-current row identity.

Instagram and Facebook requests on the same gym/date share one logical UUID and visual group. The runtime pins both rows to one journal execution across unrelated history-spine drift; a changed copy/palette/inventory binding or ambiguous first execution holds. B independently decides whether that exact original may be reserved for the sibling. Completed outcome recording requires an already committed B reservation matching the bound row. Ordinary approval and existing holds remain untouched. No coach-review marker is inserted.

The owner records a durable binding/commit phase before the DB mutation, and a lost commit requires independent reconciliation. Expired/changed/occupied requests hold. Existing held cards require a separately authorized hold-clear operation; this package never clears them. This draft supplies code and disposable database proof, not a provisioned owner process or live dispatch capability.

## Read-only production configuration evidence

On 2026-10-07, first-party Railway `describe_service` returned variable names only for production project `b49e41ea-ae21-4668-bcc8-022d596bbc69`, environment `53cb47a0-bb88-4e4f-9209-41e66ea18a11`.

| Service | Existing execution | Durable storage | Owner provision evidence |
| --- | --- | --- | --- |
| echo `73787c53-f58e-4bd7-8e1c-e9f2cb936577` | `python -m agent listen` | mounted `/data`, echo-volume | No `FORWARD_MEDIA_OWNER_DSN`, `FORWARD_MEDIA_OWNER_ROLE`, owner worker flag or generated runtime flag name |
| echo-intake-web `3adb8f0e-025e-40fe-9b26-fcb29710b7fc` | `/opt/venv/bin/python -m agent intake-web` | distinct mounted `/data`, echo-intake-web-volume | Same owner names absent |
| fixer-worker `3b6f0436-2434-45a8-a034-ec011e0be6b8` | Node hosted fixer from separate scout repository | no listed volume | Same owner names absent |

Echo/intake list existing `OPENAI_API_KEY` and R2/S3 variable names. All three list `SUPABASE_SERVICE_ROLE_KEY`; Echo/intake list `ZERNIO_API_KEY`. These current environments violate the owner-isolation contract and cannot run the owner by simply adding its DSN. No variable values were read or printed. The existing owner CLI is a code route, not a provisioned production process.

## Smallest safe release requirements

1. Integrate reviewed A, repaired B (including sealed baseline and account/format snapshot), and this runtime behind their OFF flags. Finish independent review and required SQL/release checks.
2. Independently review and apply the draft gap dispatch migration after accepted B authority migrations. The minimal approved-copy capability accepts verbatim current approved source words; paraphrased copy remains held without a persisted approval receipt. Preserve approval and existing media holds.
3. Provision the already-designed dedicated owner role/execution with only its intended DB grants, current approved source/account/palette reads, existing Astra/storage credentials and durable journal. Do not put owner DSN into the publisher/service-role process or weaken its environment guards. No new microservice/credential mutation is part of this lane.
4. Establish one durable prepared-job read source at the publisher boundaries. Echo and intake have distinct `/data` volumes today; an owner journal cannot be presumed visible to both. A secured persisted lookup may replace the local journal reader after independent design/review; missing local journal holds in this candidate.
5. Retain all sealed historic baseline image/thumbnail objects and current delivered visuals. B's selected repair returns immutable SQL proof references joined to exact history key, binding, URL and sealed baseline SHA/epoch. Runtime accepts only these owner-issued cache facts, never producer cache flags. Unresolved URLs remain independently observed. This requires the repaired B migration/API; a URL or previous pHash alone cannot authorize reuse.
6. Run an offline then staged end-to-end gap/create/prepare/reserve/attest/claim/provider-readback check. Only then consider scoped flag activation under the existing release owner.

## Offline evidence

`tests/test_generated_infographic_runtime.py` covers fresh candidate preparation through mocked owner reservation, strict original/job/storage binding, exact account/gym/date/logical identity, photo availability and late-photo rechecks, current approved copy and palette revision, complete history byte failure, ambiguity fencing, durable owner commit uncertainty, reservation-spine replay without regeneration, missing publisher journal and explicit scheduler hold without legacy fallback.

No live generation or mutation is established by these fixtures. This runtime does not close the global image activation gap.

Selected offline commands passed in the compatible existing Echo virtualenv:

- Runtime/A/B/forward publisher/legacy fill selection: 167 passed (before final two OFF-default regression additions).
- Calendar autopublish and media-sync regression selection: 256 passed.
- Runtime plus explicit provider send scope: 37 passed (30 runtime and 7 provider scope, before final OFF-default additions).
- Final runtime selection: 33 passed.
- `git diff --check`: clean.

Tests used offline fixtures only; no dependency installation or full-suite run occurred.

Selected runtime review repairs: before-provider row/job fencing, SQL-issued history cache reuse and URL deduplication, suppression of the no-source legacy seed under fresh ON, and current approved source revision checks at calendar/provider boundary. The focused runtime/publisher/media-sync/provider-scope selection passed 125 tests. These remain offline checks; no live generation or activation.

## Gap package offline evidence

- Publisher dispatch tests use mocked REST responses and prove no calendar insert or owner content assertions, photo-first holds, exact account mapping, unavailable/occupied calendar behavior and idempotent request IDs.
- Owner discovery tests bind a mock persisted row and run actual A preparation/reviewer/storage logic plus mocked B reservation; they cover approved-source revocation, finite allowlisted dates, binding rollback and durable unknown commits.
- `tests/test_generated_infographic_gap_owner_pg.py` runs against a unique disposable PostgreSQL 17 cluster, using existing installed psycopg. It applies the authority draft migrations plus this draft, verifies publisher/owner/mixed-role denial, immutable slot/request/ref behavior, concurrent binding, existing holds and client approval, late-photo and missing baseline rollback, SQL bind through B reservation/completion and normal attester/publisher claim, and same-day sibling reservation. No provider or production database is involved.
- B `e50d840f` was the initial fixture dependency. Its independent review identified more owner-authority repairs; integration must rerun this fixture against B's final accepted freeze. This package does not certify that intermediate B commit.

Gap package selection: 223 focused offline tests passed using the existing Echo virtualenv. The disposable PG17 script passed against intermediate B `e50d840f`; required rerun against final accepted B remains explicit. `git diff --check` was clean. Free disk measured 16 GiB before the PG fixture; the bounded temporary cluster is removed after shutdown. No dependencies were installed.

Discovery starvation repair: owner discovery now supplies each configured gym's exact local tomorrow/+2 window to the owner-only pending RPC. SQL validates a bounded window for every allowlisted gym and filters those dates before the batch limit. It never deletes, expires, rebinds or resets old bound/uncertain jobs. The PG17 regression inserts 26 expired requests plus one fresh date, proves the fresh request is returned with limit 25, and proves the old bound row and queue states are unchanged.

Starvation repair evidence: 59 focused runtime/gap tests passed; the disposable PG17 script passed the 26-expired-plus-fresh regression, bound-job preservation and invalid-window holds, alongside its existing authority checks. The discovery RPC's date-window bounds use UTC independently of session timezone. Intermediate B dependency and final accepted B rerun hold are unchanged.
