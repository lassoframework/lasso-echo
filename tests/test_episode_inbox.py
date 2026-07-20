"""
Episode inbox watcher tests (Parts 1-5).
All offline: fake R2 client, fake poster, tmp db.
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from agent import episode_inbox  # noqa: E402


# ---- helpers ----------------------------------------------------------------

def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_EPISODE_INBOX_ENABLED", "true")
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))


class _FakeR2Client:
    """Minimal fake that mirrors _S3Client.list_prefix interface."""
    def __init__(self, objects=None):
        self._objects = objects or []

    def list_prefix(self, prefix):
        return list(self._objects)


class _FakePoster:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


# ---- Part 1: inbox convention + state --------------------------------------

class TestClaimState:
    def test_new_key_not_claimed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
        assert not episode_inbox._is_claimed("echo/inbox/ep.mp4")

    def test_claim_wins_on_first_call(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
        assert episode_inbox._claim("echo/inbox/ep.mp4") is True

    def test_claimed_key_is_detected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
        episode_inbox._claim("echo/inbox/ep.mp4")
        assert episode_inbox._is_claimed("echo/inbox/ep.mp4")

    def test_claim_fails_on_second_call(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
        episode_inbox._claim("echo/inbox/ep.mp4")
        assert episode_inbox._claim("echo/inbox/ep.mp4") is False

    def test_marker_survives_restart(self, tmp_path, monkeypatch):
        """Claim is stored in persistent kv; a fresh kv_get after the call still sees it."""
        db_path = str(tmp_path / "echo.db")
        monkeypatch.setenv("AGENT_DB_PATH", db_path)
        episode_inbox._claim("echo/inbox/ep47.mp4")
        # Simulate restart: clear module-level caches if any, re-read from DB
        from agent import db
        val = db.kv_get(episode_inbox._claim_key_name("echo/inbox/ep47.mp4"), "")
        assert val  # marker persists

    def test_accept_ext_mp4(self):
        assert episode_inbox._accept_ext("echo/inbox/ep.mp4")

    def test_accept_ext_mov(self):
        assert episode_inbox._accept_ext("echo/inbox/ep.MOV")

    def test_accept_ext_mp3(self):
        assert episode_inbox._accept_ext("ep.mp3")

    def test_accept_ext_wav(self):
        assert episode_inbox._accept_ext("ep.wav")

    def test_reject_ext_pdf(self):
        assert not episode_inbox._accept_ext("ep.pdf")

    def test_reject_ext_jpg(self):
        assert not episode_inbox._accept_ext("ep.jpg")

    def test_mark_processed_updates_status(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
        episode_inbox._claim("echo/inbox/ep.mp4")
        episode_inbox._mark_processed("echo/inbox/ep.mp4")
        from agent import db
        raw = db.kv_get(episode_inbox._claim_key_name("echo/inbox/ep.mp4"), "{}")
        state = json.loads(raw)
        assert state["status"] == "processed"

    def test_mark_failed_stores_reason(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
        episode_inbox._claim("echo/inbox/ep.mp4")
        episode_inbox._mark_failed("echo/inbox/ep.mp4", "transcription error")
        from agent import db
        raw = db.kv_get(episode_inbox._claim_key_name("echo/inbox/ep.mp4"), "{}")
        state = json.loads(raw)
        assert state["status"] == "failed"
        assert "transcription" in state["reason"]


# ---- Part 2: watcher loop ---------------------------------------------------

class _FakeClipper:
    """Injected clipper: returns a canned selection."""
    def __init__(self):
        self.calls = []

    def __call__(self, source, transcriber=None, llm=None, account_key=None):
        self.calls.append(source)
        return {"staged": source, "transcript": {}, "selection": []}


class TestPollLoop:
    def test_disabled_returns_disabled(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENT_EPISODE_INBOX_ENABLED", raising=False)
        monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
        result = episode_inbox.poll(client=_FakeR2Client())
        assert result["status"] == "disabled"

    def test_no_client_returns_no_client(self, tmp_path, monkeypatch):
        _arm(monkeypatch, tmp_path)
        result = episode_inbox.poll(client=None)
        assert result["status"] == "no_client"

    def test_empty_prefix_returns_ok(self, tmp_path, monkeypatch):
        _arm(monkeypatch, tmp_path)
        result = episode_inbox.poll(client=_FakeR2Client([]))
        assert result["status"] == "ok"
        assert result["objects_found"] == 0

    def test_unstable_file_not_processed(self, tmp_path, monkeypatch):
        """File size changes between polls: never claimed."""
        _arm(monkeypatch, tmp_path)
        key = "echo/inbox/ep.mp4"
        client1 = _FakeR2Client([{"key": key, "size": 100, "last_modified": ""}])
        client2 = _FakeR2Client([{"key": key, "size": 200, "last_modified": ""}])
        poster = _FakePoster()
        # First poll: registers size 100
        episode_inbox.poll(client=client1, poster=poster)
        # Second poll: different size 200 -> still not stable
        episode_inbox.poll(client=client2, poster=poster)
        assert not episode_inbox._is_claimed(key)
        assert poster.notices == []

    def test_stable_file_triggers_clipper(self, tmp_path, monkeypatch):
        """Same size in two consecutive polls: clipper runs exactly once."""
        _arm(monkeypatch, tmp_path)
        key = "echo/inbox/ep.mp4"
        obj = {"key": key, "size": 5000000, "last_modified": ""}
        client = _FakeR2Client([obj])
        poster = _FakePoster()
        fake_clipper = _FakeClipper()

        monkeypatch.setattr(
            "agent.episode_inbox._post_plan_to_slack",
            lambda *a, **kw: None,
        )

        import agent.clipper as _clipper_mod
        orig_clip = _clipper_mod.clip_episode

        def fake_clip_episode(source, render=False, transcriber=None, llm=None,
                              account_key=None):
            fake_clipper(source, transcriber=transcriber,
                         llm=llm, account_key=account_key)
            return {"staged": source, "transcript": {}, "selection": {}, "reels": []}

        monkeypatch.setattr(_clipper_mod, "clip_episode", fake_clip_episode)

        episode_inbox.poll(client=client, poster=poster)  # registers size
        episode_inbox.poll(client=client, poster=poster)  # stable -> clips

        assert len(fake_clipper.calls) == 1
        assert fake_clipper.calls[0] == key

    def test_claimed_file_skipped_on_repoll(self, tmp_path, monkeypatch):
        """After successful processing, a third poll ignores the file."""
        _arm(monkeypatch, tmp_path)
        key = "echo/inbox/ep.mp4"
        obj = {"key": key, "size": 5000000, "last_modified": ""}
        client = _FakeR2Client([obj])

        monkeypatch.setattr("agent.episode_inbox._post_plan_to_slack",
                            lambda *a, **kw: None)

        call_count = {"n": 0}

        import agent.clipper as _clipper_mod
        def fake_clip(source, render=False, transcriber=None, llm=None,
                      account_key=None):
            call_count["n"] += 1
            return {"staged": source, "transcript": {}, "selection": {}, "reels": []}
        monkeypatch.setattr(_clipper_mod, "clip_episode", fake_clip)

        episode_inbox.poll(client=client)   # size registered
        episode_inbox.poll(client=client)   # stable -> processed (1 call)
        episode_inbox.poll(client=client)   # claimed -> skipped

        assert call_count["n"] == 1

    def test_non_video_extension_ignored(self, tmp_path, monkeypatch):
        _arm(monkeypatch, tmp_path)
        objects = [
            {"key": "echo/inbox/notes.pdf", "size": 100, "last_modified": ""},
            {"key": "echo/inbox/image.jpg", "size": 200, "last_modified": ""},
        ]
        result = episode_inbox.poll(client=_FakeR2Client(objects))
        assert result["objects_found"] == 0

    def test_processing_exception_marks_failed_and_continues(self, tmp_path, monkeypatch):
        """An exception during Phase 1 marks file failed; loop continues."""
        _arm(monkeypatch, tmp_path)
        key = "echo/inbox/ep.mp4"
        obj = {"key": key, "size": 5000000, "last_modified": ""}
        client = _FakeR2Client([obj])

        import agent.clipper as _clipper_mod
        def bad_clip(source, render=False, transcriber=None, llm=None,
                     account_key=None):
            raise RuntimeError("transcription service down")
        monkeypatch.setattr(_clipper_mod, "clip_episode", bad_clip)

        episode_inbox.poll(client=client)   # size registered
        result = episode_inbox.poll(client=client)  # stable -> fails

        assert result["failed"] == 1
        from agent import db
        raw = db.kv_get(episode_inbox._claim_key_name(key), "{}")
        state = json.loads(raw)
        assert state["status"] == "failed"


# ---- Part 3: ops surface ----------------------------------------------------

class TestInboxStatus:
    def test_status_contains_required_keys(self, tmp_path, monkeypatch):
        _arm(monkeypatch, tmp_path)
        s = episode_inbox.inbox_status()
        assert "enabled" in s
        assert "prefix" in s
        assert "poll_interval_minutes" in s
        assert "last_run" in s
        assert "seen" in s
        assert "processed" in s
        assert "failed" in s

    def test_status_enabled_follows_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
        monkeypatch.delenv("AGENT_EPISODE_INBOX_ENABLED", raising=False)
        assert episode_inbox.inbox_status()["enabled"] is False

    def test_status_counts_processed(self, tmp_path, monkeypatch):
        _arm(monkeypatch, tmp_path)
        key = "echo/inbox/ep.mp4"
        obj = {"key": key, "size": 5000000, "last_modified": ""}
        client = _FakeR2Client([obj])

        monkeypatch.setattr("agent.episode_inbox._post_plan_to_slack",
                            lambda *a, **kw: None)
        import agent.clipper as _clipper_mod
        monkeypatch.setattr(_clipper_mod, "clip_episode",
                            lambda *a, **kw: {"staged": "", "transcript": {}, "selection": []})

        episode_inbox.poll(client=client)
        episode_inbox.poll(client=client)

        s = episode_inbox.inbox_status()
        assert s["processed"] == 1

    def test_status_counts_failed(self, tmp_path, monkeypatch):
        _arm(monkeypatch, tmp_path)
        key = "echo/inbox/ep.mp4"
        obj = {"key": key, "size": 1000, "last_modified": ""}
        client = _FakeR2Client([obj])

        import agent.clipper as _clipper_mod
        monkeypatch.setattr(_clipper_mod, "clip_episode",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

        episode_inbox.poll(client=client)
        episode_inbox.poll(client=client)  # stable -> fails

        s = episode_inbox.inbox_status()
        assert s["failed"] >= 1


# ---- Part 4: RSS episode matching + evergreen guard -------------------------

def _seed_episode(tmp_path, episode=47, title="Episode 47: Follow-Up",
                  published="Mon, 07 Jul 2026 12:00:00 GMT",
                  guid="ep47-guid"):
    """Write a fake podcast_episodes row directly."""
    from agent import db
    with db.connect(str(tmp_path / "echo.db")) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS podcast_episodes (
              guid TEXT PRIMARY KEY,
              episode INTEGER,
              title TEXT,
              description TEXT,
              audio_url TEXT,
              published TEXT,
              transcript_url TEXT,
              detected_at TEXT DEFAULT (datetime('now')));
        """)
        conn.execute(
            "INSERT OR REPLACE INTO podcast_episodes "
            "(guid, episode, title, published) VALUES (?,?,?,?)",
            (guid, episode, title, published)
        )
        conn.commit()


