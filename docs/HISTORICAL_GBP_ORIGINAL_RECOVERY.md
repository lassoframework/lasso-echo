# Historical GBP original lineage recovery (DRAFT, default OFF)

Status: **draft, no production action taken or scheduled.** This document
describes the fail-closed recovery authority implemented in
`agent/historical_gbp_original_recovery.py`,
`migrations/DRAFT_historical_gbp_original_recovery.sql` and the minimal
`agent/media_guard.py` hook. Nothing here publishes, swaps photos, hosts or
copies files, imports new originals, or claims a signed historical manifest.
The receipt it creates is a **fresh historical reconstruction receipt**.

## Problem

A small number of historical held GBP `content_calendar` rows (held with
`media_not_ready_reason = 'cross_date_media_repeat_needs_new_visual'`) have
`source_media_url` / `source_media_asset_id` / `drive_file_id` all NULL and
their raw originals are no longer tenant-local, so
`media_guard.swap_original_identity` rightly refuses to prove an original.
For each such row, a same-tenant historical IG/FB/Story row still references
the exact raw hosted original, and the deployed `gbp.crop_4x3` recipe
(`ImageOps.fit` RGB 1200x900 LANCZOS, JPEG quality 90; recipe id
`gbp-crop-4x3-jpeg90-v1`) reproduces the held row's delivered object
byte-for-byte.

## Recovery write

The **only** calendar write this feature ever performs is
`target.source_media_url`: NULL -> the operator-pinned trusted raw URL, inside
one service-role SECURITY DEFINER RPC that freezes exact before/after
snapshots into an immutable receipt. Hold, approval, status, caption, date and
delivered media bytes are preserved untouched. It is not a back-dated or
signed owner manifest; it is a new, fully re-verified receipt created at
recovery time.

## Gates (all required, all default closed)

1. `AGENT_HISTORICAL_GBP_ORIGINAL_RECOVERY` truthy (default OFF).
2. Gym in `AGENT_HISTORICAL_GBP_ORIGINAL_RECOVERY_GYMS` (CSV, default EMPTY).
3. Operator-seeded DB binding for the exact `(gym_id, row_id)` pinning:
   historical row id, tenant slug, fixed trusted origin, source URL, source
   SHA256, delivered SHA256, recipe, and FULL expected-before and historical
   row snapshots. Bindings are inserted by the database operator by hand after
   independent review; no PostgREST role (service_role included) has any
   direct write grant, and bindings are UPDATE-frozen by trigger.
4. `historical_gbp_original_recovery_gate.enabled` is false by default.
   Seeding bindings does not enable writes. Only an attended, separately
   authorized database gate change permits the apply RPC to run.
5. `portal_action_receipt_config` `public_origin` (operator-seeded, EMPTY by
   default) must equal the binding's pinned origin; the apply RPC fails closed
   while unset or different.

The private per-row proof for the four actual ENG rows (exact row ids, URLs,
hashes) stays OUTSIDE this repository and is reviewed separately by the
operator. The migration seeds NO bindings.

## Observe and apply verification

- Target row is exactly an eligible held GBP row: account `googlebusiness`,
  format `update`/`photo`, status `pending`/`coach_review`, active variant,
  the exact cross-date hold reason, no publish markers, NULL source/asset/
  drive lineage, and its full live snapshot EQUALS the pinned expected_before
  (re-checked by SQL under ordered locks on the exact target/history rows at apply time).
- The historical row is same-tenant, its full snapshot EQUALS the pinned
  historical snapshot, and it actually references the pinned raw URL in
  `source_media_url` or `image_url`.
- The raw URL matches the anchored trusted-origin tenant content-addressed
  shape: https only, exact origin, `/echo/<slug>/` or `/echo/<slug>_ig/` +
  16-hex content-address segment + file name; no query, fragment, userinfo,
  redirects (fetches refuse redirects), traversal, mixed tenant or lookalike
  host.
- Bounded downloaded raw bytes match the pinned source SHA256 and the URL's
  SHA1-16 segment; delivered bytes match the pinned delivered SHA256 and
  their URL's segment; an independent in-memory GBP crop re-render of the raw
  bytes equals the delivered bytes EXACTLY.

## Phases

