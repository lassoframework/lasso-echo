"""Variant-candidate identity hardening (2026-10-04, P1 B) — store boundary.

Under ECHO_LOGICAL_POST_ID_ENABLED ON, create_variant_candidate must treat the
caller-supplied anchor_row as an untrusted SNAPSHOT: it fetches the registered
anchor in its OWN tenant, rejects a foreign/unregistered/changed/forged anchor
BEFORE any INSERT, and stamps the candidate with the REGISTERED anchor's trusted
logical_post_id (or NULL for a historical anchor). The caller's asserted id is
never copied verbatim and never re-minted. OFF (default): the legacy
caller-copy behavior is unchanged. All HTTP is faked; nothing touches a network
or production.
"""
import os
import sys
import uuid as _uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config  # noqa: E402
from agent.portal_calendar_store import PortalStoreError, SupabaseCalendarStore  # noqa: E402


class _Resp:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}
        self.text = ""

    def json(self):
        return self._payload


class _FakeHTTP:
    """get() serves the registered anchor rows; post() records inserts."""

    def __init__(self, rows=None):
        self.rows = dict(rows or {})   # row id -> row dict
        self.posted = []

    def get(self, url, headers=None, params=None, timeout=None):
        row_id = (params or {}).get("id", "")
        if row_id.startswith("eq."):
            row = self.rows.get(row_id[3:])
            return _Resp([row] if row is not None else [])
        return _Resp([])

    def post(self, url, headers=None, json=None, params=None, timeout=None):
        self.posted.append(list(json or []))
        return _Resp(list(json or []), status_code=201)


def _store(http):
    return SupabaseCalendarStore(url="https://sb.test", service_key="k", http=http)


def _anchor(**kw):
    row = {"id": str(_uuid.uuid4()), "gym_id": "gym1", "account": "instagram",
           "post_date": "2026-10-05", "format": "feed", "pillar": "proof",
           "caption": "anchor caption", "status": "approved",
           "variant_status": "active"}
    row.update(kw)
    return row


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")
    assert config.logical_post_id_enabled() is True


# ---- ON: trusted identity from the registered anchor ------------------------

def test_on_candidate_uses_registered_anchor_trusted_id():
    group = str(_uuid.uuid4())
    anchor = _anchor(logical_post_id=group)
    http = _FakeHTTP(rows={anchor["id"]: anchor})
    _store(http).create_variant_candidate(
        "gym1", dict(anchor), "https://img.test/v2.jpg")
    assert http.posted[0][0]["logical_post_id"] == group


def test_on_candidate_stays_null_for_historical_anchor_even_if_caller_asserts_one():
    # The registered anchor predates the identity column (NULL). A caller snapshot
    # that FORGES a valid UUID onto it must not smuggle identity into the insert:
    # the write is rejected as an anchor change.
    anchor = _anchor()  # no logical_post_id registered
    forged = _anchor(id=anchor["id"], logical_post_id=str(_uuid.uuid4()))
    http = _FakeHTTP(rows={anchor["id"]: anchor})
    with pytest.raises(PortalStoreError, match="changed"):
        _store(http).create_variant_candidate(
            "gym1", forged, "https://img.test/v2.jpg")
    assert not http.posted, "a forged identity must reject before INSERT"


def test_on_candidate_uses_trusted_id_not_caller_id_when_both_valid():
    registered_id = str(_uuid.uuid4())
    caller_id = str(_uuid.uuid4())
    anchor = _anchor(logical_post_id=registered_id)
    caller_snapshot = dict(anchor, logical_post_id=caller_id)
    http = _FakeHTTP(rows={anchor["id"]: anchor})
    with pytest.raises(PortalStoreError, match="changed"):
        _store(http).create_variant_candidate(
            "gym1", caller_snapshot, "https://img.test/v2.jpg")
    assert not http.posted


