"""monthly_retro.py — Wave 7.8: the monthly retro. Runs on the 5th for the
PRIOR month, per gym, behind AGENT_LEARNING_LOOP (default OFF).

The loop closed: plan -> gate at A -> publish -> measure -> LEARN -> plan
better. For each gym with a closed month of matured post_metrics:

  1. Pull the closed month's matured metrics; check taint.
  2. Score every lever vs the gym's rolling 90-day recency-weighted baseline;
     evaluate the month's labeled experiment.
  3. Findings: top 3 keep-doing (with evidence rows), top 3 stop-doing, the
     experiment verdict, and next month's experiment.
  4. Update gym_playbook WITHIN BOUNDS (plus or minus 20% drift, protected keys
     refused — agent/playbook.py); write the monthly_retro row.
  5. Post the digest to the gym's coach channel; LASSO's own retro posts to #ops.
  6. NEVER cite a number that is not backed by a post_metrics row — every
     finding carries the evidence row keys it was computed from.

HONESTY (the Wave 7.4 guards, all enforced here):
  - external rows inform the BASELINE but never train the playbook;
  - a lever value below the sample floor (6) is an observation, never a claim;
  - adoption needs the two-month persistence rule (or one month at 12+/side);
  - a TAINTED month (second publisher, follower spike > 20%, paid boosts) is
    observed and stored but trains NOTHING — the playbook does not move;
  - reels compare against reels, photos against photos.

Fully injectable — run(month=None, gyms=None, store=None, now=None,
notifier=None) — so synthetic months test the whole path deterministically.
DO NOT run against real data until Blake flips the flags per
WAVE6_HUMAN_TAPS.md TAP 3 (metrics first, retro only after a full closed month
of clean metrics).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from agent import config, learning_guards as guards, learning_score, playbook as pb_mod

TOP_N = 3


# ---------------------------------------------------------------------------
# month math
# ---------------------------------------------------------------------------

def prior_month(now) -> str:
    d = now.date() if isinstance(now, datetime) else now
    first = d.replace(day=1)
    prev_last = first.toordinal() - 1
    return date.fromordinal(prev_last).isoformat()[:7]


def next_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"


def _month_before(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


# ---------------------------------------------------------------------------
# pure scoring pipeline
# ---------------------------------------------------------------------------

def row_key(row) -> str:
    """The evidence key of one post_metrics row: platform:platform_post_id:dN."""
    return (f"{row.get('platform')}:{row.get('platform_post_id')}"
            f":d{row.get('snapshot_day')}")


def scoring_rows(metrics_rows):
    """Day-7 rows (the scoring snapshot), with follows attribution merged in
    from the day-28 row of the same post when it exists. Returns scored dicts:
    {key, score, published_at, external, format stratum, levers...}. A row with
    no honest denominator (score None) is dropped, never zero-filled."""
    day7 = {}
    day28 = {}
    for r in metrics_rows or []:
        if not isinstance(r, dict):
            continue
        pid = (r.get("platform"), r.get("platform_post_id"))
        if r.get("snapshot_day") == learning_score.SCORING_SNAPSHOT_DAY:
            day7[pid] = r
        elif r.get("snapshot_day") == learning_score.FOLLOWS_SNAPSHOT_DAY:
            day28[pid] = r
    out = []
    for pid, r in sorted(day7.items(), key=lambda kv: str(kv[0])):
        merged = dict(r)
        late = day28.get(pid)
        if late is not None and late.get("follows") is not None:
            merged["follows"] = late.get("follows")  # day-28: follows attribution only
        s = learning_score.score(merged)
        if s is None:
            continue
        out.append({
            "key": row_key(r),
            "score": s,
            "published_at": r.get("published_at"),
            "external": bool(r.get("external")),
            # is_ad (20260827): a boosted/paid post — observed only, same
            # treatment as external (informs the baseline, never trains).
            "is_ad": bool(r.get("is_ad")),
            "format": str(r.get("format") or r.get("media_product_type")
                          or "unknown").lower(),
            "pillar": r.get("pillar"),
            "hook_family": r.get("hook_family"),
            "ask_type": r.get("ask_type"),
            "time_slot": r.get("time_slot"),
            "caption_len_band": r.get("caption_len_band"),
            # experiment_label rides in when the caller joined it from the
            # calendar row (7.7); absent -> not an experiment row.
            "experiment_label": r.get("experiment_label"),
        })
    return out


LEVERS = ("hook_family", "ask_type", "time_slot", "caption_len_band", "pillar")


def lever_stats(scored, now):
    """Format-stratified, recency-weighted lever stats over INTERNAL ORGANIC
    scored posts: {lever: {(format, value): {n, mean_score, evidence[]}}}.
    External posts never reach this function (guard: we don't learn from posts
    we didn't shape), and neither do is_ad posts (paid reach poisons organic
    lever comparisons — observed only, same treatment as external)."""
    strata = guards.stratify_by_format(
        [p for p in scored if not p["external"] and not p.get("is_ad")])
    stats = {}
    for fmt, posts in sorted(strata.items()):
        for lever in LEVERS:
            groups = {}
            for p in posts:
                val = p.get(lever)
                if not val:
                    continue
                groups.setdefault(str(val), []).append(p)
            for val, grp in sorted(groups.items()):
                mean = guards.weighted_mean_score(grp, now)
                if mean is None:
                    continue
                stats.setdefault(lever, {})[(fmt, val)] = {
                    "n": len(grp),
                    "mean_score": mean,
                    "evidence": sorted(p["key"] for p in grp),
                }
    return stats


def gym_baseline(scored, now):
    """The gym's rolling recency-weighted baseline score. External posts DO
    inform the baseline (the whole feed is the gym's reality) — they just never
    train the playbook."""
    return guards.weighted_mean_score(scored, now)


def rank_findings(stats, baseline):
    """Keep/stop candidates: every lever value AT OR ABOVE the sample floor,
    ranked by relative lift vs the gym baseline. Below-floor values never make
    a claim. Returns (keep_top3, stop_top3), each a list of finding dicts that
    carry their evidence row keys."""
    candidates = []
    for lever, values in sorted((stats or {}).items()):
        for (fmt, val), st in sorted(values.items()):
            if not guards.sample_floor(st["n"]):
                continue  # observation only, never a claim
            lift = guards.relative_lift(st["mean_score"], baseline)
            if lift is None:
                continue
            candidates.append({
                "lever": lever, "value": val, "format": fmt,
                "n": st["n"],
                "mean_score": round(st["mean_score"], 4),
                "baseline": round(baseline, 4),
                "lift": round(lift, 4),
                "evidence": st["evidence"],
            })
    keep = sorted(candidates, key=lambda c: (-c["lift"], c["lever"], c["value"]))[:TOP_N]
    stop = sorted(candidates, key=lambda c: (c["lift"], c["lever"], c["value"]))[:TOP_N]
    stop = [c for c in stop if c["lift"] < 0]  # only genuine underperformers
    return keep, stop


def experiment_verdict(scored, month):
    """Evaluate the month's labeled experiment (rows whose calendar carried
    experiment_label '<lever>:<month>' — surfaced on the metrics row as
    experiment_label). Both sides must clear the sample floor for a verdict;
    otherwise it is honestly inconclusive."""
    exp = [p for p in scored if not p["external"] and not p.get("is_ad")
           and p.get("experiment_label")]
    if not exp:
        return {"status": "none", "detail": "no labeled experiment rows this month"}
    label = exp[0]["experiment_label"]
    lever = label.split(":", 1)[0]
    control = [p for p in scored
               if not p["external"] and not p.get("is_ad")
               and not p.get("experiment_label")]
    if not guards.sample_floor(exp) or not guards.sample_floor(control):
        return {"status": "inconclusive", "label": label,
                "detail": f"below sample floor ({len(exp)} experiment / "
                          f"{len(control)} control; floor {guards.MIN_SAMPLE})"}
    exp_mean = sum(p["score"] for p in exp) / len(exp)
    ctl_mean = sum(p["score"] for p in control) / len(control)
    lift = guards.relative_lift(exp_mean, ctl_mean)
    return {"status": "evaluated", "label": label, "lever": lever,
            "experiment_n": len(exp), "control_n": len(control),
            "experiment_score": round(exp_mean, 4),
            "control_score": round(ctl_mean, 4),
            "lift": round(lift, 4) if lift is not None else None,
            "evidence": sorted(p["key"] for p in exp + control)}


def _best_pair(stats, lever):
    """The (winner, alternative) stat pair for one lever within ONE format
    stratum (reels vs reels, photos vs photos), both above the sample floor, or
    None. The stratum with the largest combined n wins."""
    values = (stats or {}).get(lever) or {}
    by_fmt = {}
    for (fmt, val), st in values.items():
        if guards.sample_floor(st["n"]):
            by_fmt.setdefault(fmt, []).append((val, st))
    best = None
    for fmt, pairs in sorted(by_fmt.items()):
        if len(pairs) < 2:
            continue
        ranked = sorted(pairs, key=lambda kv: -kv[1]["mean_score"])
        combined = ranked[0][1]["n"] + ranked[1][1]["n"]
        if best is None or combined > best[0]:
            best = (combined, fmt, ranked[0], ranked[1])
    if best is None:
        return None
    _, fmt, (wval, wst), (aval, ast) = best
    return {"format": fmt, "winner": wval, "winner_stat": wst,
            "alternative": aval, "alternative_stat": ast}


def propose_changes(stats, prev_stats, current_playbook):
    """The bounded playbook proposal for the month, honesty guards first:
    a lever change is proposed ONLY when the persistence rule passes (two
    consecutive months at >= 30% lift, or one month at 12+ per side). Returns
    (changes, evidence, adopted[]) — empty when nothing qualifies, so the
    playbook does not move on noise."""
    changes = {}
    evidence = []
    adopted = []
    lever_key = {"hook_family": "hook_family_weights", "pillar": "pillar_weights"}
    for lever in ("hook_family", "pillar"):
        pair = _best_pair(stats, lever)
        if pair is None:
            continue
        cur_month = {
            "winner_score": pair["winner_stat"]["mean_score"],
            "alternative_score": pair["alternative_stat"]["mean_score"],
            "winner_n": pair["winner_stat"]["n"],
            "alternative_n": pair["alternative_stat"]["n"],
        }
        months = []
        prev_pair = _best_pair(prev_stats or {}, lever)
        if prev_pair is not None and prev_pair["winner"] == pair["winner"]:
            months.append({
                "winner_score": prev_pair["winner_stat"]["mean_score"],
                "alternative_score": prev_pair["alternative_stat"]["mean_score"],
                "winner_n": prev_pair["winner_stat"]["n"],
                "alternative_n": prev_pair["alternative_stat"]["n"],
            })
        months.append(cur_month)
        if not guards.persistence_rule(months):
            continue  # observation, not adoption
        key = lever_key[lever]
        weights = dict((current_playbook or {}).get(key) or {})
        cur_w = weights.get(pair["winner"])
        base = cur_w if isinstance(cur_w, (int, float)) and cur_w > 0 else 1.0
        weights[pair["winner"]] = round(base * (1.0 + pb_mod.DRIFT_CAP), 4)
        changes[key] = weights
        evidence.extend(pair["winner_stat"]["evidence"])
        evidence.extend(pair["alternative_stat"]["evidence"])
        adopted.append({"lever": lever, "winner": pair["winner"],
                        "alternative": pair["alternative"],
                        "format": pair["format"]})
    # time slots: reorder toward the winning slot only under the same guards.
    pair = _best_pair(stats, "time_slot")
    if pair is not None:
        prev_pair = _best_pair(prev_stats or {}, "time_slot")
        months = []
        if prev_pair is not None and prev_pair["winner"] == pair["winner"]:
            months.append({
                "winner_score": prev_pair["winner_stat"]["mean_score"],
                "alternative_score": prev_pair["alternative_stat"]["mean_score"],
                "winner_n": prev_pair["winner_stat"]["n"],
                "alternative_n": prev_pair["alternative_stat"]["n"]})
        months.append({
            "winner_score": pair["winner_stat"]["mean_score"],
            "alternative_score": pair["alternative_stat"]["mean_score"],
            "winner_n": pair["winner_stat"]["n"],
            "alternative_n": pair["alternative_stat"]["n"]})
        if guards.persistence_rule(months):
            slots = list((current_playbook or {}).get("top_time_slots") or [])
            slots = [pair["winner"]] + [s for s in slots if s != pair["winner"]]
            changes["top_time_slots"] = slots[:3]
            evidence.extend(pair["winner_stat"]["evidence"])
            adopted.append({"lever": "time_slot", "winner": pair["winner"],
                            "alternative": pair["alternative"],
                            "format": pair["format"]})
    return changes, sorted(set(evidence)), adopted


# ---------------------------------------------------------------------------
# digest (every number backed by an evidence row)
# ---------------------------------------------------------------------------

def build_digest(gym_id, month, findings, tainted):
    """The coach-channel digest. HARD RULE: every number in this text is
    computed from the evidence rows carried in the findings — nothing cited
    that a post_metrics row cannot back. Scrubbed through copy_gate (no dashes
    in client-facing text)."""
    lines = [f"ECHO monthly retro, {gym_id}, {month}"]
    if tainted:
        lines.append("This month was TAINTED (second publisher, follower spike, "
                     "or paid boosts). Observed only. The playbook did not move.")
    keeps = findings.get("keep_doing") or []
    if keeps:
        lines.append("Keep doing:")
        for f in keeps:
            lines.append(
                f"  {f['lever']} = {f['value']} ({f['format']}): "
                f"avg score {f['mean_score']} vs baseline {f['baseline']}, "
                f"n={f['n']} posts, {len(f['evidence'])} evidence rows")
    stops = findings.get("stop_doing") or []
    if stops:
        lines.append("Stop doing:")
        for f in stops:
            lines.append(
                f"  {f['lever']} = {f['value']} ({f['format']}): "
                f"avg score {f['mean_score']} vs baseline {f['baseline']}, "
                f"n={f['n']} posts, {len(f['evidence'])} evidence rows")
    exp = findings.get("experiment") or {}
    if exp.get("status") == "evaluated":
        lines.append(
            f"Experiment {exp['label']}: score {exp['experiment_score']} vs "
            f"control {exp['control_score']} "
            f"(n={exp['experiment_n']} vs {exp['control_n']})")
    elif exp.get("status") == "inconclusive":
        lines.append(f"Experiment {exp.get('label')}: inconclusive, {exp['detail']}")
    nxt = findings.get("next_experiment")
    if nxt:
        lines.append(f"Next month's experiment: {nxt}")
    # Demographics line (20260827): present ONLY when a stored
    # gym_audience_demographics row backs it — never guessed.
    demo = findings.get("demographics")
    if demo:
        lines.append(demo)
    # Deny volume (recreate-budget usage): present ONLY when a real count was
    # read from content_calendar — never guessed (2x-cadence watch item, D8).
    deny = findings.get("deny_volume")
    if deny:
        lines.append(deny)
    adopted = findings.get("adopted") or []
    if adopted:
        for a in adopted:
            lines.append(f"Playbook updated: {a['lever']} now favors "
                         f"{a['winner']} over {a['alternative']} ({a['format']})")
    else:
        lines.append("Playbook unchanged this month (guards held).")
    # SINCE ECHO STARTED (20260828, flag AGENT_SOCIAL_BASELINE): the before vs
    # after block from the PUBLIC Instagram feed via Apify — present ONLY when
    # a stored immutable baseline and a fresh after-pull back every number.
    since_echo = findings.get("since_echo")
    if since_echo:
        lines.extend(since_echo)
    text = "\n".join(lines)
    try:
        from agent.copy_gate import scrub
        return scrub(text)
    except Exception:
        return text


# ---------------------------------------------------------------------------
# default store + notifier (both injectable)
# ---------------------------------------------------------------------------

class SupabaseRetroStore:
    """Default store: post_metrics reads, taint signals, monthly_retro writes,
    plus the playbook store. `http` injectable. content_calendar is never
    written here."""

    def __init__(self, url=None, service_key=None, http=None):
        self._url = url if url is not None else config.supabase_url()
        self._key = service_key if service_key is not None else config.supabase_service_key()
        self._http = http
        self.playbook_store = pb_mod.SupabasePlaybookStore(
            url=self._url, service_key=self._key, http=http)

    def _client(self):
        if self._http is not None:
            return self._http
        import requests
        return requests

    def _headers(self, extra=None):
        h = {"apikey": self._key, "Authorization": f"Bearer {self._key}",
             "Accept": "application/json"}
        if extra:
            h.update(extra)
        return h

    def month_metrics(self, gym_id, month):
        first = f"{month}-01"
        nxt = f"{next_month(month)}-01"
        r = self._client().get(
            f"{self._url}/rest/v1/post_metrics",
            params={"gym_id": f"eq.{gym_id}",
                    "published_at": [f"gte.{first}", f"lt.{nxt}"],
                    "order": "published_at"},
            headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            return []
        return r.json() or []

    def taint_signals(self, gym_id, month):
        """Assemble the month's taint signals from the data we hold: external
        post activity (second publisher) from post_metrics. Follower spikes and
        paid boosts have no trustworthy source yet, so they are reported as
        unchecked (never silently assumed clean)."""
        rows = self.month_metrics(gym_id, month)
        external = [r for r in rows if r.get("external")]
        return {"second_publisher_active": len(external) > 0,
                "follower_spike_pct": None,
                "paid_boosts": None,
                "unchecked": ["follower_spike_pct", "paid_boosts"]}

    def deny_count(self, gym_id, month):
        """COUNT of this gym's DENIED content_calendar rows dated in `month`
        (YYYY-MM): the real recreate-budget usage number the digest cites.
        Read-only; returns None on any failure (the digest then omits the line,
        never guesses)."""
        try:
            from calendar import monthrange as _mr
            last = _mr(int(month[:4]), int(month[5:7]))[1]
            r = self._client().get(
                f"{self._url}/rest/v1/content_calendar",
                params={"gym_id": f"eq.{gym_id}", "status": "eq.denied",
                        "post_date": [f"gte.{month}-01", f"lte.{month}-{last:02d}"],
                        "select": "id"},
                headers=self._headers({"Prefer": "count=exact",
                                       "Range": "0-0"}),
                timeout=30,
            )
            if r.status_code >= 400:
                return None
            cr = r.headers.get("content-range") or r.headers.get("Content-Range") or ""
            total = cr.rsplit("/", 1)[-1]
            return int(total) if total.isdigit() else None
        except Exception:  # noqa: BLE001
            return None

    def insert_retro(self, row):
        r = self._client().post(
            f"{self._url}/rest/v1/monthly_retro",
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "return=representation"}),
            json=row, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"monthly_retro insert failed: {r.status_code}")
        out = r.json() or []
        return out[0] if out else row


def _default_notifier(gym_id, text):
    """Coach-channel digest post (the existing notifier pattern): the gym's
    approval channel via SlackPoster.post_notice; LASSO's own retro goes to
    #ops via ops_alerts. Failure never takes the retro down."""
    try:
        if gym_id == "lasso":
            from agent import ops_alerts
            ops_alerts.alert(text)
            return
        from agent.slack_surface import SlackPoster
        SlackPoster().post_notice(text)
    except Exception as exc:  # noqa: BLE001
        print(f"[monthly-retro] digest post failed for {gym_id}: "
              f"{type(exc).__name__}")


