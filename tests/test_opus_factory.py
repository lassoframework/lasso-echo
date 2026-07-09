"""
Opus video factory tests. Fully OFFLINE (fake Opus API, no network, no spend).

Part 1: all-project scan normalizes finished clips across MULTIPLE projects with
no allowlist; unfinished clips (no export url) are excluded; the master flag
gates the whole thing.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, opus_factory  # noqa: E402

POD_CLIP = {
    "id": "PODPROJ.C1", "title": "The follow up problem",
    "durationMs": 38000, "uriForExport": "https://cdn.opus/pod1.mp4",
    "score": 92, "transcript": "Most gyms do not have a lead problem.",
}
BIZ_CLIP = {
    "id": "PROJ2.C1", "title": "Booking rate math",
    "durationMs": 45000, "uriForExport": "https://cdn.opus/biz1.mp4",
    "viralityScore": "72", "transcriptText": "We book 71.9 percent of leads.",
}
UNFINISHED = {"id": "PROJ2.C2", "title": "still rendering", "durationMs": 30000}


class FakeOpus:
    """Mirrors the OpusAPI surface the factory uses: list_projects +
    list_exportable_clips(q, project_id)."""

    def __init__(self, projects, clips_by_project):
        self._projects = projects                   # [{"id","title"}]
        self._clips = clips_by_project              # {project_id: [clip,...]}

    def list_projects(self):
        return list(self._projects)

    def list_exportable_clips(self, q, source_id):
        assert q == "findByProjectId"
        return list(self._clips.get(source_id, []))

    def download(self, url):
        return b"VIDEO"


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_OPUS_FACTORY_ENABLED", "true")


def _fake():
    return FakeOpus(
        projects=[{"id": "PODPROJ", "title": "Gym Marketing Made Simple"},
                  {"id": "PROJ2", "title": "Client Webinars"}],
        clips_by_project={"PODPROJ": [POD_CLIP], "PROJ2": [BIZ_CLIP, UNFINISHED]})


# ---- flag gate -----------------------------------------------------------------------------

def test_scan_flag_off_returns_empty(monkeypatch):
    monkeypatch.delenv("AGENT_OPUS_FACTORY_ENABLED", raising=False)
    assert opus_factory.scan(api=_fake()) == []


# ---- all-project scan, no allowlist ---------------------------------------------------------

def test_scan_returns_clips_across_projects(monkeypatch):
    _arm(monkeypatch)
    records = opus_factory.scan(api=_fake())
    by_id = {r.clip_id: r for r in records}
    assert set(by_id) == {"PODPROJ.C1", "PROJ2.C1"}    # both projects, finished only
    pod = by_id["PODPROJ.C1"]
    assert pod.project_id == "PODPROJ"
    assert pod.source_title == "Gym Marketing Made Simple"
    assert pod.opus_score == 92.0
    assert pod.duration_s == 38.0
    assert pod.transcript == "Most gyms do not have a lead problem."
    biz = by_id["PROJ2.C1"]
    assert biz.source_title == "Client Webinars"
    assert biz.opus_score == 72.0                       # string coerced


def test_scan_excludes_unfinished(monkeypatch):
    _arm(monkeypatch)
    ids = {r.clip_id for r in opus_factory.scan(api=_fake())}
    assert "PROJ2.C2" not in ids                        # render-in-progress excluded


def test_scan_needs_no_allowlist(monkeypatch):
    """No AGENT_OPUS_PROJECT_IDS / collection id set: the scan still finds every
    project's clips via list_projects."""
    _arm(monkeypatch)
    monkeypatch.delenv("AGENT_OPUS_PROJECT_IDS", raising=False)
    monkeypatch.delenv("AGENT_OPUS_COLLECTION_IDS", raising=False)
    assert len(opus_factory.scan(api=_fake())) == 2


def test_scan_dedupes_clip_shared_across_projects(monkeypatch):
    _arm(monkeypatch)
    fake = FakeOpus(projects=[{"id": "A", "title": "A"}, {"id": "B", "title": "B"}],
                    clips_by_project={"A": [POD_CLIP], "B": [POD_CLIP]})
    assert len(opus_factory.scan(api=fake)) == 1


def test_normalize_rejects_unexportable():
    assert opus_factory.normalize_clip({"id": "X"}, "P") is None
    assert opus_factory.normalize_clip({"uriForExport": "http://x/y.mp4"}, "P") is None


