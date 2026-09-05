# METRICS DATA CONTRACT

Owner: AGENT INTEGRATIONS (track/4-zernio-apify). Consumers: the portal metrics surface
(Track 5), Echo reporting, and anything that renders a follower or engagement number to a
client.

Status of this document: BINDING. Column names, null semantics and the health enum below
are the interface. A reader may build fixtures against this file today; the pipeline half
fills the tables behind it.

All schemas below were read from the LIVE PostgREST OpenAPI definition on the production
Supabase project on 2026-09-05, not from a migration file. Columns marked NEW are added by
an additive migration on this track and are called out as such.

---

## 1. Tables and columns

### 1.1 `gym_social_metrics_daily` — the daily series (primary read for Track 5)

One row per gym per platform account per calendar day.

| column | type | required | meaning |
| --- | --- | --- | --- |
| `id` | uuid | yes (pk) | row id |
| `gym_id` | uuid | yes (fk gyms.id) | the gym. UUID, never a slug, never an account key. |
| `late_account_id` | text | no | the Zernio account id this row was measured from. Names the account, and by extension the platform lane, when one gym has two accounts on one platform. |
| `metric_date` | date | yes | the calendar day the metric describes, in UTC. NOT the day it was fetched. |
| `followers` | integer | no | follower count at end of `metric_date`. NULL when not measured. |
| `reach` | integer | no | unique accounts reached on `metric_date`. NULL when not measured. |
| `impressions` | integer | no | impressions on `metric_date`. NULL when not measured. |
| `engagement` | integer | no | engagement actions on `metric_date`. NULL when not measured. |
| `profile_views` | integer | no | profile views on `metric_date`. NULL when not measured. |
| `raw` | jsonb | no | the upstream payload for this day plus the provenance block (section 3). |
| `pulled_at` | timestamptz | no | when this row was written. This is the as-of stamp (section 2). |
| `platform` | text | NEW, no | `instagram` \| `facebook` \| `googlebusiness`. Denormalised from the account so a reader never has to join to know the lane. NULL only on rows written before the migration. |
| `source` | text | NEW, no | `zernio` \| `apify`. Which lane produced this row. NULL only on rows written before the migration. |

Uniqueness: one row per (`gym_id`, `late_account_id`, `metric_date`). A re-pull of the same
day UPDATES that row; it never appends a second one. A reader may assume at most one row per
that triple and does not need to de-duplicate.

### 1.2 `post_metrics` — per post snapshots

Primary key is (`gym_id`, `platform`, `platform_post_id`, `snapshot_day`).

`gym_id` here is TEXT and carries the Echo account key, NOT the gyms.id uuid. This differs
from `gym_social_metrics_daily`. Do not join the two on `gym_id` without translating.

`snapshot_day` is the age of the post in days at the moment of the snapshot, and is
constrained to the ladder in section 5: **1, 7, 28**. Historical rows carrying `3` exist and
are valid history; no new row is written at 3.

`external` is true for posts published before Echo took over the account. External rows
inform the before window and the baseline. They MUST NOT be used to train the playbook and
MUST NOT be presented to a client as Echo's work.

`is_ad` true means the post was boosted or run as an ad. Organic reporting excludes these.

### 1.3 `social_baseline` — the frozen before window

One IMMUTABLE row per gym, primary key `gym_id` (text, the Echo account key). The writer is
a plain INSERT: a second capture conflicts and is refused. `measures` is jsonb. `window_start`
and `window_end` bracket the 90 days of public feed ending at `echo_start`.

A baseline row is a historical claim about a period before Echo. It is never recomputed and
never backfilled with newer numbers.

### 1.4 Connection state

`echo_social_connections` is the single source of truth for whether a gym is connected on a
platform. `gym_social_accounts` is legacy and is being retired on this track. Do not add a
new read against `gym_social_accounts`. See the AUD-005 note in the track PR.

---

## 2. The as-of stamp

Every metric a reader renders carries an as-of stamp so a client is never shown a stale
number as if it were live.

Shape, in every payload that leaves the pipeline:

```json
{
  "as_of": {
    "metric_date": "2026-09-04",
    "fetched_at": "2026-09-05T06:12:44.113Z",
    "is_stale": false,
    "staleness_days": 1
  }
}
```

- `metric_date` (date, string `YYYY-MM-DD`): the newest day the series actually covers.
- `fetched_at` (RFC 3339 UTC, `Z` suffix): `pulled_at` of the newest row. When the pipeline
  has never run for this gym, `fetched_at` is `null` and `metric_date` is `null`. Never
  substitute "now".
