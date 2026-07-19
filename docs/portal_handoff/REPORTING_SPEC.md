# Reporting Spec

Source of truth: `agent/reporting.py`, `agent/reporting_live.py`.

---

## What the Portal Displays

The reporting view shows a per-gym 30-day performance summary. Echo assembles all numbers; the portal displays them.

---

## Report Shape

Verified against `build_report()` in `agent/reporting.py`. The portal should expect this structure from the planned `/api/report/<account_key>` endpoint:

```json
{
  "account_key": "districth",
  "window_days": 30,
  "engagement_rate": 0.048,
  "engagement_rate_baseline": 0.039,
  "followers": 1842,
  "followers_net": 47,
  "followers_growth_rate": 0.026,
  "posting_freq_current": 12,
  "posting_freq_baseline": 4,
  "top_posts": [
    {
      "id": "17846368219941196",
      "engagement": 312,
      "views": 6500,
      "engagement_rate": 0.048
    }
  ],
  "bottom_posts": [],
  "gaps": [],
  "health": "growing"
}
```

### Field Reference

| Field | What it is | Unit |
|---|---|---|
| `engagement_rate` | Current window engagement rate on VIEWS | Ratio (0.048 = 4.8%). Never impressions |
| `engagement_rate_baseline` | Same rate for the baseline window | Ratio |
| `followers` | Current follower count | Integer |
| `followers_net` | Net change vs baseline | Integer (positive = growth) |
| `followers_growth_rate` | `followers_net / baseline_followers` | Ratio |
| `posting_freq_current` | Posts published in the 30-day window | Integer |
| `posting_freq_baseline` | Posts published in the baseline window | Integer |
| `top_posts` | Top 3 posts by engagement | List of post objects |
| `bottom_posts` | Bottom 3 posts by engagement | List of post objects |
| `gaps` | Metrics Echo could not fill (never guessed, never fabricated) | List of strings |
| `health` | `growing`, `flat`, or `declining` | String |

### Health Read Logic

`health` is computed from two signals: follower growth rate and engagement rate vs baseline. If no signal is available, health is `flat`. Mixed votes resolve by majority. Echo never guesses a missing signal.

---

## Metrics Live Today vs Planned

| Metric | Status |
|---|---|
| Follower count via Graph API | LIVE (behind `AGENT_REPORTING_ENABLED`) |
| IG account views, reach, likes, comments, saves, shares | LIVE (behind flag) |
| Per-post engagement (views, reach, likes, saves, shares) | LIVE (behind flag) |
| Facebook page views and post engagements | LIVE (behind flag) |
| `engagement_rate` on VIEWS (not impressions) | LIVE (assembled by `build_report()`) |
| `health` read | LIVE |
| Top 3 / bottom 3 posts | LIVE |
| Posting frequency before Echo (from `capture-baseline`) | LIVE |
| Posting frequency after Echo | LIVE |
| `/api/report/<account_key>` endpoint | PLANNED |
| Portal display of report | PLANNED |
| Social grade letter + subscores | LIVE in Echo behind `AGENT_GRADE_ENABLED`; portal display PLANNED |

### Gaps the Portal Must Handle

When `gaps` is non-empty, the portal shows a note per gap: "This number was not available for this period." Never hide gaps. Never substitute zero for a missing metric.

---

## Portal Build Instructions (Phase 1)

Show a "Reporting coming soon" holding card. Do not fabricate numbers or pull from any source other than the Echo reporting endpoint. When the endpoint ships, display: follower count and net change, engagement rate labeled as "on views not impressions", health badge, posting frequency before vs after Echo, top 3 posts, and any gaps listed.