def _default_gyms():
    try:
        from agent.calendar_autopublish import client_gym_bases
        gyms = list(client_gym_bases() or [])
        if "lasso" not in gyms:
            gyms = ["lasso"] + gyms
        return gyms
    except Exception:
        return ["lasso"]


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def retro_for_gym(gym_id, month, store, now, notifier):
    """One gym's retro: pure over the injected store. Returns the retro row."""
    metrics = store.month_metrics(gym_id, month) or []
    signals = store.taint_signals(gym_id, month) or {}
    tainted = guards.month_is_tainted(gym_id, month, signals)

    scored = scoring_rows(metrics)
    baseline = gym_baseline(scored, now)
    stats = lever_stats(scored, now)

    keep, stop = ([], [])
    if baseline is not None:
        keep, stop = rank_findings(stats, baseline)
    exp = experiment_verdict(scored, month)

    playbook_diff = {}
    adopted = []
    refused = []
    if not tainted and baseline is not None:
        prev = _month_before(month)
        prev_scored = scoring_rows(store.month_metrics(gym_id, prev) or [])
        prev_stats = lever_stats(prev_scored, now)
        current_pb = pb_mod.load_playbook(gym_id, store=store.playbook_store)
        changes, evidence, adopted = propose_changes(stats, prev_stats, current_pb)
        if changes and evidence:
            result = pb_mod.propose_update(
                gym_id, changes, evidence, store=store.playbook_store, now=now)
            refused = result.get("refused") or []
            if result.get("wrote"):
                playbook_diff = changes
            else:
                adopted = []

    # Demographics line (20260827, flag AGENT_AUDIENCE_DEMOGRAPHICS): cite the
    # newest STORED engaged-audience row when one exists. Flag OFF or no row ->
    # no line, no store constructed. Never fails the retro.
    demographics_line = None
    if config.audience_demographics_enabled():
        try:
            from agent.jobs import demographics_sync as _demo
            if hasattr(store, "latest_demographics"):
                engaged_row = store.latest_demographics(gym_id, "engaged")
            else:
                engaged_row = _demo.SupabaseDemographicsStore().latest(
                    gym_id, "engaged")
            demographics_line = _demo.digest_line(engaged_row)
        except Exception as exc:  # noqa: BLE001
            print(f"[monthly-retro] demographics read failed for {gym_id}: "
                  f"{type(exc).__name__}")

    # DENY VOLUME (CADENCE_SPEC.md D8 addition, Blake 2026-08-27): surface the
    # gym's recreate-budget usage so the 2x-cadence watch item has a real number.
    # Read-only count of denied content_calendar rows in the month; a store
    # without the reader (test fakes, legacy) or a failed read -> no line, never
    # a guessed number, never a failed retro.
    deny_line = None
    try:
        if hasattr(store, "deny_count"):
            _denies = store.deny_count(gym_id, month)
            if _denies is not None:
                from agent.portal_social import MONTHLY_RECREATE_BUDGET as _BUDGET
                deny_line = (f"Denies this month: {int(_denies)} of {_BUDGET} "
                             "recreate budget used")
    except Exception as exc:  # noqa: BLE001
        print(f"[monthly-retro] deny-count read failed for {gym_id}: "
              f"{type(exc).__name__}")

    # SINCE ECHO STARTED (20260828, flag AGENT_SOCIAL_BASELINE): before vs after
    # on the public Instagram feed via Apify — the stored immutable pre-Echo
    # baseline against a fresh last-90 pull, same rubric. Flag OFF, no APIFY_TOKEN,
    # no baseline, or no handle -> no block, never a guessed number, never a
    # failed retro. Injectable: a store carrying since_echo_block (test fakes)
    # is used instead of the live module.
    since_echo_lines = None
    if config.social_baseline_enabled():
        try:
            if hasattr(store, "since_echo_block"):
                since_echo_lines = store.since_echo_block(gym_id)
            else:
                from agent import social_baseline as _sb
                since_echo_lines = _sb.since_echo_lines(gym_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[monthly-retro] since-echo read failed for {gym_id}: "
                  f"{type(exc).__name__}")

    findings = {
        "keep_doing": keep,
        "stop_doing": stop,
        "demographics": demographics_line,
        "deny_volume": deny_line,
        "since_echo": since_echo_lines,
        "experiment": exp,
        "next_experiment": pb_mod.experiment_lever_for(gym_id, next_month(month)),
        "adopted": adopted,
        "refused": refused,
        "taint_signals": signals,
        "baseline": round(baseline, 4) if baseline is not None else None,
        "scored_posts": len(scored),
    }
    row = {
        "gym_id": gym_id,
        "month": f"{month}-01",
        "findings": findings,
        "playbook_diff": playbook_diff,
        "tainted": tainted,
    }
    store.insert_retro(row)
    digest = build_digest(gym_id, month, findings, tainted)
    if notifier is not None:
        try:
            notifier(gym_id, digest)
        except Exception as exc:  # noqa: BLE001
            print(f"[monthly-retro] notifier failed for {gym_id}: "
                  f"{type(exc).__name__}")
    row["digest"] = digest
    return row


