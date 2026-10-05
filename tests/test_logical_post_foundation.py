"""Logical post identity foundation (2026-10-04) — store payload behavior.

A logical post is one IG feed, its FB mirror, and its paired caption-burned
Story: sibling rows share ONE immutable logical_post_id minted upstream.
insert_rows must pass a caller-provided UUID through verbatim, must NEVER mint
one per row, and must fail invalid non-UUID values loudly. create_variant_candidate
must inherit the anchor's logical_post_id so a promoted variant keeps identity.
All HTTP is faked; nothing here touches a network or production.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.portal_calendar_store import SupabaseCalendarStore  # noqa: E402


class _FakeResp:
    def __init__(self, payload, status_code=201, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = dict(headers or {})
        self.text = ""

    def json(self):
        return self._payload


class _FakeHTTP:
    def __init__(self):
        self.posted = []

    def post(self, url, headers=None, json=None, params=None, timeout=None):
        self.posted.append(list(json or []))
        return _FakeResp(list(json or []))

    def get(self, url, headers=None, params=None, timeout=None):
        return _FakeResp([], status_code=200, headers={"Content-Range": "*/0"})


def _store(http):
    return SupabaseCalendarStore(url="https://sb.test", service_key="k", http=http)


def _row(**kw):
    r = {"account": "instagram", "post_date": "2026-10-05",
         "time_slot": "evening", "format": "feed",
         "caption": "hook line for the post", "status": "pending",
         "image_url": "https://img.test/a.jpg"}
    r.update(kw)
    return r


# ---- insert_rows: pass-through, no minting, strict validation --------------------

def test_insert_rows_passes_caller_logical_post_id_to_every_sibling():
    group = str(uuid.uuid4())
    http = _FakeHTTP()
    inserted = _store(http).insert_rows("gym1", [
        _row(logical_post_id=group),
        _row(logical_post_id=group, format="story", time_slot="early_morning"),
    ])
    assert len(inserted) == 2
    assert all(r["logical_post_id"] == group for r in inserted)
    # the exact POST body carried it (normalized union keys also carry it)
    assert all(r["logical_post_id"] == group for r in http.posted[0])


def test_insert_rows_never_mints_an_id():
    http = _FakeHTTP()
    inserted = _store(http).insert_rows("gym1", [_row(), _row(
        time_slot="early_morning", format="story")])
    assert len(inserted) == 2
    assert all(r.get("logical_post_id") is None for r in inserted)


def test_insert_rows_normalizes_uuid_text_and_nones():
    group = uuid.uuid4()
    http = _FakeHTTP()
    inserted = _store(http).insert_rows("gym1", [
        _row(logical_post_id=group),          # uuid object accepted
        _row(logical_post_id=str(group).upper(), time_slot="early_morning",
             format="story"),                  # uppercase text accepted
    ])
    assert [r["logical_post_id"] for r in inserted] == [str(group)] * 2


@pytest.mark.parametrize("bad", ["not-a-uuid", "12345", "", {"x": 1}, 42])
def test_insert_rows_rejects_invalid_logical_post_id(bad):
    http = _FakeHTTP()
    with pytest.raises(ValueError, match="logical_post_id must be a UUID"):
        _store(http).insert_rows("gym1", [_row(logical_post_id=bad)])
    assert not http.posted, "nothing may be staged when an ID is invalid"


# ---- create_variant_candidate: identity preservation ------------------------------

def test_variant_candidate_inherits_anchor_logical_post_id():
    group = str(uuid.uuid4())
    http = _FakeHTTP()
    anchor = {"id": str(uuid.uuid4()), "gym_id": "gym1", "account": "instagram",
              "post_date": "2026-10-05", "format": "feed", "pillar": "proof",
              "caption": "anchor caption", "logical_post_id": group}
    _store(http).create_variant_candidate(
        "gym1", anchor, "https://img.test/v2.jpg")
    assert http.posted[0][0]["logical_post_id"] == group
    assert http.posted[0][0]["gym_id"] == "gym1"


def test_variant_candidate_stays_null_when_anchor_has_no_id():
    http = _FakeHTTP()
    anchor = {"id": str(uuid.uuid4()), "gym_id": "gym1", "account": "instagram",
              "post_date": "2026-10-05", "format": "feed", "pillar": "proof",
              "caption": "anchor caption"}
    row = _store(http).create_variant_candidate(
        "gym1", anchor, "https://img.test/v2.jpg")
    assert "logical_post_id" not in http.posted[0][0]
    assert row is not None


def test_variant_candidate_via_group_candidate_keeps_same_id():
    """A candidate created off another candidate (variant_of set) still carries
    the group's single logical_post_id."""
    group = str(uuid.uuid4())
    http = _FakeHTTP()
    anchor_id = str(uuid.uuid4())
    candidate = {"id": str(uuid.uuid4()), "variant_of": anchor_id,
                 "gym_id": "gym1", "account": "instagram",
                 "post_date": "2026-10-05", "format": "feed", "pillar": "proof",
                 "caption": "c1", "logical_post_id": group}
    _store(http).create_variant_candidate(
        "gym1", candidate, "https://img.test/v3.jpg")
    posted = http.posted[0][0]
    assert posted["logical_post_id"] == group
    assert posted["variant_of"] == anchor_id


def test_variant_candidate_rejects_invalid_anchor_id():
    http = _FakeHTTP()
    anchor = {"id": str(uuid.uuid4()), "gym_id": "gym1", "account": "instagram",
              "post_date": "2026-10-05", "format": "feed", "pillar": "proof",
              "caption": "c", "logical_post_id": "bogus"}
    with pytest.raises(ValueError, match="anchor logical_post_id must be a UUID"):
        _store(http).create_variant_candidate(
            "gym1", anchor, "https://img.test/v2.jpg")
    assert not http.posted


# ---- rollout flag (ECHO_LOGICAL_POST_ID_ENABLED, default OFF) --------------
# The generic calendar store is NOT behind the flag: it keeps accepting
# caller-provided logical_post_id values and rejecting invalid ones regardless
# (covered above). Only the forward writers are gated. This pins the config
# contract the writers read.

def test_logical_post_id_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("ECHO_LOGICAL_POST_ID_ENABLED", raising=False)
    from agent import config
    assert config.logical_post_id_enabled() is False


def test_logical_post_id_flag_truthy_pattern(monkeypatch):
    from agent import config
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")
    assert config.logical_post_id_enabled() is True
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "1")
    assert config.logical_post_id_enabled() is True
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "yes")
    assert config.logical_post_id_enabled() is True
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "0")
    assert config.logical_post_id_enabled() is False
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "nonsense")
    assert config.logical_post_id_enabled() is False
