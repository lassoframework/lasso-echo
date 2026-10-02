"""
Story failure-handling tests (Echo channel repair, 2026-09-21).

Scope: agent/stories.py passes a per-call failure_info={} plus the story
draft_id into creative_studio.generate, and handles the three distinct
terminal outcomes accurately:
  * QUALITY rejection already reported by the studio -> NO redundant story
    ops alert (the studio's alert stands), just an accurate local log line.
  * HOSTING failure (render succeeded, no public URL) -> one distinct ops
    alert that names hosting, not the studio.
  * RENDER UNAVAILABLE / unknown failure -> the existing single dark-studio
    ops alert still fires; unknown failures are never suppressed.
Plus: failure_info and draft_id are actually threaded through, feed assets are
never cropped/reused, the no-9:16-by-design skip stays log-only, and the
dormant flag behavior is unchanged.

Fully offline: creative_studio.generate and media_host.host_media are stubbed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, stories  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402

DAY = "2027-07-07"  # a Wednesday: a posting day under the default cadence

QUALITY_INFO = {
    "reason": "wordmark above y=.10 and CTA below y=.85 on every attempt",
    "stage": "quality",
    "reported": True,
    "attempts": 3,
}


def _acct(key="lasso_ig"):
    return Account(key=key, display_name=key, platform=Platform.INSTAGRAM,
                   token_env="STORYFAIL_TOKEN", target_id_env="STORYFAIL_TARGET")


def _feed_draft(tmp_path, **kw):
    feed_img = tmp_path / "nano_hook_line.png"
    feed_img.write_bytes(b"\x89PNG\r\n\x1a\nFEED")
    base = dict(
        draft_id="feed1", account_key="lasso_ig", platform="instagram",
        caption="c", hashtags=[], creative_path=str(feed_img),
        creative_public_url="https://cdn.test/feed.png",
        scheduled_for=f"{DAY}T18:30:00+00:00", status=DraftStatus.PENDING,
        source_fragments=["Hook line.", "Body line one.", "Body line two."],
    )
    base.update(kw)
    return Draft(**base)


class _AlertCapture:
    def __init__(self):
        self.messages = []

    def alert(self, msg, **_):
        self.messages.append(msg)
        return None


class _StudioStub:
    """Stands in for creative_studio.generate; optionally mutates the passed
    failure_info the way the studio does for a terminal quality failure."""

    def __init__(self, result=None, mutate_info=None):
        self.result = result
        self.mutate_info = mutate_info
        self.calls = []

    def __call__(self, headline, facts, **kwargs):
        self.calls.append({"headline": headline, "facts": facts, **kwargs})
        info = kwargs.get("failure_info")
        if info is not None and self.mutate_info:
            info.update(self.mutate_info)
        return self.result


def _arm(monkeypatch, tmp_path, studio_result=None, mutate_info=None):
    monkeypatch.setenv("AGENT_STORIES_ENABLED", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    studio = _StudioStub(result=studio_result, mutate_info=mutate_info)
    monkeypatch.setattr(stories.creative_studio, "generate", studio)
    monkeypatch.setattr(
        stories.media_host, "host_media",
        lambda path, tenant, client=None: "https://cdn.test/story.png",
    )
    alerts = _AlertCapture()
    monkeypatch.setattr(stories.ops_alerts, "alert", alerts.alert)
    return studio, alerts


# ---- 1. failure_info (fresh dict) + draft_id are threaded into generate -------

def test_failure_info_and_draft_id_passed_to_generate(monkeypatch, tmp_path):
    art_path = str(tmp_path / "nano_story_hook_line.png")
    studio, _ = _arm(monkeypatch, tmp_path,
                     studio_result={"path": art_path, "route": "astra:test"})
    feed = _feed_draft(tmp_path)
    story = stories.build_story_draft(_acct(), DAY, feed_draft=feed)

    assert story is not None
    assert len(studio.calls) == 1
    call = studio.calls[0]
    assert call["failure_info"] is not None and call["failure_info"] == {}
    assert isinstance(call["failure_info"], dict)
    expected_draft_id = stories._make_id("lasso_ig", "story", DAY)
    assert call["draft_id"] == expected_draft_id
    assert story.draft_id == expected_draft_id
    # per-call: two separate calls never share one failure_info dict
    stories.build_story_draft(_acct(), DAY, feed_draft=feed)
    assert studio.calls[0]["failure_info"] is not studio.calls[1]["failure_info"]


# ---- 2. reported quality failure -> NO redundant story alert ------------------

def test_reported_quality_failure_suppresses_redundant_alert(monkeypatch, tmp_path, capsys):
    studio, alerts = _arm(monkeypatch, tmp_path, studio_result=None,
                          mutate_info=QUALITY_INFO)
    result = stories.build_story_draft(_acct(), DAY,
                                       feed_draft=_feed_draft(tmp_path))
    assert result is None
    assert studio.calls[0]["failure_info"]["reported"] is True
    assert alerts.messages == [], \
        f"reported quality failure must not double-alert, got: {alerts.messages}"
    out = capsys.readouterr().out
    assert "[stories] skip" in out
    assert "quality gate" in out
    assert "lasso_ig" in out and DAY in out


# ---- 3. unknown / unreported failure -> the single dark-studio alert survives -

def test_unreported_failure_still_alerts(monkeypatch, tmp_path):
    # failure_info populated but reported is False: the studio did NOT emit an
    # alert for this call, so the story layer must still fire one.
    studio, alerts = _arm(monkeypatch, tmp_path, studio_result=None,
                          mutate_info={"reason": "engine timeout",
                                       "stage": "quality", "reported": False})
    result = stories.build_story_draft(_acct(), DAY,
                                       feed_draft=_feed_draft(tmp_path))
    assert result is None
    assert len(alerts.messages) == 1
    assert "story" in alerts.messages[0].lower()


def test_empty_failure_info_still_alerts(monkeypatch, tmp_path):
    # generate returned None with no failure_info at all (dark studio): the
    # existing single dark-studio ops alert fires, exactly as before.
    _, alerts = _arm(monkeypatch, tmp_path, studio_result=None)
    result = stories.build_story_draft(_acct(), DAY,
                                       feed_draft=_feed_draft(tmp_path))
    assert result is None
    assert len(alerts.messages) == 1
    assert "story" in alerts.messages[0].lower()
    assert "studio" in alerts.messages[0].lower()


def test_unknown_stage_never_suppressed(monkeypatch, tmp_path):
    # reported=True but a stage this module does not recognize: never swallow
    # an unknown failure.
    _, alerts = _arm(monkeypatch, tmp_path, studio_result=None,
                     mutate_info={"reason": "provider quota exhausted",
                                  "stage": "billing", "reported": True})
    result = stories.build_story_draft(_acct(), DAY,
                                       feed_draft=_feed_draft(tmp_path))
    assert result is None
    assert len(alerts.messages) == 1
    assert "story" in alerts.messages[0].lower()


# ---- 4. hosting failure is distinct from render unavailable -------------------

def test_hosting_failure_gets_distinct_alert(monkeypatch, tmp_path):
    art_path = str(tmp_path / "nano_story_hook_line.png")
    monkeypatch.setenv("AGENT_STORIES_ENABLED", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(
        stories.creative_studio, "generate",
        lambda *a, **kw: {"path": art_path, "route": "astra:test"})
    monkeypatch.setattr(stories.media_host, "host_media",
                        lambda path, tenant, client=None: None)  # hosting dark
    alerts = _AlertCapture()
    monkeypatch.setattr(stories.ops_alerts, "alert", alerts.alert)

    result = stories.build_story_draft(_acct(), DAY,
                                       feed_draft=_feed_draft(tmp_path))
    assert result is None
    assert len(alerts.messages) == 1
    msg = alerts.messages[0].lower()
    assert "hosting" in msg
    assert "no public url" in msg
    # must NOT blame the studio for a successful render
    assert "came back dark" not in msg


def test_render_unavailable_alert_does_not_blame_hosting(monkeypatch, tmp_path):
    _, alerts = _arm(monkeypatch, tmp_path, studio_result=None)
    result = stories.build_story_draft(_acct(), DAY,
                                       feed_draft=_feed_draft(tmp_path))
    assert result is None
    assert len(alerts.messages) == 1
    assert "hosting" not in alerts.messages[0].lower()


# ---- 5. no crop/reuse of feed assets; full-frame 9:16 output ------------------

def test_story_output_is_full_frame_9_16_not_feed_card(monkeypatch, tmp_path):
    art_path = str(tmp_path / "nano_story_hook_line.png")
    with open(art_path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\nSTORY")
    studio, _ = _arm(monkeypatch, tmp_path,
                     studio_result={"path": art_path, "route": "astra:test"})
    feed = _feed_draft(tmp_path)
    story = stories.build_story_draft(_acct(), DAY, feed_draft=feed)
    assert story is not None
    assert story.creative_path == art_path
    assert story.creative_path != feed.creative_path  # never the 4:5 feed card
    call = studio.calls[0]
    assert call["aspect"] == config.STORY_ASPECT
    assert call["pixels"] == config.STORY_PIXELS
    assert "story" in call["surface"].lower()


# ---- 6. dormant flag + no-9:16-by-design log-only behavior unchanged -----------

def test_flag_off_still_dormant(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_STORIES_ENABLED", raising=False)
    studio, alerts = _arm(monkeypatch, tmp_path,
                          studio_result={"path": "x.png"})
    # re-stub AFTER _arm armed the flag, so we can delete the flag again
    monkeypatch.delenv("AGENT_STORIES_ENABLED", raising=False)
    result = stories.build_story_draft(_acct(), DAY,
                                       feed_draft=_feed_draft(tmp_path))
    assert result is None
    assert studio.calls == []
    assert alerts.messages == []


def test_no_9_16_by_design_stays_log_only(monkeypatch, tmp_path, capsys):
    """A plain library feed creative (no 9:16 sibling by design) skips the Story
    with a LOG line only, never an ops alert. Unchanged by this package."""
    monkeypatch.setenv("AGENT_STORIES_ENABLED", "true")
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    alerts = _AlertCapture()
    monkeypatch.setattr(stories.ops_alerts, "alert", alerts.alert)
    feed = _feed_draft(tmp_path, creative_path="library_asset.png",
                       creative_public_url="", source_fragments=[])
    result = stories.build_story_draft(_acct(), DAY, feed_draft=feed)
    assert result is None
    assert alerts.messages == []
    out = capsys.readouterr().out
    assert "[stories] skip" in out
    assert "by design" in out


def test_reported_render_failure_does_not_emit_duplicate_dark_alert(monkeypatch, tmp_path):
    _, alerts = _arm(monkeypatch, tmp_path, mutate_info={
        "stage": "render_unavailable", "reported": True, "reason": "provider failed"})
    result = stories.build_story_draft(_acct(), DAY, feed_draft=_feed_draft(tmp_path))
    assert result is None
    assert alerts.messages == []