def test_on_rejects_cross_gym_anchor():
    anchor = _anchor(gym_id="other-gym", logical_post_id=str(_uuid.uuid4()))
    http = _FakeHTTP(rows={anchor["id"]: anchor})
    with pytest.raises(PortalStoreError, match="not registered"):
        _store(http).create_variant_candidate(
            "gym1", dict(anchor), "https://img.test/v2.jpg")
    assert not http.posted


def test_on_rejects_unregistered_anchor():
    anchor = _anchor(logical_post_id=str(_uuid.uuid4()))
    http = _FakeHTTP(rows={})   # nothing registered in this tenant
    with pytest.raises(PortalStoreError, match="not registered"):
        _store(http).create_variant_candidate(
            "gym1", dict(anchor), "https://img.test/v2.jpg")
    assert not http.posted


def test_on_rejects_anchor_changed_underneath():
    anchor = _anchor(logical_post_id=str(_uuid.uuid4()))
    registered = dict(anchor, caption="edited caption")
    http = _FakeHTTP(rows={anchor["id"]: registered})
    with pytest.raises(PortalStoreError, match="changed"):
        _store(http).create_variant_candidate(
            "gym1", dict(anchor), "https://img.test/v2.jpg")
    assert not http.posted


def test_on_stale_snapshot_missing_the_registered_id_is_rejected():
    anchor = _anchor(logical_post_id=str(_uuid.uuid4()))
    stale = {k: v for k, v in anchor.items() if k != "logical_post_id"}
    http = _FakeHTTP(rows={anchor["id"]: anchor})
    with pytest.raises(PortalStoreError, match="changed"):
        _store(http).create_variant_candidate(
            "gym1", stale, "https://img.test/v2.jpg")
    assert not http.posted


def test_on_candidate_of_candidate_uses_group_anchor_trusted_id():
    group = str(_uuid.uuid4())
    anchor_id = str(_uuid.uuid4())
    group_anchor = _anchor(id=anchor_id, logical_post_id=group)
    candidate = _anchor(variant_of=anchor_id, variant_status="candidate",
                        logical_post_id=group)
    http = _FakeHTTP(rows={candidate["id"]: candidate, anchor_id: group_anchor})
    _store(http).create_variant_candidate(
        "gym1", dict(candidate), "https://img.test/v3.jpg")
    posted = http.posted[0][0]
    assert posted["logical_post_id"] == group
    assert posted["variant_of"] == anchor_id


# ---- OFF: legacy behavior unchanged ------------------------------------------

def test_off_keeps_caller_copy_and_skips_registration(monkeypatch):
    monkeypatch.delenv("ECHO_LOGICAL_POST_ID_ENABLED", raising=False)
    assert config.logical_post_id_enabled() is False
    group = str(_uuid.uuid4())
    anchor = _anchor(logical_post_id=group)
    http = _FakeHTTP(rows={})   # nothing registered; OFF must not read at all
    row = _store(http).create_variant_candidate(
        "gym1", dict(anchor), "https://img.test/v2.jpg")
    assert http.posted[0][0]["logical_post_id"] == group
    assert row is not None


def test_off_historical_anchor_stays_null(monkeypatch):
    monkeypatch.delenv("ECHO_LOGICAL_POST_ID_ENABLED", raising=False)
    anchor = _anchor()
    http = _FakeHTTP(rows={})
    _store(http).create_variant_candidate(
        "gym1", dict(anchor), "https://img.test/v2.jpg")
    assert "logical_post_id" not in http.posted[0][0]


def test_off_rejects_malformed_anchor_id_loudly(monkeypatch):
    monkeypatch.delenv("ECHO_LOGICAL_POST_ID_ENABLED", raising=False)
    anchor = _anchor(logical_post_id="bogus")
    http = _FakeHTTP(rows={})
    with pytest.raises(ValueError, match="anchor logical_post_id must be a UUID"):
        _store(http).create_variant_candidate(
            "gym1", dict(anchor), "https://img.test/v2.jpg")
    assert not http.posted
