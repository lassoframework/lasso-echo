"""
Story Studio logical_post_id stamp. The calendar row a Story Studio request stages
carries the request's own identity in logical_post_id: a rebuild that mints a NEW
request_id stages a NEW identity, never reusing/grouping an existing row by date
or image.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import story_studio as ss  # noqa: E402
from agent import story_music as sm  # noqa: E402


class _FakeStore:
    def __init__(self):
        self.requests = []
        self.renders = []

    def available(self):
        return True

    def insert_request(self, row):
        self.requests.append(dict(row))
        return dict(row)

    def insert_render(self, row):
        self.renders.append(dict(row))
        return dict(row)

    def update_request(self, rid, fields, gym_id=None):
        for r in self.requests:
            if r.get("id") == rid:
                r.update(fields)
        return True

    def update_render(self, rid, fields, gym_id=None):
        for r in self.renders:
            if r.get("id") == rid:
                r.update(fields)
        return True


class _RealPathLibrary(sm.StubMusicLibrary):
    def __init__(self, audio_path):
        super().__init__(tracks=[sm.Track(
            track_id="hype_test", license_ref="lasso-lib:LIC-TEST",
            shelf=sm.SHELF_HYPE, title="Test Hype", path=audio_path)])
        self._audio = audio_path

    def resolve_path(self, track):
        return self._audio


def _cands(gym, n=6, seg=10.0):
    return [{"asset_id": f"a{i}", "gym_id": gym, "start_ts": 0,
             "end_ts": seg, "score": 90 - i} for i in range(n)]


def _fake_render(plan, *, output_dir, **_k):
    from agent import story_composer as comp
    return comp.ComposeResult(plan=plan, output_path=f"{output_dir}/final.mp4")


_STAGED_ROWS = []


class _FakeCalStore:
    def insert_rows(self, gym_id, rows):
        out = []
        for i, r in enumerate(rows or []):
            row = dict(r)
            row["id"] = f"cal-{len(_STAGED_ROWS) + i}"
            _STAGED_ROWS.append((gym_id, row))
            out.append(row)
        return out


@pytest.fixture(autouse=True)
def _cal(monkeypatch):
    _STAGED_ROWS.clear()
    monkeypatch.setattr("agent.config.portal_calendar_supabase_enabled", lambda: True)
    monkeypatch.setattr("agent.portal_calendar_store.SupabaseCalendarStore",
                        lambda *a, **k: _FakeCalStore())
    yield


def _arm(monkeypatch, gym="pierce"):
    monkeypatch.setenv("STORY_STUDIO_RENDER_GYMS", gym)
    monkeypatch.setattr("agent.config.supabase_url", lambda: "")
    monkeypatch.setattr("agent.config.supabase_service_key", lambda: "")
    monkeypatch.setattr("agent.story_studio._host", lambda p, g: "https://r2/story.mp4")


def _create(monkeypatch, tmp_path, payload):
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"ID3fake")
    return ss.create_story(
        payload,
        candidates=_cands(payload["gym_id"]), assets_by_id={},
        analysis={"confidence": 0.9, "tags": ["workout"]},
        store=_FakeStore(), music_library=_RealPathLibrary(str(audio)),
        render_fn=_fake_render, output_dir=str(tmp_path))


def test_staged_row_is_stamped_with_the_request_id(monkeypatch, tmp_path):
    res = _create(monkeypatch, tmp_path,
                  {"gym_id": "pierce", "asset_ids": ["a0", "a1"],
                   "brief": "Members crushed today",
                   "identity_tokens": ["Pierce"], "requested_by": "coach1"})
    assert res["status"] == "staged"
    assert len(_STAGED_ROWS) == 1
    row = _STAGED_ROWS[0][1]
    assert row.get("logical_post_id") == res["request_id"], \
        "calendar row must carry the Story Studio request identity"


def test_rebuild_with_new_request_id_stamps_a_new_identity(monkeypatch, tmp_path):
    """Never group by date/image: a rebuilt Story (a freshly minted request_id)
    stages its OWN row with the NEW identity, not the previous request's."""
    first = _create(monkeypatch, tmp_path,
                    {"gym_id": "pierce", "asset_ids": ["a0"],
                     "brief": "Morning crew", "identity_tokens": ["Pierce"],
                     "requested_by": "coach1"})
    new_id = str(uuid.uuid4())
    second = _create(monkeypatch, tmp_path,
                     {"id": new_id, "gym_id": "pierce", "asset_ids": ["a0"],
                      "brief": "Morning crew", "identity_tokens": ["Pierce"],
                      "requested_by": "coach1"})
    assert first["request_id"] != second["request_id"]
    assert len(_STAGED_ROWS) == 2, "a rebuilt story must stage its own row"
    ids = [r[1].get("logical_post_id") for r in _STAGED_ROWS]
    assert ids == [first["request_id"], new_id], \
        "each staged row keeps its own request identity; no date/image grouping"


# ---- rollout flag (ECHO_LOGICAL_POST_ID_ENABLED, default OFF) --------------


@pytest.fixture(autouse=True)
def _logical_post_id_flag_on(monkeypatch):
    """Existing tests in this file exercise the ON behavior."""
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")


def test_flag_defaults_off_and_staged_row_carries_no_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ECHO_LOGICAL_POST_ID_ENABLED", raising=False)
    from agent import config
    assert config.logical_post_id_enabled() is False
    res = _create(monkeypatch, tmp_path,
                  {"gym_id": "pierce", "asset_ids": ["a0", "a1"],
                   "brief": "Members crushed today",
                   "identity_tokens": ["Pierce"], "requested_by": "coach1"})
    assert res["status"] == "staged"
    assert len(_STAGED_ROWS) == 1
    assert "logical_post_id" not in _STAGED_ROWS[0][1]
