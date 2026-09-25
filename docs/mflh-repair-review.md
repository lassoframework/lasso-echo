# MFLH recovery — local review only, 2026-09-25

No message is queued or sent, and no live records were changed. Evidence comes
from `ORCHESTRATOR-EVIDENCE.md`, not an independent live query in this repair.

## Canonical identity and duplicate path

Portal gym `a0fcb10f-73dc-4e56-b6ca-61ac9bc9470f` is bound by
`echo_intake_tokens` to **mflhaa5139**. Preserve that binding.
`mflha0fcb1` is MFLH + the same UUID's raw first six characters. Its NOT_SET
token status, NULL timezone and Sep 23 16:03 UTC update are consistent with a
connect-side registry warmup, not a second tenant.

`intake_web.account_key_from_token` calls `_resolved_account_key` before the
social routes. Previously `account_key_resolve._build` computed only the
SHA-derived alias, so a raw-UUID key survived unchanged. Social status/connect
then calls `zernio_routes._resolve_profile_id` / `_persist_profile_id`; these
`db.gym_upsert(account_key, zernio_profile_id=...)` calls create/warm the local
registry and mirror into `echo_gyms`. Status warmup alone can touch a row. The
supplied timestamp cannot distinguish status warmup, connection finalization,
or page-selection upsert without request logs; this is the proven reachable
write path, not a claim of recovered production call traces.

The repair computes BOTH exact UUID-bound aliases, retaining all live-key,
complete-read, collision, split-gym and ambiguous-owner refusals. No name-only
matching. The auth boundary now resolves the raw alias to mflhaa5139; revoked
links stay revoked. `calendar_autopublish.client_gym_bases` resolves/dedupes
aliases before fleet generation and alerts, retiring duplicate fleet work.
Historical rows are not moved or deleted.

## Non-destructive alias/retire procedure (operator only; not executed)

After deploy, inspect these non-secret columns in the trusted SQL console:

```sql
select gym_id, echo_account_key from echo_intake_tokens
where gym_id = 'a0fcb10f-73dc-4e56-b6ca-61ac9bc9470f'
   or echo_account_key in ('mflhaa5139','mflha0fcb1');
select account_key, posting_timezone, publish_flag from echo_gyms
where account_key in ('mflhaa5139','mflha0fcb1');
select gym_id, platform, status, count(*) from content_calendar
where gym_id in ('mflhaa5139','mflha0fcb1') group by 1,2,3;
select gym_id, count(*) from media_source
where gym_id in ('mflhaa5139','mflha0fcb1') group by 1;
select gym_id, count(*) from media_asset
where gym_id in ('mflhaa5139','mflha0fcb1') group by 1;
```

Stop if the raw alias is another token row's live key or has active calendar/media
work: inventory/reschedule that work individually before retirement. Do not
repoint tokens, rename historical calendar IDs, revoke working signed links,
or copy provider credentials. The deployment's resolver supplies the alias;
there is no new alias-table migration to apply.

In BOTH worker and intake-web trusted shells, verify the deployed mapping first:

```bash
python - <<'PY'
from agent.account_key_resolve import resolve
assert resolve('mflha0fcb1') == 'mflhaa5139', 'mapping unavailable/ambiguous; stop'
assert resolve('mflha0fcb1_ig') == 'mflhaa5139_ig'
print('Canonical MFLH alias verified')
PY
```

After the inventory checks pass, mark the duplicate registry OFF. This is a retirement marker, not a
calendar kill switch; the new canonical fleet dedupe prevents new alias work.
Exact **operator-only** local registry update on each service (no helper that
might emit alerts; no token changes):

```bash
python - <<'PY'
from agent import db
from agent.account_key_resolve import resolve
assert resolve('mflha0fcb1') == 'mflhaa5139'
with db.connect() as conn:
    conn.execute("UPDATE gyms SET publish_flag='OFF' WHERE account_key='mflha0fcb1'")
    conn.commit()
print('Alias registry marked OFF; all rows retained')
PY
```

Matching shared mirror change, after confirming there is exactly one canonical
binding and the alias is not independently live:

```sql
begin;
update echo_gyms set publish_flag = 'OFF'
where account_key = 'mflha0fcb1'
  and exists (select 1 from echo_intake_tokens
              where gym_id = 'a0fcb10f-73dc-4e56-b6ca-61ac9bc9470f'
                and echo_account_key = 'mflhaa5139')
  and not exists (select 1 from echo_intake_tokens
                  where echo_account_key = 'mflha0fcb1');
-- Inspect affected row count and non-secret state before COMMIT.
select account_key, publish_flag from echo_gyms
where account_key in ('mflhaa5139','mflha0fcb1');
commit;
```