# ---- Part 2: score gate FIRST -------------------------------------------------------------
def _rec(clip_id="C", score=95, duration_s=30, transcript="", title="",
         source_title="", project_id="P", bucket=""):
    return opus_factory.ClipRecord(
        clip_id=clip_id, project_id=project_id, source_title=source_title,
        title=title, opus_score=score, duration_s=duration_s,
        transcript=transcript, download_url="http://x/y.mp4", bucket=bucket)


def test_score_89_dropped_90_passes(monkeypatch):
    monkeypatch.delenv("AGENT_OPUS_SCORE_FLOOR", raising=False)   # default 90
    ok89, reason = opus_factory.passes_score_gate(_rec(score=89))
    assert ok89 is False and "below floor" in reason
    ok90, _ = opus_factory.passes_score_gate(_rec(score=90))
    assert ok90 is True


def test_duration_window_default_15_95(monkeypatch):
    monkeypatch.delenv("AGENT_OPUS_DURATION_MIN", raising=False)
    monkeypatch.delenv("AGENT_OPUS_DURATION_MAX", raising=False)
    assert opus_factory.passes_score_gate(_rec(duration_s=14))[0] is False
    assert opus_factory.passes_score_gate(_rec(duration_s=15))[0] is True
    assert opus_factory.passes_score_gate(_rec(duration_s=95))[0] is True
    ok, reason = opus_factory.passes_score_gate(_rec(duration_s=96))
    assert ok is False and "duration" in reason


def test_score_gate_splits_and_marks(monkeypatch):
    survivors, dropped = opus_factory.score_gate([
        _rec(clip_id="keep", score=91, duration_s=30),
        _rec(clip_id="lowscore", score=80, duration_s=30),
        _rec(clip_id="tooLong", score=99, duration_s=200),
    ])
    assert {r.clip_id for r in survivors} == {"keep"}
    dmap = {r.clip_id: r for r in dropped}
    assert dmap["lowscore"].status == "drop" and "below floor" in dmap["lowscore"].reason
    assert "duration" in dmap["tooLong"].reason


def test_score_floor_override(monkeypatch):
    monkeypatch.setenv("AGENT_OPUS_SCORE_FLOOR", "70")
    assert opus_factory.passes_score_gate(_rec(score=72))[0] is True


# ---- Part 3: bucket tagger ----------------------------------------------------------------
from agent import ops_alerts  # noqa: E402


def test_podcast_sourced_tags_podcast(monkeypatch):
    monkeypatch.delenv("AGENT_OPUS_PODCAST_SHOW", raising=False)  # default show
    r = _rec(clip_id="pod", source_title="Gym Marketing Made Simple",
             transcript="anything at all here")
    opus_factory.tag_clip(r)
    assert r.bucket == "podcast" and r.confidence == 1.0 and r.status == ""


def test_on_topic_nonpodcast_tagged_to_bucket(monkeypatch):
    r = _rec(clip_id="biz", source_title="Client Webinars",
             transcript="We book 71.9 percent and the no shows vanish once "
                        "booking is on the calendar.")
    opus_factory.tag_clip(r)
    assert r.bucket == "platform"
    assert r.confidence >= config.opus_relevance_floor()
    assert r.status == ""


def test_off_topic_held_with_alert(monkeypatch):
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: fired.append(msg))
    r = _rec(clip_id="beach", source_title="Random Vlog",
             transcript="Yesterday I went to the beach and ate ice cream.")
    opus_factory.tag_clip(r)
    assert r.status == "hold" and r.bucket == ""
    assert len(fired) == 1 and "beach" in fired[0]


def test_low_confidence_held(monkeypatch):
    monkeypatch.setenv("AGENT_OPUS_RELEVANCE_FLOOR", "0.9")
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: None)
    r = _rec(clip_id="weak", source_title="Webinars",
             transcript="A quick word on follow up.")
    opus_factory.tag_clip(r)
    assert r.status == "hold" and "below floor" in r.reason


def test_classify_empty_invents_nothing():
    assert opus_factory.classify_transcript("") == {
        "bucket": "", "confidence": 0.0, "themes": []}


# ---- Part 4: hook check -------------------------------------------------------------------
def test_strong_hook_stays_eligible():
    for t in ("Most gyms do not have a lead problem, they have follow up.",
              "We book 71.9 percent of leads while the industry books far less.",
              "Are you still chasing dead leads every single night after close?"):
        r = _rec(transcript=t)
        opus_factory.hook_check(r)
        assert r.status == "", f"strong hook demoted: {t!r}"


