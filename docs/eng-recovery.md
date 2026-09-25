# ENG recovery — operator-only, not executed

The 37 Sep 19–25 FB/IG rows are blocked on asset review, not caption cooldown.
No asset, calendar, outbox or publishing flag was changed during this repair.
The supplied evidence is aggregate: it does not include IDs, format/platform
split, effective slots, or effective cadence. Consequently **no literal 37-row
schedule is claimed complete**. Do not guess those fields or enable catch-up
against the old dates: that could dump the backlog immediately.

## 1. Deploy and inspect without publishing

After Blake separately approves/merges/deploys the feature PR, use the deployed
worker's trusted shell. Keep ENG's publish lane paused during review/rescheduling.
Do not run the general runner for a manual check: it contains outbound lanes.
Use the existing operator controls for pausing; do not infer an active flag from
the Sep 10 mirror. The actual calendar kill switches are
`AGENT_CALENDAR_AUTOPUBLISH` and `AGENT_PUBLISH_ENABLED` (both must be armed);
pausing either affects the fleet, so coordinate the review window. The registry's
`publish_flag` is not this calendar lane's kill switch. `db.gym_get('eng')` reads
the worker's local registry with shared read-through; `config.posting_timezone_for('eng')` uses its
posting_timezone with global fallback; `cadence.resolve_posts_per_day_live('eng')`
uses the 2x switch, shared preference then local/default. Read only these fields:

```bash
python - <<'PY'
from agent import db, config, cadence
r = db.gym_get('eng') or {}
print({'publish_flag': r.get('publish_flag'),
       'posting_timezone': r.get('posting_timezone'),
       'effective_timezone': config.posting_timezone_for('eng'),
       'effective_posts_per_day': cadence.resolve_posts_per_day_live('eng'),
       'calendar_lane_armed': config.calendar_autopublish_enabled(),
       'global_publish_armed': config.publish_enabled(),
       'drive_lane_armed': config.gym_drive_connect_active_for('eng')})
PY
```

Do not dump the registry row: it contains credential fields. Check provider/Drive
and Supabase credential *presence* through the deployment manager, not their values.
Moderation uses `AGENT_NANO_API_KEY` / `AGENT_OCR_MODEL`, existing Drive credentials,
and existing Supabase service configuration. Runner scheduling uses the existing
`GYM_DRIVE_CONNECT` / `GYM_DRIVE_CONNECT_GYMS` gate. Ensure ENG is included without
replacing the other gyms' allowlist. There is no separate default-off moderation
flag. The scheduled pass records at most 50 pending photos per daily run; it does
not approve them. For the blocked rows, explicit asset CLI scans avoid waiting
for the fleet backlog.

## 2. Obtain the exact non-secret inventory

Run in the trusted SQL console (read-only), export as JSON. Include all future
reservations, including pending rows with NULL scheduled_at:

```sql
select id, gym_id, platform, format, post_date, scheduled_at, slot_index,
       status, reject_reason, late_post_id, source_media_asset_id
from content_calendar
where gym_id = 'eng' and post_date >= '2026-09-19'
order by post_date, platform, id;

select distinct a.id, a.gym_id, a.source_id, a.kind, a.content_hash,
       a.review_status, a.moderation_status, a.people_detected,
       a.consent_status, a.moderation_json
from media_asset a join content_calendar c on c.source_media_asset_id = a.id
where c.gym_id = 'eng' and c.platform in ('facebook','instagram')
  and c.post_date between '2026-09-19' and '2026-09-25'
  and c.status = 'approved' and c.late_post_id is null
  and c.reject_reason = 'media_asset_review_required';
```

Confirm the actual platform vocabulary before using the filter. These queries
never select tokens. Dedupe asset IDs: several rows may share one asset.

## 3. Human moderation / consent / approval

For EACH actual asset ID from the export, in an interactive trusted shell:

```bash
ASSET_ID='replace-with-exported-asset-id'
python -m agent.jobs.moderate_gym_media eng --asset-id "$ASSET_ID"
python -m agent.jobs.moderate_gym_media eng --asset-id "$ASSET_ID" --apply
python -m agent.gym_media_review eng "$ASSET_ID" approve --note 'Operator inspected current image and moderation evidence'
```

First command scans but does not write; second writes hash-bound evidence only.
Review the real image, evidence and tenant before approval. No-people clean
photos still need explicit review. For recognizable people, request a release
until genuine supporting evidence exists:

```bash
python -m agent.gym_media_review eng "$ASSET_ID" request_release --note 'Recognizable people; release needed'
# Only with verified real references and expiry (never invented placeholders):
python -m agent.gym_media_review eng "$ASSET_ID" approve --release-ref "$RELEASE_REF" --member-ref "$MEMBER_REF" --expires-at "$CONSENT_EXPIRES_AT" --note 'Verified member release for this image'
```

Videos are deliberately excluded from the photo-only automated classifier. Do
not bypass their moderation requirements. Unsafe, unknown, hash-drifted and
unreleased media remain blocked. No mass approve command is provided.

## 4. Exact offline scheduling command and application boundary

Create `eng-catchup-input.json` from the authoritative inventory:

- `timezone`: confirmed IANA zone; `posts_per_day`: effective 1 or 2.
- `as_of`: current offset-aware time after review/deployment, never a stale time.
- `rows`: exported calendar rows above, including all future active rows.
- `slots`: cadence-approved future `{platform, format, scheduled_at}` objects,
  including occupied slots. Derive effective times with
  `calendar_autopublish.slot_time_for_row(row)` and the confirmed gym timezone;
  carry the derived value in the LOCAL input for NULL scheduled_at rows. Do not
  change live rows just to build this input. Include enough future days to fit
  every missed row. Preserve stories' midday slot and feed cadence slots.

```bash
python tools/eng_catchup_plan.py eng-catchup-input.json > eng-catchup-plan.json
```

This command is wholly offline and cannot contact a service. It requires exactly
37 matching blocked rows; reserves all existing future FB/IG rows; sorts backlog
oldest-first; assigns at most one row/platform/slot; caps daily feed slots at the
confirmed cadence and stories at one/day. It refuses unknown future slots,
duplicate slots, missing reservations, cross-tenant data or insufficient space.
It preserves row/media IDs and proposes `pending` for renewed calendar review.

The output is the precise ID-by-ID application manifest. Blake can apply each
row's `post_date` and `scheduled_at` through the calendar editor, retaining the
ID and media, leaving it pending until reviewed. **There is no safe literal
application command or dated manifest yet without the missing input.** Before
applying, refresh the export and regenerate if any reservation/status/provider ID
changed. Do not use a bulk “publish now”, reset past dates en masse, delete rows,
or call the general runner. Re-enable only the normal publish lane after all
schedule/media gates are verified; inspect provider receipts one slot at a time.