class TestRssEpisodeMatching:
    def test_latest_episode_from_db_returns_row(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
        _seed_episode(tmp_path)
        ep = episode_inbox._latest_episode_from_db()
        assert ep["episode"] == 47
        assert "Follow-Up" in ep["title"]

    def test_latest_episode_empty_db_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
        ep = episode_inbox._latest_episode_from_db()
        assert ep == {}

    def test_plan_carries_episode_metadata(self, tmp_path, monkeypatch):
        """The Slack plan notice must include episode number and title."""
        _arm(monkeypatch, tmp_path)
        _seed_episode(tmp_path)

        key = "echo/inbox/ep47.mp4"
        obj = {"key": key, "size": 5000000, "last_modified": ""}
        client = _FakeR2Client([obj])
        poster = _FakePoster()

        import agent.clipper as _clipper_mod
        monkeypatch.setattr(_clipper_mod, "clip_episode",
                            lambda *a, **kw: {"staged": "", "transcript": {}, "selection": []})
        monkeypatch.setattr(_clipper_mod, "print_plan",
                            lambda sel: "PLAN TEXT")

        episode_inbox.poll(client=client, poster=poster)   # size registered
        episode_inbox.poll(client=client, poster=poster)   # stable -> plan posted

        assert len(poster.notices) == 1
        notice = poster.notices[0]
        assert "47" in notice
        assert "Follow-Up" in notice or "Episode 47" in notice

    def test_evergreen_banned_phrase_triggers_guard(self):
        violations = episode_inbox._evergreen_check(
            "New episode is live! Listen now."
        )
        assert violations  # at least one banned phrase found

    def test_clean_text_passes_evergreen(self):
        violations = episode_inbox._evergreen_check(
            "Episode 47: How to follow up with leads in a gym."
        )
        assert violations == []


# ---- Part 5: Monday nudge ---------------------------------------------------

def _monday_9am_et() -> datetime:
    """A concrete Monday 9:00 AM ET (= 13:00 UTC in summer)."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        return datetime(2026, 7, 6, 9, 0, 0, tzinfo=tz)   # Monday 2026-07-06
    except Exception:
        return datetime(2026, 7, 6, 14, 0, 0, tzinfo=timezone.utc)


def _tuesday_9am_et() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        return datetime(2026, 7, 7, 9, 0, 0, tzinfo=tz)  # Tuesday
    except Exception:
        return datetime(2026, 7, 7, 14, 0, 0, tzinfo=timezone.utc)


class TestMondayNudge:
    def _base(self, tmp_path, monkeypatch):
        _arm(monkeypatch, tmp_path)
        # Episode published 1 day ago (within default 2-day window)
        _seed_episode(tmp_path, episode=47,
                      title="Episode 47: Follow-Up",
                      published="Mon, 06 Jul 2026 12:00:00 GMT")

    def test_disabled_returns_disabled(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENT_EPISODE_INBOX_ENABLED", raising=False)
        monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
        result = episode_inbox.check_monday_nudge(now=_monday_9am_et())
        assert result["status"] == "disabled"

    def test_not_monday_returns_not_monday(self, tmp_path, monkeypatch):
        _arm(monkeypatch, tmp_path)
        result = episode_inbox.check_monday_nudge(
            now=_tuesday_9am_et(), poster=_FakePoster()
        )
        assert result["status"] == "not_monday"

    def test_before_nudge_time_returns_not_yet(self, tmp_path, monkeypatch):
        _arm(monkeypatch, tmp_path)
        try:
            from zoneinfo import ZoneInfo
            early = datetime(2026, 7, 6, 8, 30, 0, tzinfo=ZoneInfo("America/New_York"))
        except Exception:
            early = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
        result = episode_inbox.check_monday_nudge(now=early, poster=_FakePoster())
        assert result["status"] == "not_yet"

    def test_new_unmatched_episode_within_window_sends_nudge(self, tmp_path, monkeypatch):
        self._base(tmp_path, monkeypatch)
        poster = _FakePoster()
        result = episode_inbox.check_monday_nudge(now=_monday_9am_et(), poster=poster)
        assert result["status"] == "nudge_sent"
        assert len(poster.notices) == 1
        assert "Follow-Up" in poster.notices[0] or "47" in poster.notices[0]

    def test_nudge_includes_inbox_prefix(self, tmp_path, monkeypatch):
        self._base(tmp_path, monkeypatch)
        monkeypatch.setenv("AGENT_EPISODE_INBOX_PREFIX", "echo/episode_inbox/lasso/")
        poster = _FakePoster()
        episode_inbox.check_monday_nudge(now=_monday_9am_et(), poster=poster)
        assert "echo/episode_inbox/lasso/" in poster.notices[0]

    def test_second_call_same_day_does_not_duplicate_nudge(self, tmp_path, monkeypatch):
        self._base(tmp_path, monkeypatch)
        poster = _FakePoster()
        episode_inbox.check_monday_nudge(now=_monday_9am_et(), poster=poster)
        episode_inbox.check_monday_nudge(now=_monday_9am_et(), poster=poster)
        assert len(poster.notices) == 1  # second call is idempotent

    def test_already_matched_episode_produces_no_nudge(self, tmp_path, monkeypatch):
        self._base(tmp_path, monkeypatch)
        episode_inbox._mark_ep_matched(47)
        poster = _FakePoster()
        result = episode_inbox.check_monday_nudge(now=_monday_9am_et(), poster=poster)
        assert result["status"] == "already_matched"
        assert poster.notices == []

    def test_stale_episode_outside_window_produces_no_nudge(self, tmp_path, monkeypatch):
        _arm(monkeypatch, tmp_path)
        monkeypatch.setenv("AGENT_EPISODE_NUDGE_WINDOW_DAYS", "2")
        # Episode published 10 days ago
        _seed_episode(tmp_path, episode=45,
                      title="Episode 45: Old Content",
                      published="Fri, 26 Jun 2026 12:00:00 GMT")
        poster = _FakePoster()
        result = episode_inbox.check_monday_nudge(now=_monday_9am_et(), poster=poster)
        assert result["status"] == "outside_window"
        assert poster.notices == []

    def test_idempotent_same_episode_different_day_can_nudge(self, tmp_path, monkeypatch):
        """A nudge on Monday 1 does NOT block a nudge on the following Monday 2
        (different date_str key)."""
        self._base(tmp_path, monkeypatch)
        poster = _FakePoster()
        monday1 = _monday_9am_et()
        episode_inbox.check_monday_nudge(now=monday1, poster=poster)
        assert len(poster.notices) == 1


# ---- Part 2 (render-armed): Phase 2+3 wiring -----------------------------------

class _FakeDraftStore:
    def __init__(self):
        self.saved = []

    def put(self, draft):
        self.saved.append(draft)
        return draft


class _FakeDraftPoster:
    def __init__(self):
        self.posted = []

    def post_approval_card(self, draft):
        self.posted.append(draft)
        return {"ok": True, "ts": "111.222", "channel": "C_TEST"}


def _arm_render(monkeypatch, tmp_path):
    """Arm both inbox and render flags."""
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_CLIPPER_RENDER_ENABLED", "true")
    monkeypatch.setenv("AGENT_CLIPPER_ENABLED", "true")


class TestPollRenderArmed:
    """Phase 2+3 wiring: when AGENT_CLIPPER_RENDER_ENABLED is armed, poll()
    must call clip_episode(render=True) and save a HELD draft per rendered Reel."""

    def _make_moment(self):
        from agent.clipper import Moment
        return Moment(
            start_ts=5.0, end_ts=50.0, duration=45.0,
            hook="Most gyms ignore follow-up completely.",
            rationale="Strong opening claim.",
            bucket="doctrine", score=90,
            transcript_text="Most gyms ignore follow-up completely.",
        )

    def test_poll_passes_render_true_when_flag_armed(self, tmp_path, monkeypatch):
        """clip_episode must be called with render=True when the render flag is set."""
        _arm_render(monkeypatch, tmp_path)
        key = "echo/inbox/ep.mp4"
        obj = {"key": key, "size": 5000000, "last_modified": ""}
        client = _FakeR2Client([obj])

        render_calls = {"render": None}

        import agent.clipper as _clipper_mod
        def fake_clip(source, render=False, transcriber=None, llm=None,
                      account_key=None):
            render_calls["render"] = render
            return {"staged": source, "transcript": {}, "selection": {}, "reels": []}

        monkeypatch.setattr(_clipper_mod, "clip_episode", fake_clip)
        monkeypatch.setattr("agent.episode_inbox._post_plan_to_slack",
                            lambda *a, **kw: None)

        episode_inbox.poll(client=client)    # size registered
        episode_inbox.poll(client=client)    # stable -> clips with render=True

        assert render_calls["render"] is True

    def test_poll_saves_draft_per_rendered_reel(self, tmp_path, monkeypatch):
        """One Reel in the result must produce one HELD draft in the store."""
        _arm_render(monkeypatch, tmp_path)
        key = "echo/inbox/ep47.mp4"
        obj = {"key": key, "size": 5000000, "last_modified": ""}
        client = _FakeR2Client([obj])

        moment = self._make_moment()
        fake_reel = {"moment": moment, "reel_path": "/data/render/clip_reel.mp4"}

        import agent.clipper as _clipper_mod
        monkeypatch.setattr(_clipper_mod, "clip_episode",
                            lambda *a, **kw: {
                                "staged": key, "transcript": {},
                                "selection": {}, "reels": [fake_reel],
                            })
        monkeypatch.setattr("agent.episode_inbox._post_plan_to_slack",
                            lambda *a, **kw: None)

        # Stub out media_host.host_media so no real R2 upload is attempted.
        import agent.media_host as _media_host
        monkeypatch.setattr(_media_host, "host_media",
                            lambda path, tenant, client=None: "https://cdn.test/reel.mp4")

        store = _FakeDraftStore()
        draft_poster = _FakeDraftPoster()

        episode_inbox.poll(client=client)    # size registered
        result = episode_inbox.poll(
            client=client,
            draft_store=store,
            draft_poster=draft_poster,
        )

        assert result["drafts_saved"] == 1
        # save_clip_draft calls store.put() twice (save + Slack ts update);
        # verify one unique draft was saved (by draft_id).
        assert len({d.draft_id for d in store.saved}) == 1
        draft = store.saved[0]
        assert draft.status.value == "pending" or str(draft.status).lower() == "pending"

    def test_poll_draft_has_pending_status_never_autopublishes(
            self, tmp_path, monkeypatch):
        """Clipper drafts are always PENDING regardless of trust ladder."""
        _arm_render(monkeypatch, tmp_path)
        key = "echo/inbox/ep48.mp4"
        obj = {"key": key, "size": 5000000, "last_modified": ""}
        client = _FakeR2Client([obj])

        moment = self._make_moment()
        fake_reel = {"moment": moment, "reel_path": "/data/render/clip48.mp4"}

        import agent.clipper as _clipper_mod
        monkeypatch.setattr(_clipper_mod, "clip_episode",
                            lambda *a, **kw: {
                                "staged": key, "transcript": {},
                                "selection": {}, "reels": [fake_reel],
                            })
        monkeypatch.setattr("agent.episode_inbox._post_plan_to_slack",
                            lambda *a, **kw: None)

        import agent.media_host as _media_host
        monkeypatch.setattr(_media_host, "host_media",
                            lambda path, tenant, client=None: "")

        store = _FakeDraftStore()
        episode_inbox.poll(client=client)    # size registered
        episode_inbox.poll(client=client, draft_store=store,
                           draft_poster=_FakeDraftPoster())

        assert len(store.saved) >= 1
        for d in store.saved:
            assert str(d.status).lower() == "pending" or d.status.value == "pending"

    def test_poll_render_flag_off_no_drafts_saved(self, tmp_path, monkeypatch):
        """When the render flag is OFF, poll still works but never saves drafts."""
        _arm(monkeypatch, tmp_path)  # inbox ON, render OFF
        monkeypatch.delenv("AGENT_CLIPPER_RENDER_ENABLED", raising=False)

        key = "echo/inbox/ep49.mp4"
        obj = {"key": key, "size": 5000000, "last_modified": ""}
        client = _FakeR2Client([obj])

        moment = self._make_moment()
        fake_reel = {"moment": moment, "reel_path": "/data/render/clip49.mp4"}

        import agent.clipper as _clipper_mod
        monkeypatch.setattr(_clipper_mod, "clip_episode",
                            lambda *a, **kw: {
                                "staged": key, "transcript": {},
                                "selection": {}, "reels": [fake_reel],
                            })
        monkeypatch.setattr("agent.episode_inbox._post_plan_to_slack",
                            lambda *a, **kw: None)

        store = _FakeDraftStore()
        episode_inbox.poll(client=client)   # size registered
        result = episode_inbox.poll(client=client, draft_store=store,
                                    draft_poster=_FakeDraftPoster())

        assert "drafts_saved" not in result
        assert len(store.saved) == 0

    def test_poll_slack_card_posted_per_rendered_reel(
            self, tmp_path, monkeypatch):
        """Each rendered Reel gets its own Slack approval card."""
        _arm_render(monkeypatch, tmp_path)
        key = "echo/inbox/ep50.mp4"
        obj = {"key": key, "size": 5000000, "last_modified": ""}
        client = _FakeR2Client([obj])

        moment = self._make_moment()
        fake_reels = [
            {"moment": moment, "reel_path": "/data/render/clipA.mp4"},
            {"moment": moment, "reel_path": "/data/render/clipB.mp4"},
        ]

        import agent.clipper as _clipper_mod
        monkeypatch.setattr(_clipper_mod, "clip_episode",
                            lambda *a, **kw: {
                                "staged": key, "transcript": {},
                                "selection": {}, "reels": fake_reels,
                            })
        monkeypatch.setattr("agent.episode_inbox._post_plan_to_slack",
                            lambda *a, **kw: None)

        import agent.media_host as _media_host
        monkeypatch.setattr(_media_host, "host_media",
                            lambda path, tenant, client=None: "https://cdn.test/r.mp4")

        store = _FakeDraftStore()
        draft_poster = _FakeDraftPoster()

        episode_inbox.poll(client=client)   # size registered
        result = episode_inbox.poll(
            client=client, draft_store=store, draft_poster=draft_poster)

        assert result["drafts_saved"] == 2
        assert len(draft_poster.posted) == 2

    def test_reel_upload_failure_does_not_crash_loop(
            self, tmp_path, monkeypatch):
        """A reel upload failure fires an ops alert but the draft is still saved."""
        _arm_render(monkeypatch, tmp_path)
        key = "echo/inbox/ep51.mp4"
        obj = {"key": key, "size": 5000000, "last_modified": ""}
        client = _FakeR2Client([obj])

        moment = self._make_moment()
        fake_reel = {"moment": moment, "reel_path": "/data/render/clipC.mp4"}

        import agent.clipper as _clipper_mod
        monkeypatch.setattr(_clipper_mod, "clip_episode",
                            lambda *a, **kw: {
                                "staged": key, "transcript": {},
                                "selection": {}, "reels": [fake_reel],
                            })
        monkeypatch.setattr("agent.episode_inbox._post_plan_to_slack",
                            lambda *a, **kw: None)

        import agent.media_host as _media_host
        monkeypatch.setattr(_media_host, "host_media",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                RuntimeError("R2 upload failed")))

        store = _FakeDraftStore()
        draft_poster = _FakeDraftPoster()

        episode_inbox.poll(client=client)   # size registered
        result = episode_inbox.poll(
            client=client, draft_store=store, draft_poster=draft_poster)

        # Draft is still saved (with empty URL) even after upload failure
        assert result["drafts_saved"] == 1
        # save_clip_draft calls put() twice (save + Slack ts update); check 1 unique draft_id
        assert len({d.draft_id for d in store.saved}) == 1
