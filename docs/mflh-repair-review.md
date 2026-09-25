# MFLH repair review (2026-09-25)

This is a review plan, not an executable migration. No customer reply or data
change has been made. Both `mflhaa5139` and `mflha0fcb1` must be inventoried.

## Required evidence

Read only the non-secret columns of `echo_intake_tokens` (`gym_id`,
`echo_account_key`), `gyms` identity/profile fields, social connection snapshots,
and calendar status/counts for each key. Do not select token blobs. Establish
that both keys belong to the same portal UUID; names alone are insufficient.
Read Zernio accounts under that UUID's verified profile and compare provider
account IDs for Facebook, Instagram, and Google Business. A callback timestamp
alone does not prove a currently healthy connection.

`agent/account_key_resolve.py` explains the historical derivation split:
portal used slug plus raw UUID prefix; Echo used slug plus SHA-256 prefix.
Existing issued keys must be retained. The resolver intentionally refuses to
choose when two live token keys exist. Do not weaken that ambiguity fence.

## Non-destructive resolution proposal

1. Choose a canonical key only after proving tenant/profile ownership and
   inventorying content, media, intake sources, and reply queues on both keys.
2. Add a tenant-bound alias mapping from the other key to the canonical key,
   with explicit review metadata and a retired-for-new-generation flag on the
   alias. Retain both original keys and all historical rows. The mapping must
   reject cross-tenant collisions, chains, and cycles.
3. Route new generation through the canonical key. Reads must include historical
   alias data under the same verified tenant; existing calendar row IDs and
   provider IDs remain unchanged. Publish reservations/cadence must aggregate
   both keys before enabling either lane, or aliases can double the daily cap.
4. Preserve existing signed links. Do not repoint the intake token alone: the
   current `account_key_reconcile` writer explicitly refuses when that would
   strand data. Do not delete or automatically replay either calendar.
5. Verify all three connections by provider ID and state, and inspect pending
   generation per platform. GBP uses `gbp_conn_sync` / `gbp_planner`, separate
   from the IG/FB publisher. Normalize Google platform aliases with
   `zernio.normalize_platform` before comparing tables.

This proposal requires the live inventory and a schema/consumer implementation
review before applying a mapping. Neither key is declared canonical here.

## Reply preparation

Inspect the particular held support message's `attachments.held_why`,
`hold_tier`, `hold_rule`, `fixer_superseded_by`, and its ticket's current
`verification_after`, request version and destination. No reason is inferred
from the Slack card's words HELD/BLOCKED alone.

Scout's held-answer repair now confirms verification metadata before suppressing
the original held draft. Its offline test proves a failed metadata write keeps
the draft recoverable and a later successful run prepares a `ready` replacement.

Only after fresh connection evidence, a suggested manual reply is:

> Your Facebook, Instagram, and Google Business connections are confirmed.

This is conditional copy, **not a verified claim or a queued message**. Do not
add “posting” or “drafts are ready” without separate calendar/provider evidence.
Writing `delivery_status=ready` to the live outbox can send automatically, so
retain any prepared copy locally for Blake; do not run the held-answer worker
or release an outbox row during this repair.