def run(month=None, gyms=None, store=None, now=None, notifier=None):
    """The monthly retro across gyms. Behind AGENT_LEARNING_LOOP (default OFF
    -> no-op; no store constructed, nothing read or written). `month` defaults
    to the PRIOR month of `now` (the job runs the 5th for the closed month).
    Fully injectable for synthetic-month testing — DO NOT point at real data
    until the flags are armed by hand."""
    if not config.learning_loop_enabled():
        return {"ok": False, "reason": "AGENT_LEARNING_LOOP is OFF (default). "
                                       "No retro run.", "gyms": []}
    now = now or datetime.now(timezone.utc)
    month = month or prior_month(now)
    store = store or SupabaseRetroStore()
    notifier = notifier if notifier is not None else _default_notifier
    gyms = list(gyms) if gyms else _default_gyms()

    results = []
    for gym_id in gyms:
        try:
            results.append(retro_for_gym(gym_id, month, store, now, notifier))
        except Exception as exc:  # noqa: BLE001
            results.append({"gym_id": gym_id, "ok": False,
                            "reason": f"retro failed: {type(exc).__name__}"})
    return {"ok": True, "month": month, "gyms": results}


if __name__ == "__main__":
    import sys
    month_arg = None
    gyms_arg = []
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--month" and i + 1 < len(args):
            month_arg = args[i + 1]
            i += 2
        elif args[i] == "--gym" and i + 1 < len(args):
            gyms_arg.append(args[i + 1])
            i += 2
        else:
            i += 1
    print(json.dumps(run(month=month_arg, gyms=gyms_arg or None),
                     indent=2, default=str))
