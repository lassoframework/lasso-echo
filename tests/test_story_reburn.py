"""
Task #28 §5c — story caption re-burn (agent/story_reburn.py + the edit wiring). Fully
gated: no-op unless BOTH AGENT_STORY_SOURCE_MEDIA and AGENT_STORY_FORMAT are on and the row
is a story with a source_media_url. Best-effort: a re-burn failure NEVER fails the saved
edit. Offline — the burn + host + download are stubbed.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import story_reburn, portal_social as ps  # noqa: E402


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    monkeypatch.setenv("AGENT_SOCIAL_BILLING_DELEGATED", "true")
    yield


def _story_row(**o):
    d = {"id": "s1", "gym_id": "gritx", "format": "story",
         "source_media_url": "https://r2/w/raw.jpg", "caption": "old", "status": "pending"}
    d.update(o)
    return d


# ---- gating -----------------------------------------------------------------

def test_should_reburn_requires_both_flags_story_and_source(monkeypatch):
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "true")
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    assert story_reburn.should_reburn(_story_row()) is True
    assert story_reburn.should_reburn(_story_row(format="feed")) is False       # feed
    assert story_reburn.should_reburn(_story_row(source_media_url="")) is False  # no source


def test_should_reburn_off_when_flag_off(monkeypatch):
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "false")
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    assert story_reburn.should_reburn(_story_row()) is False


# ---- reburn() best-effort ---------------------------------------------------

def test_reburn_hosting_off_is_noop(monkeypatch):
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    # hosting off -> None, no crash
    assert story_reburn.reburn("https://r2/w/raw.jpg", "new cap", "GritX", "gritx") is None


def test_reburn_download_failure_returns_none(monkeypatch):
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(story_reburn, "_download", lambda url, log: None)
    assert story_reburn.reburn("https://r2/w/raw.jpg", "new cap", "GritX", "gritx") is None


def test_reburn_happy_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    src = tmp_path / "raw.jpg"
    src.write_bytes(b"x")
    monkeypatch.setattr(story_reburn, "_download", lambda url, log: str(src))
    import agent.story_image as si
    import agent.media_host as mh
    monkeypatch.setattr(si, "get_or_make_story_image",
                        lambda p, c, g, lib, **k: str(tmp_path / "burned.jpg"))
    monkeypatch.setattr(mh, "host_media", lambda path, tenant, client=None: "https://r2/w/burned_hosted.jpg")
    out = story_reburn.reburn("https://r2/w/raw.jpg", "new cap", "GritX", "gritx")
    assert out == "https://r2/w/burned_hosted.jpg"
    assert not src.exists()      # temp source cleaned up


# ---- edit wiring: maybe_reburn_story swaps image_url, best-effort ------------

class _FakeStore:
    def __init__(self):
        self.image_patches = []

    def patch_image_url(self, account_key, row_id, new_url):
        self.image_patches.append((row_id, new_url))
        return {"id": row_id, "image_url": new_url}


def test_maybe_reburn_swaps_image_url_when_eligible(monkeypatch):
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "true")
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    monkeypatch.setattr(story_reburn, "reburn",
                        lambda src, cap, gym, tenant, **k: "https://r2/w/fresh.jpg")
    store = _FakeStore()
    out = ps.maybe_reburn_story("gritx", _story_row(), "brand new caption", store)
    assert out == "https://r2/w/fresh.jpg"
    assert store.image_patches == [("s1", "https://r2/w/fresh.jpg")]


def test_maybe_reburn_noop_when_gated_off(monkeypatch):
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "false")
    store = _FakeStore()
    out = ps.maybe_reburn_story("gritx", _story_row(), "brand new caption", store)
    assert out is None and store.image_patches == []


def test_maybe_reburn_failure_never_raises(monkeypatch):
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "true")
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")

    def _boom(*a, **k):
        raise RuntimeError("burn exploded")
    monkeypatch.setattr(story_reburn, "reburn", _boom)
    store = _FakeStore()
    # must swallow and return None (the saved caption edit stands)
    assert ps.maybe_reburn_story("gritx", _story_row(), "cap", store) is None
    assert store.image_patches == []


# ---- pre-migration safety: source_media_url only in the row when set ---------

def test_real_row_includes_source_media_only_when_present():
    from types import SimpleNamespace
    from agent import real_calendar_mirror as rcm
    base = dict(platform="instagram", day_key="2026-09-01", caption="c",
                creative_public_url="https://r2/x.jpg", is_story=True,
                status="pending", category="proof")
    # flag ON case: the draft carries source_media_url -> row includes it
    row_with = rcm._real_row("gritx", SimpleNamespace(source_media_url="https://r2/raw.jpg", **base))
    assert row_with["source_media_url"] == "https://r2/raw.jpg"
    # flag OFF case: the draft has no source_media_url -> the column is OMITTED from the
    # payload entirely, so a pre-migration insert never carries an unknown column.
    row_without = rcm._real_row("gritx", SimpleNamespace(source_media_url="", **base))
    assert "source_media_url" not in row_without


# ---- LASSO's OWN story lanes stage source_media_url too -----------------------
# The client lane (client_month_run._maybe_format_story) stamps the story's raw hosted
# media, but LASSO builds its stories in its own lanes (the nano 9:16 story render and
# the summit sprint's paired *_story render). Those omitted the stamp, so every LASSO
# story row had source_media_url NULL and an edited LASSO story caption could never
# re-burn (the old text shipped). These pin the stamp on both LASSO lanes.

def _lasso_ig_account():
    from types import SimpleNamespace
    return SimpleNamespace(key="lasso_ig", platform="instagram", display_name="LASSO IG")


def _pending_feed_draft():
    from agent.drafter import Draft, DraftStatus
    return Draft(draft_id="f1", account_key="lasso_ig", platform="instagram",
                 caption="feed cap", hashtags=[],
                 creative_path="/tmp/nano_hook.png",
                 creative_public_url="https://r2/lasso_ig/nano_hook.png",
                 scheduled_for="2026-09-01T18:00:00Z", status=DraftStatus.PENDING,
                 source_fragments=["headline", "fact"])


def _build_lasso_story(monkeypatch, story_url):
    """Run stories.build_story_draft down the premade 9:16 lane, offline."""
    from agent import stories
    monkeypatch.setenv("AGENT_STORIES_ENABLED", "true")
    monkeypatch.setenv("AGENT_STORY_PREMADE_ENABLED", "true")
    monkeypatch.setattr(stories.schedule, "should_post_on", lambda day: True)
    monkeypatch.setattr(stories.schedule, "scheduled_for",
                        lambda day, slot=None: f"{day}T09:00:00Z")
    monkeypatch.setattr(stories, "_premade_story_variant",
                        lambda feed: "/tmp/nano_hook_story.png")
    monkeypatch.setattr(stories.media_host, "host_media",
                        lambda path, key, client=None: story_url)
    return stories.build_story_draft(_lasso_ig_account(), "2026-09-01",
                                     feed_draft=_pending_feed_draft())


def test_lasso_nano_story_draft_carries_source_media_url(monkeypatch):
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "true")
    url = "https://r2/lasso_ig/nano_story_hook.png"
    draft = _build_lasso_story(monkeypatch, url)
    assert draft is not None and draft.is_story is True
    # the story's own hosted media is recorded as the raw source to re-burn from
    assert getattr(draft, "source_media_url", "") == url
    # ...and it survives into the content_calendar row the portal reads
    from agent import real_calendar_mirror as rcm
    row = rcm._real_row("lasso", draft)
    assert row["format"] == "story"
    assert row["source_media_url"] == url


def test_lasso_nano_story_omits_source_media_when_flag_off(monkeypatch):
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "false")
    draft = _build_lasso_story(monkeypatch, "https://r2/lasso_ig/nano_story_hook.png")
    assert draft is not None
    assert not getattr(draft, "source_media_url", "")
    from agent import real_calendar_mirror as rcm
    assert "source_media_url" not in rcm._real_row("lasso", draft)


def _summit_sprint_story(monkeypatch, story_url):
    from agent import real_month_run as rmr
    slot = {"filename": "06_room_a.png", "caption": "room a",
            "scheduled_for": "2026-09-01T18:00:00Z"}
    monkeypatch.setattr(rmr, "_sprint_slot_map",
                        lambda posts_per_day=None: {("2026-09-01", 0): slot})
    manifest = {"06_room_a.png": "https://r2/lasso_summit/06_room_a.png",
                "06_room_a_story.png": story_url}
    _feed, _story = rmr.sprint_builders(_lasso_ig_account(), manifest=manifest)
    return _story(None, "2026-09-01", 0, _pending_feed_draft())


def test_lasso_summit_sprint_story_carries_source_media_url(monkeypatch):
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "true")
    url = "https://r2/lasso_summit/06_room_a_story.png"
    draft = _summit_sprint_story(monkeypatch, url)
    assert draft is not None and draft.is_story is True
    assert getattr(draft, "source_media_url", "") == url
    from agent import real_calendar_mirror as rcm
    assert rcm._real_row("lasso", draft)["source_media_url"] == url


def test_lasso_summit_sprint_story_omits_source_media_when_flag_off(monkeypatch):
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "false")
    draft = _summit_sprint_story(monkeypatch,
                                 "https://r2/lasso_summit/06_room_a_story.png")
    assert draft is not None
    assert not getattr(draft, "source_media_url", "")


def test_stamp_source_media_never_touches_a_feed_draft(monkeypatch):
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "true")
    feed = _pending_feed_draft()
    story_reburn.stamp_source_media(feed)
    assert not getattr(feed, "source_media_url", "")