Only publish_flag changes; existing tokens, credentials, connection IDs and
history remain untouched. Do not enable canonical publishing merely because
connections are healthy. Check current profile ownership and future approved
calendar separately. New connect/status traffic should touch canonical key only;
fleet discovery should list canonical once. Dynamic alias resolution depends on
complete readable identity tables; an unavailable plane fails back unchanged,
so deployment monitoring must also watch for renewed alias writes.

## Why the answer was held

Ticket `52c2373b-d15a-4fca-b110-3b681bf7cde5` is an answerable_question, with
FIXER outcome=answer, but its draft `94e1820c-fae9-440e-8ea4-8d6939986bf8`
was suppressed as a code release. Scout's sender checked both stale claimed
classification and current classification. Triage changes the durable row first,
then passes its older claimed snapshot; a stale code_fix snapshot reproduces
the exact deployment hold. Sender now uses current classification, while an
actual current PR or explicit code-fix marker still requires deployment proof.

Echo's outbox also treated any FIXER-authored answer failing direct-answer
eligibility (hold/escalation/content) as a code release. It now distinguishes
answer routing from eligibility. A no-code question skips PR proof but still
must pass current requester version/hash, hold, content, membership and arming
gates. Existing hold metadata is not automatically cleared. This fixes the
false PR requirement, **not** authorization to release a currently held row.
Regression tests use this ticket's shape and local fakes; no live ready row was
created. Read the current ticket before Blake manually responds; do not revive
the old “all set / will start using all three” copy.

## Connections and generation

Evidence: Facebook (mflhcollective), Instagram and Google Business (Move Fast
Lift Heavy Collective) are connected; all last verified Sep 25 18:01 UTC.
Canonical GBP status was connected at 12:07 UTC. This supports connected,
not publishing. There are six pending rows: **two FB + two IG**, dated Sep 19–20,
and **two GBP**, dated Sep 27–30. No forward FB/IG runway, no media_source,
no media_asset. posts_per_day=1 and autonomous=false.

`client_month_run` requires usable client media for its normal FB/IG lane.
`client_infographic_fill.fill_gaps` can create pending source-grounded graphics
behind AGENT_CLIENT_INFOGRAPHIC_FILL, but intersects its dates with
`media_bridge.bridge_days`. That episode persists exactly two fixed dates:
depleted Sep 18 -> Sep 19 and 20. It does not slide forward each day. On Sep 25
there are no eligible bridge dates; enabling the fill flag alone cannot extend
this expired episode. Voice, approved sources and image provider are additional
requirements. The shared evidence does not establish current runtime flags or
local library contents, but the episode dates explain the observed Sep 19–20
stopping point. Do not reset the episode to manufacture an endless fallback.

To restore FB/IG, Anthony/Blake must connect a client-owned Drive folder or supply
new usable uploads, then sync, moderate and explicitly review them (with consent
for recognizable people). The new moderation runner fills evidence only; use
`python -m agent.jobs.moderate_gym_media mflhaa5139 --asset-id "$ASSET_ID" --apply`
and `python -m agent.gym_media_review mflhaa5139 "$ASSET_ID" approve ...` in a
trusted interactive shell after real inspection. New approved local uploads rearm the bridge through the existing upload path;
approved Drive media unblocks the normal media planner without extending the
expired fallback. Confirm GYM_DRIVE_CONNECT
allowlisting and GYM_DRIVE_STAGE, then let normal planning create pending drafts
at one post/day. Check any stale rows individually; don't bulk publish them.

GBP has a separate `gbp_planner` / `jobs/gbp_month_sweep` path and already has two
pending posts; connected GBP alone does not replenish FB/IG assets. Review GBP
source/voice requirements and those pending drafts before scheduling. Existing
manual GBP generation command is `python -m agent.jobs.gbp_month_sweep --gym
mflhaa5139 --force`, but it can write drafts and emit notifications: **do not run
it under this task's hard rules**. Likewise, general month generation/runner can
notify clients. None was executed. autonomous=false means no promise of automatic
approval or immediate publishing across any platform.

## Ready-to-send text for Blake — LOCAL ONLY

> Anthony, Facebook, Instagram, and Google Business are connected as of today's check. New Facebook and Instagram posts still need media: Echo has no connected media folder for MFLH, and the two-day fallback has ended. Please connect your gym's Drive folder or upload photos you have permission to use so they can be reviewed for new drafts. Two Google Business drafts are waiting for review for September 27 and 30. The connections are ready; the posting calendar still needs review and scheduling.

This text is not queued. Recheck these facts if sending after Sep 25. Blake sends
manually; do not set any outbox row to ready or run the held-answer sweep.
