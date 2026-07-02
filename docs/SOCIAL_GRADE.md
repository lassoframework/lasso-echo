# Social Grade v1 rubric

Flag: `AGENT_GRADE_ENABLED` (default OFF). Computed per account by the reporting
assembler (`agent/reporting.py: compute_grade`) and summarized in one Slack line
(`grade_summary_line`). HONEST GRADES, no fake green: a missing input never
invents a score; that subscore is None, is listed in `gaps`, and does not vote.
If nothing can be scored, the grade is "not enough data to grade honestly".

## Letter scale

Overall score = the mean of the subscores that have real data.

| Letter | Score |
|--------|-------|
| A | 90 to 100 |
| B | 80 to 89 |
| C | 70 to 79 |
| D | 60 to 69 |
| F | below 60 |

## Subscores (each 0 to 100 or None)

1. **Consistency** - published posts vs planned posts for the window:
   `min(1, published / planned) * 100`. None when either count is missing.
2. **Mix** - balance across content pillars: `min(count) / max(count) * 100`
   over the window's pillar counts. A single-pillar window scores 40 (on message
   but not balanced). None when no pillar counts are available.
3. **Engagement trend** - the views-based engagement rate vs its baseline window:
   above +5 percent scores 90, within plus or minus 5 percent scores 70, below
   -5 percent scores 40. None when either rate is missing (never impressions).
4. **Growth trend** - follower growth rate for the window: above 2 percent scores
   90, zero to 2 percent scores 70, negative scores 40. None when unknown.
5. **Verified proof usage** - at least one verified, permissioned social proof
   post in the window scores 100; none used scores 40 (proof converts). None when
   usage is not tracked for the window.

## Before / after posting frequency

When `/data/baseline_<YYYY-MM>.json` exists (written by
`python -m agent capture-baseline` before Echo started posting), the grade payload
carries `posting_freq_before` (the pre-Echo avg posts per week) next to
`posting_freq_after` (the window's count) and the Slack line shows
`posts/wk <before> before -> <after> now`. A missing baseline file is listed as a
gap, never estimated.
