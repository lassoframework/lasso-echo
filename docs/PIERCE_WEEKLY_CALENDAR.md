# Pierce weekly calendar operator runbook

This runbook covers the Pierce Fitness tenant (`piercefitness`) only. It documents
the code path; it does not establish that a worker is currently configured,
connected, or healthy. Confirm live configuration and command output on the worker
before treating a stage as complete.

## Operating model

Pierce’s weekly review block is exactly seven days, Saturday through Friday. On
Friday, `pierce_weekly_window()` selects the *following* Saturday through Friday
block so it can be reviewed before posting. On the other six days it selects the
current block and repairs it only when needed (`agent/client_media_sync.py`,
`pierce_weekly_window`, `scan_and_generate`). The test cases pin this behavior for
September 18, 21, and 25, 2026 (`tests/test_pierce_weekly_window.py`).

The weekly store is tenant-scoped. `_PierceWeekStore` preserves rows outside the
selected seven dates when the month builder performs its delete-and-insert, and it
raises if asked to mutate any account other than `piercefitness`
(`agent/client_media_sync.py`, `_PierceWeekStore.delete_month`). Existing feed
counts are narrowed to the selected dates, so an older published feed does not
block the next weekly block (`_existing_feed_count`; test
`test_week_count_excludes_older_published_feeds`).

## Gates and expected safety behavior

Keep both flags off until the operator has reviewed the inputs and is ready to
stage a draft:

* `AGENT_CLIENT_MEDIA_SYNC` is the global gate and defaults to off. With it off,
  `scan_and_generate()` returns `ok: false` with reason `AGENT_CLIENT_MEDIA_SYNC
  off` and performs no R2 read, download, or calendar write
  (`agent/config.py`, `client_media_sync_enabled`; `agent/client_media_sync.py`,
  `scan_and_generate`).
* `AGENT_PIERCE_WEEKLY` is the Pierce-only opt-in. A truthy value is one of
  `1`, `true`, `yes`, or `on`, case-insensitively. It changes planning to the
  Saturday–Friday window only for `piercefitness`; it does not arm other gyms
  (`agent/client_media_sync.py`, the `weekly_pierce` branch in
  `scan_and_generate`).

When the scan is armed, it uses real synced media and approved client sources.
The calendar output is a pending draft; this path has no publisher or autopublish
call. Missing media, missing calendar store, unreadable calendar data, or missing
voice material leave the tenant awaiting/held and do not justify inventing a post.
The per-gym scan is isolated so one tenant failure does not block others, but this
runbook’s reset command explicitly targets Pierce.

## One-time September reset and first block

`scripts/reset_pierce_weekly.py` is a one-time reset for the inclusive range
**2026-09-19 through 2026-10-18**. It targets only `piercefitness` and only
active rows whose status is one of `approved`, `denied`, `pending`, `draft`, or
`queued`. Before deletion it writes the complete matched rows to:

`/data/pierce_calendar_reset_20260918.json`

The file is created exclusively with mode `0600`, flushed, fsynced, and printed
with a SHA-256 digest. A pre-existing backup causes the normal reset to stop. The
script also stops when the Toronto date is after September 19, when the calendar
store is unavailable, when `AGENT_PIERCE_WEEKLY` is not armed, when no rows match,
when a delete is partial, or when future rows remain after deletion. On September
19 itself it refuses to reset if a row for that date is already `published`,
`publishing`, or `failed`.

Run on the Echo worker, after checking the flags and the worker’s calendar/store
configuration:

```sh
python scripts/reset_pierce_weekly.py --dry-run
python scripts/reset_pierce_weekly.py
```

The first command validates guards and reports the removable row count without
writing. The second creates the backup, deletes exactly the backed-up IDs within
the guarded tenant/date/status scope, and calls `scan_and_generate()` with
`now=2026-09-18` and `days=7` to stage the first Saturday–Friday block. It exits
nonzero if the first-week stage is not generated or if its audit finds missing,
non-pending, blank, repeated-old, or near-duplicate Instagram feed captions.

Do not interpret a successful process exit as approval or publication. Inspect the
persisted first-week rows and review them through the normal human approval flow.

## Recovery: stage only

If the first weekly stage fails after the backup exists, keep the backup and use:

```sh
python scripts/reset_pierce_weekly.py --stage-only
```

Recovery requires the backup to exist, match `gym_id`, `first`, and `last`, and
find no future Pierce rows already present. It does not delete again; it reuses
the saved rows as the audit baseline and stages the first week. The same weekly
scan and first-week diversity audit still apply. If the backup is missing, scope
mismatched, or future rows exist, stop and investigate rather than bypassing the
guard.

## Verification and failure boundaries

Record the command output, backup path/digest, removable/deleted row counts, and
the `Weekly scan` result. Verify that the first block is 2026-09-19 through
2026-09-25, persisted Instagram `feed`/`reel` rows are `pending`, captions are
nonblank and distinct, and no old caption or near-duplicate was accepted. Confirm
that rows outside the selected week were preserved; the `_PierceWeekStore` test
specifically covers preservation of an outside date.

Stop without notifying the client or approving anything when a guard fails, the
backup already exists unexpectedly, the calendar read is unreliable, deletion is
partial, the first-week audit fails, or live status cannot be established. The
repository proves intended code behavior and test fixtures only; it does not prove
that Railway flags, `/data`, Supabase, R2, media, approval state, or publishing
state are live and correct.

