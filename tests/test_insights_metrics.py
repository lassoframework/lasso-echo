"""
Media-type-aware insights tests (the backfill-400 patch). Fixture the allowed
metric sets from the current Graph docs per media type and assert every built
request stays inside them. Adversarial: the invalid media metric "saves" and
the deprecated "impressions" never appear in any media request; an expired
story skips with the exact reason; a permissions error NAMES the permission;
the token never appears in any output; the backfill and the snapshot use the
same builder.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from agent import backfill, db, reporting_live  # noqa: E402
from agent.accounts import Platform  # noqa: E402

TOKEN = "tok-insights-secret"

# The allowed sets per the CURRENT Graph docs for the pinned version.
ALLOWED = {
    "feed": {"views", "reach", "likes", "comments", "saved", "shares",
             "total_interactions", "profile_visits", "profile_activity",
             "follows", "reposts"},
    "reel": {"views", "reach", "likes", "comments", "saved", "shares",
             "total_interactions", "ig_reels_avg_watch_time",
             "ig_reels_video_view_total_time", "reposts"},
    "story": {"views", "reach", "replies", "navigation", "shares",
              "total_interactions", "profile_visits", "follows", "link_clicks"},
}


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {"data": []}
        self.text = str(self._body)

    def json(self):
        return self._body


class Recorder:
    def __init__(self, resp=None):
        self.requests = []
        self.resp = resp or FakeResp()

    def get(self, url, params=None, timeout=None):
        self.requests.append({"url": url, "params": dict(params or {})})
        return self.resp


# ---- per media type: only valid metrics, adversarial exclusions ---------------
def test_each_kind_requests_only_valid_metrics():
    for kind in ("feed", "reel", "story"):
        rec = Recorder()
        reporting_live.fetch_post_metrics("M1", TOKEN, http=rec,
                                          platform=Platform.INSTAGRAM, kind=kind)
        requested = set(rec.requests[0]["params"]["metric"].split(","))
        assert requested <= ALLOWED[kind], f"{kind}: {requested - ALLOWED[kind]}"
        # ADVERSARIAL: the two known 400 makers never appear
        assert "saves" not in requested, kind      # media metric is "saved"
        assert "impressions" not in requested, kind


def test_fb_page_post_never_touches_insights_namespace():
    rec = Recorder(FakeResp(200, {
        "reactions": {"summary": {"total_count": 12}},
        "comments": {"summary": {"total_count": 3}},
        "shares": {"count": 2}}))
    out = reporting_live.fetch_post_metrics("111_222", TOKEN, http=rec,
                                            platform=Platform.FACEBOOK_PAGE)
    assert "/insights" not in rec.requests[0]["url"]        # object fields only
    assert "metric" not in rec.requests[0]["params"]
    assert out == {"likes": 12, "comments": 3, "shares": 2}


def test_saved_maps_to_our_saves_column():
    rec = Recorder(FakeResp(200, {"data": [
        {"name": "saved", "values": [{"value": 9}]},
        {"name": "views", "values": [{"value": 100}]}]}))
    out = reporting_live.fetch_post_metrics("M1", TOKEN, http=rec,
                                            platform=Platform.INSTAGRAM)
    assert out["saves"] == 9 and "saved" not in out


# ---- expired story: graceful skip with the exact reason -----------------------
def test_expired_story_skips_gracefully(monkeypatch, capsys):
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    with pytest.raises(reporting_live.SkipRead, match="story insights expired"):
        reporting_live.fetch_post_metrics("S1", TOKEN, http=Recorder(),
                                          platform=Platform.INSTAGRAM,
                                          kind="story", published_at=old)
    # and through the backfill it is a SKIP line, not an error tone
    monkeypatch.setenv("AGENT_LASSO_IG_TOKEN", TOKEN)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, media_id, "
            "mode, published_at, creative_key) VALUES ('ds','lasso_ig','instagram',"
            "'c','S1','published',?, 'x.png')", (old,))
        conn.execute(
            "INSERT OR REPLACE INTO drafts (draft_id, account_key, status, day_key, "
            "draft_type, data) VALUES ('ds','lasso_ig','approved','2026-07-03',"
            "'story', '{\"is_story\": true}')")
        conn.commit()
    out = backfill.backfill_insights("lasso_ig", "2026-07-01", http=Recorder(),
                                     sleeper=lambda s: None)
    assert out["skipped"] == 1
    printed = capsys.readouterr().out
    assert "story insights expired" in printed
    rows = [r for r in db.audit_rows() if r["kind"] == "insights_skip"]
    assert any("story insights expired" in r["reason"] for r in rows)


def test_fresh_story_requests_story_metrics():
    fresh = datetime.now(timezone.utc).isoformat()
    rec = Recorder()
    reporting_live.fetch_post_metrics("S2", TOKEN, http=rec,
                                      platform=Platform.INSTAGRAM,
                                      kind="story", published_at=fresh)
    assert "replies" in rec.requests[0]["params"]["metric"]


# ---- permissions error NAMES the permission ------------------------------------
def test_permission_error_names_the_permission(monkeypatch, capsys):
    perm_resp = FakeResp(400, {"error": {
        "code": 10, "message": "(#10) Application does not have permission "
                               "for this action"}})
    with pytest.raises(RuntimeError) as e:
        reporting_live.fetch_post_metrics("M1", TOKEN, http=Recorder(perm_resp),
                                          platform=Platform.INSTAGRAM)
    assert "instagram_manage_insights" in str(e.value)
    assert "code 10" in str(e.value)                        # the honest detail
    with pytest.raises(RuntimeError) as e2:
        reporting_live.fetch_post_metrics("F1", TOKEN, http=Recorder(perm_resp),
                                          platform=Platform.FACEBOOK_PAGE)
    assert "pages_read_engagement" in str(e2.value)


# ---- token never in any output ----------------------------------------------------
def test_token_never_in_skip_output(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_LASSO_IG_TOKEN", TOKEN)
    bad = FakeResp(400, {"error": {"code": 100,
                                   "message": f"bad metric, token {TOKEN}"}})
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, media_id, "
            "mode, published_at, creative_key) VALUES ('d1','lasso_ig','instagram',"
            "'c','M1','published','2026-07-03T10:00:00','x.png')")
        conn.commit()
    backfill.backfill_insights("lasso_ig", "2026-07-01", http=Recorder(bad),
                               sleeper=lambda s: None)
    printed = capsys.readouterr().out
    assert TOKEN not in printed                             # scrubbed
    assert "code 100" in printed                            # detail still honest
    import json as _json
    assert TOKEN not in _json.dumps(db.audit_rows())


# ---- one builder, both callers ------------------------------------------------------
def test_backfill_and_snapshot_share_the_builder(monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_IG_TOKEN", TOKEN)
    assert backfill.fetch_post_metrics is reporting_live.fetch_post_metrics
    for kind in ("feed", "reel", "story"):
        assert reporting_live.media_metrics_for(Platform.INSTAGRAM, kind) == \
            reporting_live.MEDIA_METRICS[("instagram", kind)]


# ---- FB node types: no invalid field, ever (the 1861436475266813 class) -----------
FB_ALLOWED = {
    "photo": {"page_story_id"},
    "pagepost": {"reactions.summary(true)", "comments.summary(true)", "shares"},
}


def test_fb_photo_node_resolves_owner_then_reads_post():
    """A bare photo id first asks ONLY for page_story_id, then reads the owning
    post with ONLY the pagepost-valid fields. 'likes' is never requested."""
    class PhotoGraph:
        def __init__(self):
            self.requests = []

        def get(self, url, params=None, timeout=None):
            self.requests.append({"url": url, "params": dict(params or {})})
            if url.endswith("/1861436475266813"):
                return FakeResp(200, {"page_story_id": "222_333"})
            return FakeResp(200, {
                "reactions": {"summary": {"total_count": 21}},
                "comments": {"summary": {"total_count": 4}},
                "shares": {"count": 1}})

    g = PhotoGraph()
    out = reporting_live.fetch_post_metrics("1861436475266813", TOKEN, http=g,
                                            platform=Platform.FACEBOOK_PAGE)
    assert out == {"likes": 21, "comments": 4, "shares": 1}
    photo_fields = set(g.requests[0]["params"]["fields"].split(","))
    post_fields = set(g.requests[1]["params"]["fields"].split(","))
    assert photo_fields <= FB_ALLOWED["photo"]
    assert post_fields <= FB_ALLOWED["pagepost"]
    for req in g.requests:                                 # ADVERSARIAL
        assert "likes" not in req["params"].get("fields", "").replace(
            "likes.summary", "")  # the bare field never
        assert "insights" not in req["url"]
    assert g.requests[1]["url"].endswith("/222_333")       # the owning post


def test_fb_pagepost_id_reads_directly_valid_fields_only():
    rec = Recorder(FakeResp(200, {
        "reactions": {"summary": {"total_count": 5}},
        "comments": {"summary": {"total_count": 2}},
        "shares": {"count": 0}}))
    out = reporting_live.fetch_post_metrics("222_333", TOKEN, http=rec,
                                            platform=Platform.FACEBOOK_PAGE)
    assert out["likes"] == 5
    assert len(rec.requests) == 1                          # no resolve hop needed
    fields = set(rec.requests[0]["params"]["fields"].split(","))
    assert fields <= FB_ALLOWED["pagepost"]


def test_fb_photo_without_owner_skips_gracefully():
    rec = Recorder(FakeResp(200, {}))                      # no page_story_id
    with pytest.raises(reporting_live.SkipRead, match="no owning post"):
        reporting_live.fetch_post_metrics("1861436475266813", TOKEN, http=rec,
                                          platform=Platform.FACEBOOK_PAGE)