def test_weak_hook_shortlisted():
    r = _rec(transcript="So anyway today we sat down and chatted about things.")
    opus_factory.hook_check(r)
    assert r.status == "shortlist"
    assert "weak hook" in r.reason


def test_hook_check_does_not_revive_held():
    r = _rec(transcript="weak opening here about nothing much")
    r.status = "hold"
    r.reason = "off topic"
    opus_factory.hook_check(r)
    assert r.status == "hold"           # a held clip is never demoted/revived


def test_has_strong_hook_empty_is_false():
    assert opus_factory.has_strong_hook("") is False


# ---- Part 5: caption writer ---------------------------------------------------------------
_DASH_RE = __import__("re").compile(r"[—–‒‐-]")


def test_podcast_caption_has_footer_and_soft_cta():
    r = _rec(bucket="podcast",
             transcript="Most gyms do not have a lead problem. They have a "
                        "follow up problem. Fix the follow up and revenue follows.")
    cap = opus_factory.write_caption(r)
    assert cap.startswith("Most gyms do not have a lead problem.")
    assert opus_factory.PODCAST_FOOTER in cap
    assert "Hear the full conversation" in cap
    assert "new episode" not in cap.lower()      # evergreen, never "new episode"


def test_tier2_caption_bucket_cta_no_footer():
    r = _rec(bucket="platform",
             transcript="Your leads do not die in the ads. They die in the "
                        "handoffs between tools. One platform closes the gap.")
    cap = opus_factory.write_caption(r)
    assert opus_factory.PODCAST_FOOTER not in cap          # footer only on podcast
    assert opus_factory.BUCKET_CTA["platform"] in cap


def test_caption_no_claim_absent_from_transcript():
    transcript = ("We book 71.9 percent of leads. The industry books 18.5 "
                  "percent. Same leads, very different outcomes.")
    r = _rec(bucket="platform", transcript=transcript)
    cap = opus_factory.write_caption(r)
    assert cap                                              # a caption was written
    # every claim-bearing sentence in the caption appears in the transcript
    tl = transcript.lower()
    for sent in [s.strip() for s in cap.replace("\n", " ").split(".") if s.strip()]:
        low = sent.lower()
        if any(ch.isdigit() for ch in low):
            assert low[:30] in tl or any(low[:20] in t for t in [tl]), sent


def test_caption_dash_and_vendor_filters_fire():
    r = _rec(bucket="platform",
             transcript="Stop juggling vendors and their logins. One platform "
                        "replaces the whole stack today.")
    cap = opus_factory.write_caption(r)
    assert "vendor" not in cap.lower()
    assert _DASH_RE.search(cap) is None


def test_empty_transcript_held_no_caption():
    r = _rec(bucket="platform", transcript="")
    assert opus_factory.write_caption(r) == ""
    assert r.status == "hold"


# ---- Part 6: dedupe + no-repost ledger ----------------------------------------------------
from agent import db as _db  # noqa: E402


def _wipe_ledger(*clip_ids):
    with _db._lock, _db.connect() as conn:
        for cid in clip_ids:
            conn.execute("DELETE FROM kv WHERE key IN (?,?)",
                         (f"{opus_factory._LEDGER_DRAFTED}{cid}",
                          f"{opus_factory._LEDGER_POSTED}{cid}"))
        conn.commit()


def test_drafted_clip_not_redrafted():
    _wipe_ledger("clipA", "clipB")
    recs = [_rec(clip_id="clipA"), _rec(clip_id="clipB")]
    fresh, seen = opus_factory.dedupe(recs)
    assert {r.clip_id for r in fresh} == {"clipA", "clipB"}   # first run: both fresh
    opus_factory.mark_drafted("clipA")
    fresh2, seen2 = opus_factory.dedupe([_rec(clip_id="clipA"), _rec(clip_id="clipB")])
    assert {r.clip_id for r in fresh2} == {"clipB"}           # A already drafted
    dmap = {r.clip_id: r for r in seen2}
    assert dmap["clipA"].status == "dupe"
    _wipe_ledger("clipA", "clipB")