1. **Schema**: database operator reviews/applies
   `migrations/DRAFT_historical_gbp_original_recovery.sql` (depends on
   `portal_action_receipt_draft_20261004.sql`) and seeds `public_origin`.
2. **Allowlist**: operator seeds one binding per verified row and arms the
   two env flags for the operator session only.
3. **Observe**: `observe()` is read-only; it downloads bounded bytes,
   re-renders and compares, and returns the proof payload. Default CLI mode
   (`python -m agent.historical_gbp_original_recovery`) prints the plan only,
   with no network and no writes.
4. **Independent review** of the observation against the private proof.
5. **Apply**: a separate explicit step (`apply()` with an operator-chosen
   request id). One transaction: lock binding/target/historical, re-verify
   exact snapshots, update only `source_media_url`, re-read the persisted row
   (any trigger soft rewrite or drift rolls back update and receipt
   together), insert the immutable receipt.
6. **Reconcile**: an unknown transport outcome is resolved by
   `historical_gbp_original_recovery_read` (exact persisted receipt readback)
   and immutable identity comparison, never by blind replay. Concurrent equal
   requests converge on exactly one receipt; a different request id,
   fingerprint, proof or binding is a conflicting reuse and raises. One
   recovery per target row, ever.

## media_guard integration

`swap_original_identity` consults the recovery receipt ONLY after the
ordinary local-original path cannot verify (the library-miss branch), and
only when the immutable succeeded receipt's frozen after snapshot equals the
row's full current snapshot, the binding and proof agree, and the raw +
delivered bytes and the exact in-memory GBP re-render re-verify. The returned
identity has the ordinary shape (`{"sha256", "source_asset_id",
"source_url"}`), so the downstream distinct-original guard is unchanged. No
receipt, no flag, no allowlist, or any drift produces the SAME refusal as
before; a forged, copied, cross-tenant or wrong-row receipt is rejected.
Every other media_guard path is untouched.

## Operator commands

From the approved deployed runtime, with normal credentials already present:

```sh
/opt/venv/bin/python -m agent.historical_gbp_original_recovery
/opt/venv/bin/python -m agent.historical_gbp_original_recovery --gym <exact-gym> --row-id <exact-UUID> --observe
/opt/venv/bin/python -m agent.historical_gbp_original_recovery --gym <exact-gym> --row-id <exact-UUID> --apply --request-id <one-opaque-request-ID>
/opt/venv/bin/python -m agent.historical_gbp_original_recovery --gym <exact-gym> --row-id <exact-UUID> --reconcile --request-id <same-request-ID>
```

The default command performs no network or writes. Keep the database gate OFF
through schema installation, binding review and observation. Env flags and the
exact gym allowlist are separate explicit release gates. Save operator evidence
outside the repo with directory mode0700 and receipt file mode0600. Apply always
rereads and freshly observes bytes; a passed earlier Observation cannot bypass
those checks. It independently rereads the committed full after-state.

After response loss use `--reconcile`: it performs only reads, checks the exact
request identity, full current target/history snapshots and fresh rendered/raw
bytes. Never mint a second request ID or infer authority from a matching mutable
row. A refusal or drift remains a hold for bounded investigation.

`swap_original_identity` continues through its existing byte_hash and r2_key
checks after accepting this additional receipt authority. Audit timestamp
`updated_at` may advance during the sole source-link write; every other column
must match, and the complete resulting timestamp is frozen in the receipt.

## Remaining gates before any real recovery

- [ ] Database operator review + apply of the DRAFT migration (production).
- [ ] Operator seeding of `public_origin` and the per-row bindings after
      independent review of the private four-row proof (kept outside repo).
- [ ] Operator session runs observe -> independent review -> explicit apply.
- [x] Native owner executed the actual migration and race/rollback/role tests
      against disposable databases on existing PostgreSQL17. No production SQL
      was executed by those tests.
- [ ] Independent exact-head integration review and required CI checks.
- [ ] Production deployment/readback, schema preflight, separately reviewed
      private per-row bindings and explicit attended gate activation.
- [ ] After recovery, rerun the normal original guard read-only for each exact
      row before a separately authorized swap. The prior shortlist does not pin
      the standard live picker's choice; approval, scheduling and publication
      remain separate. Keep the kids row outside every recovery and swap scope.

No photo swap, no ticket closeout, and no client communication is part of
this change.