- `staleness_days` (integer): whole days between `metric_date` and today, UTC. `null` when
  `metric_date` is `null`.
- `is_stale` (boolean or null): true when `staleness_days` is greater than 2. `null` when
  `staleness_days` is `null`. A reader that cannot tell staleness must say so, not guess.

A surface that has no as-of stamp must not render a number.

---

## 3. Provenance

Every number that can reach a client report is traceable to the call that produced it. The
provenance block lives at `raw->'_provenance'` on `gym_social_metrics_daily`, and is
required on every row this track writes.

Zernio lane:

```json
{
  "_provenance": {
    "source": "zernio",
    "endpoint": "/v1/accounts/{accountId}/follower-stats",
    "account_id": "6a721f3bd0fe733d1a0e85ef",
    "profile_id": "6a69fc6040a7a3c860fb5450",
    "fetched_at": "2026-09-05T06:12:44.113Z"
  }
}
```

Apify lane (before window backfill only):

```json
{
  "_provenance": {
    "source": "apify",
    "actor_id": "apify/instagram-profile-scraper",
    "build_id": "…",
    "run_id": "…",
    "dataset_id": "…",
    "fetched_at": "2026-09-05T06:12:44.113Z"
  }
}
```

Rules:
- `source` is one of exactly `zernio` or `apify`. It also appears as the top level `source`
  column so a reader can filter without opening jsonb.
- The Apify lane MUST carry `actor_id`, `build_id` and `run_id`. A scraped number with no
  run id is not admissible and must not be written.
- `fetched_at` in the provenance block always equals the row's `pulled_at`.
- A row with no `_provenance` block is untrusted. A client facing surface must not render it.

---

## 4. Null semantics

**NULL MEANS NULL. A missing metric is never zero.**

- A metric column is NULL when the number was not measured, not returned, or not supported
  for that platform. It is 0 only when the upstream explicitly reported 0.
- A reader MUST NOT apply `COALESCE(x, 0)`, `x || 0`, `x ?? 0`, `Number(x)` on null, or any
  other zero fill on a metric column. A dash, "not measured", or an omitted row is the
  correct rendering.
- A sum or average over a window skips nulls and reports the count of days it actually had.
  A 30 day average built from 4 measured days is reported as an average over 4 days, or not
  reported at all. It is never divided by 30.
- A delta between two points is NULL if either endpoint is NULL.
- A rate whose denominator is NULL or 0 is NULL, not 0 and not infinity.
- `googlebusiness` does not report follower counts. Those rows carry NULL `followers`
  permanently, and that is correct, not a gap to be filled.

This is enforced in code, not by convention. The writer refuses a row that would coerce a
missing upstream field into 0, and the pipeline has a regression test asserting it.

---

## 5. The snapshot ladder

`post_metrics.snapshot_day` is normalised to a fixed ladder: **1, 7, 28**.

- Day 1 captures the initial push, day 7 the settled organic reach, day 28 the long tail.
- A post younger than the rung has no row at that rung. That is an absent row, not a zero row.
- A gym onboarded fewer than 28 days ago legitimately has no day 28 rows. A reader must not
  read that absence as a decline.
- Uneven historical coverage exists (defect D3). The ladder normalisation lands on this
  track; until it does, a reader must treat rung coverage as per gym, never assume uniform.

---

## 6. Health read

The single enum a surface may render for the direction of a gym's social growth:

```
growing | flat | declining
```

Exactly those three lowercase strings, plus `null`.

Computation, over the `followers` series in `gym_social_metrics_daily` for one gym and one
platform:

1. Take the measured (non NULL) follower points in the trailing 28 day window.
2. **If there are fewer than 2 measured points, or the window they span is under 14 days,
   the health read is `null`.** Not `flat`. Insufficient data is not a finding.
3. Let `delta` be last minus first, and `pct` be `delta / first` where `first` is greater
   than 0. If `first` is 0, the health read is `null`.
4. `growing` when `pct >= +0.01`. `declining` when `pct <= -0.01`. `flat` otherwise.

`flat` is a measured result meaning the account moved less than one percent over a real
window. `null` means we do not know. A surface must render those differently, and must never
print `flat` when it means `null`.

The payload shape:

```json
{
  "health": "growing",
  "health_basis": {
    "points": 21,
    "window_start": "2026-08-08",
    "window_end": "2026-09-04",
    "first": 812,
    "last": 847,
    "pct": 0.0431
  }
}
```

When `health` is `null`, `health_basis` still carries `points` and the window so a surface
can explain why.

---

## 7. Copy rules for anything client facing

Strings built from this data carry no em dashes, no en dashes and no hyphens, and never use
the word "vendor" or "vendors".