def test_posted_clip_not_redrafted():
    _wipe_ledger("clipP")
    opus_factory.mark_posted("clipP", when="2026-07-01")
    assert opus_factory.is_posted("clipP") is True
    fresh, seen = opus_factory.dedupe([_rec(clip_id="clipP")])
    assert fresh == []
    assert seen[0].reason.startswith("already drafted or posted")
    _wipe_ledger("clipP")


def test_ledger_survives_rerun_idempotent():
    _wipe_ledger("clipR")
    opus_factory.mark_drafted("clipR")
    # a re-run sees it every time
    for _ in range(3):
        assert opus_factory.already_seen("clipR") is True
    _wipe_ledger("clipR")


# ---- Part 7: calendar routing -------------------------------------------------------------
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402
from agent import trust  # noqa: E402


def _eligible(clip_id, bucket, caption="Hook line here.\n\nPayoff.\n\nCTA."):
    r = _rec(clip_id=clip_id, bucket=bucket)
    r.caption = caption
    return r


def _clear_ledger(*ids):
    with _db._lock, _db.connect() as conn:
        for cid in ids:
            conn.execute("DELETE FROM kv WHERE key LIKE ?", (f"opus_%{cid}",))
        conn.commit()


def test_route_flag_off_returns_empty(monkeypatch):
    monkeypatch.delenv("AGENT_OPUS_FACTORY_ENABLED", raising=False)
    assert opus_factory.route([_eligible("x", "platform")], "2026-08-03") == []


def test_route_drafts_are_pending_and_bucket_matched(monkeypatch):
    _arm(monkeypatch)
    _clear_ledger("podA", "platA")
    monkeypatch.setenv("AGENT_OPUS_WEEKLY_CAP", "9")
    recs = [_eligible("podA", "podcast"), _eligible("platA", "platform")]
    drafts = opus_factory.route(recs, "2026-08-03", weeks=2)   # 2026-08-03 = Monday
    assert drafts, "expected drafts"
    for d in drafts:
        assert d.status == DraftStatus.PENDING
        assert d.draft_type == "opus_clip"
    from agent.schedule import weekday_abbr
    by_bucket = {d.category: d for d in drafts}
    # podcast video slot is Thursday; platform video slots are Tue/Sat
    assert weekday_abbr(by_bucket["podcast"].day_key) == "thu"
    assert weekday_abbr(by_bucket["platform"].day_key) in ("tue", "sat")
    _clear_ledger("podA", "platA")


def test_route_respects_weekly_cap(monkeypatch):
    _arm(monkeypatch)
    ids = [f"c{i}" for i in range(8)]
    _clear_ledger(*ids)
    monkeypatch.setenv("AGENT_OPUS_WEEKLY_CAP", "1")
    # many platform clips; cap = 1 per ISO week
    recs = [_eligible(cid, "platform") for cid in ids]
    drafts = opus_factory.route(recs, "2026-08-03", weeks=3)
    from collections import Counter
    per_week = Counter(opus_factory._iso_week(d.day_key) for d in drafts)
    assert per_week and max(per_week.values()) <= 1, per_week
    _clear_ledger(*ids)


def test_route_nothing_auto_publishes(monkeypatch):
    _arm(monkeypatch)
    _clear_ledger("trustclip")
    monkeypatch.setenv("AGENT_TRUST_LADDER_ENABLED", "true")
    drafts = opus_factory.route([_eligible("trustclip", "platform")], "2026-08-03")
    assert drafts
    acct = Account(key="lasso_ig", display_name="IG", platform=Platform.INSTAGRAM,
                   token_env="X", target_id_env="Y", trust=trust.TrustLevel.ROUTINE_AUTO)
    d = drafts[0]
    # even at a raised level, a PENDING opus draft still requires the tap
    assert trust.requires_approval(acct, d) is True
    eligible, _reason = trust.auto_eligibility(acct, d)
    assert eligible is False
    _clear_ledger("trustclip")


def test_route_ledger_prevents_redraft(monkeypatch):
    _arm(monkeypatch)
    _clear_ledger("once")
    monkeypatch.setenv("AGENT_OPUS_WEEKLY_CAP", "9")
    first = opus_factory.route([_eligible("once", "platform")], "2026-08-03", weeks=2)
    assert len(first) == 1
    # re-run after dedupe: the clip is in the ledger, so it is filtered out
    fresh, seen = opus_factory.dedupe([_eligible("once", "platform")])
    assert fresh == [] and seen[0].status == "dupe"
    _clear_ledger("once")
