"""
A calendar rebuild (the nightly delete-then-insert every plan/mirror/client lane runs)
must NEVER destroy or duplicate a HUMAN OWNED row. This is the fix for Dale's approvals
reverting to "waiting on you": the rebuild used to delete the whole month (his approved
posts included) and re-insert fresh 'pending' rows.

Covers:
  * delete_month(preserve_human=True) only deletes wipeable rows (adds the status guard).
  * delete_month(preserve_human=False) does a full wipe (no status guard).
  * locked_slots() returns only the human-owned cells.
  * preserve_and_prune() drops rows that collide with a locked slot, keeps the rest, and
    is safe when the store has no locked_slots / when the read fails.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_calendar_store as pcs  # noqa: E402


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text

    def json(self):
        return self._payload


class _FakeHTTP:
    def __init__(self, get_resp=None, delete_resp=None):
        self.calls = []
        self._get_resp = get_resp or _Resp(200, [])
        self._delete_resp = delete_resp or _Resp(200, [])

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("get", url, params or {}, headers or {}))
        return self._get_resp

    def delete(self, url, params=None, headers=None, json=None, timeout=None):
        self.calls.append(("delete", url, params or {}, headers or {}, json))
        return self._delete_resp


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-secret")
    yield


def _row(gym_id="eng", post_date="2026-08-13", account="instagram",
         fmt="feed", status="pending"):
    return {"gym_id": gym_id, "post_date": post_date, "account": account,
            "format": fmt, "status": status}


# ---- delete_month status guard -------------------------------------------

def test_delete_month_preserves_human_rows_by_default(monkeypatch):
    http = _FakeHTTP(delete_resp=_Resp(200, []))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    pcs.SupabaseCalendarStore().delete_month("eng", "2026-08")
    method, url, params, headers, _ = http.calls[0]
    assert method == "delete"
    assert params["gym_id"] == "eq.eng"
    # the status guard: only NULL or a wipeable status is deleted
    assert params["or"] == "(status.is.null,status.in.(pending,draft,queued))"


def test_delete_month_full_wipe_when_preserve_off(monkeypatch):
    http = _FakeHTTP(delete_resp=_Resp(200, []))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    pcs.SupabaseCalendarStore().delete_month("eng", "2026-08", preserve_human=False)
    _, _, params, _, _ = http.calls[0]
    assert "or" not in params            # no status guard -> deletes everything


# ---- locked_slots --------------------------------------------------------

def test_locked_slots_returns_only_human_owned(monkeypatch):
    rows = [
        _row(status="pending"),                                   # wipeable
        _row(account="facebook", status="approved"),              # LOCKED
        _row(fmt="story", status="published"),                    # LOCKED
        _row(post_date="2026-08-14", status="draft"),             # wipeable
        _row(post_date="2026-08-15", status="denied"),            # LOCKED
    ]
    http = _FakeHTTP(get_resp=_Resp(200, rows))
    monkeypatch.setattr(pcs.SupabaseCalendarStore, "_client", lambda self: http)
    locked = pcs.SupabaseCalendarStore().locked_slots("eng", "2026-08")
    assert locked == {
        ("2026-08-13", "facebook", "feed"),
        ("2026-08-13", "instagram", "story"),
        ("2026-08-15", "instagram", "feed"),
    }


# ---- preserve_and_prune --------------------------------------------------

class _StoreWithLocks:
    def __init__(self, locked):
        self._locked = locked

    def locked_slots(self, account_key, month):
        return self._locked


def test_prune_drops_colliding_rows_keeps_others():
    locked = {("2026-08-13", "instagram", "feed")}
    incoming = [
        _row(account="instagram", fmt="feed"),        # collides -> dropped
        _row(account="facebook", fmt="feed"),         # kept
        _row(account="instagram", fmt="story"),       # kept
    ]
    kept, n = pcs.preserve_and_prune(_StoreWithLocks(locked), "eng",
                                     ["2026-08"], incoming)
    assert n == 1
    slots = {pcs._slot_key(r) for r in kept}
    assert ("2026-08-13", "instagram", "feed") not in slots
    assert len(kept) == 2


def test_prune_keeps_all_when_store_has_no_locked_slots():
    class _Bare:
        pass
    incoming = [_row(), _row(account="facebook")]
    kept, n = pcs.preserve_and_prune(_Bare(), "eng", ["2026-08"], incoming)
    assert kept == incoming and n == 0


def test_prune_keeps_all_when_read_fails():
    class _Boom:
        def locked_slots(self, *a, **k):
            raise RuntimeError("supabase down")
    incoming = [_row(), _row(fmt="story")]
    kept, n = pcs.preserve_and_prune(_Boom(), "eng", ["2026-08"], incoming)
    assert kept == incoming and n == 0


# ---- the client rebuild lane honors the guard end to end ------------------

class _ApplyStore:
    """Records delete/insert and reports one already-approved slot as locked."""
    def __init__(self, locked):
        self._locked = locked
        self.deleted = []
        self.inserted = []

    def locked_slots(self, account_key, month):
        return self._locked

    def delete_month(self, account_key, month, *, preserve_human=True):
        # the lane must keep asking for a preserving delete, never a full wipe
        assert preserve_human is True
        self.deleted.append((account_key, month))
        return 0

    def insert_rows(self, account_key, rows):
        self.inserted.extend(rows)
        return rows


def test_client_apply_skips_locked_slot_and_still_deletes():
    from agent.client_month_run import _apply
    from datetime import date
    locked = {("2026-08-13", "instagram", "feed")}
    store = _ApplyStore(locked)
    rows = [
        _row(account="instagram", fmt="feed"),        # collides w/ approved -> skip
        _row(account="facebook", fmt="feed"),         # inserted
        _row(account="instagram", fmt="story"),       # inserted
    ]
    out = _apply("eng", rows, date(2026, 8, 13), 30, store, lambda m: None)
    assert out["ok"] is True
    assert store.deleted                              # delete lane still runs
    inserted_slots = {pcs._slot_key(r) for r in store.inserted}
    assert ("2026-08-13", "instagram", "feed") not in inserted_slots
    assert out["upserted"] == 2
