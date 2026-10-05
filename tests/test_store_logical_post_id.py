"""PendingStore logical_post_id durability (agent/store.py), offline SQLite.

Under test:
  * Round-trip: put/get and list_for_account preserve a draft's logical_post_id.
  * ensure_logical_post_id: stamps once under BEGIN IMMEDIATE, idempotent re-calls
    return the same id, stable across a FRESH PendingStore object.
  * Tenant isolation: stamping under the wrong account_key raises and writes nothing.
  * Missing row raises KeyError; invalid stored id fails closed (ValueError).
  * put preserves: a stale whole-draft write that omits the id keeps the stored one;
    a put with a CONFLICTING valid id is refused; an invalid incoming id is refused.
  * Revision conflict: a stale expected_updated_at raises RuntimeError (bounded) and
    the stored id is unchanged.
"""

import os
import sys
import uuid as _uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.drafter import Draft, DraftStatus
from agent.store import PendingStore


def _draft(draft_id="d1", account_key="northside_ig", **kw):
    kw.setdefault("caption", "hi")
    return Draft(
        draft_id=draft_id, account_key=account_key, platform="instagram",
        hashtags=[], creative_path="x.png",
        creative_public_url="https://cdn/x.jpg", scheduled_for="",
        status=DraftStatus.PENDING, day_key="2026-08-06", draft_type="feed",
        **kw)


@pytest.fixture
def store(tmp_path):
    return PendingStore(str(tmp_path / "p.db")), str(tmp_path / "p.db")


def test_round_trip(store):
    st, _ = store
    lp = str(_uuid.uuid4())
    st.put(_draft(logical_post_id=lp))
    assert st.get("d1").logical_post_id == lp
    assert st.list_for_account("northside_ig")[0].logical_post_id == lp


def test_default_empty_round_trip(store):
    st, _ = store
    st.put(_draft())
    assert st.get("d1").logical_post_id == ""


def test_ensure_stamps_once_idempotent(store):
    st, path = store
    st.put(_draft())
    first = st.ensure_logical_post_id("northside_ig", "d1")
    _uuid.UUID(first)
    assert st.ensure_logical_post_id("northside_ig", "d1") == first
    # A fresh store object over the same db reads back the same durable id.
    fresh = PendingStore(path)
    assert fresh.ensure_logical_post_id("northside_ig", "d1") == first
    assert fresh.get("d1").logical_post_id == first


def test_ensure_tenant_isolation(store):
    st, _ = store
    st.put(_draft())
    with pytest.raises(ValueError):
        st.ensure_logical_post_id("other_gym", "d1")
    assert st.get("d1").logical_post_id == ""  # nothing stamped


def test_ensure_missing_row(store):
    st, _ = store
    with pytest.raises(KeyError):
        st.ensure_logical_post_id("northside_ig", "nope")


def test_ensure_invalid_stored_id_fails_closed(store):
    st, _ = store
    st.put(_draft())
    with st._conn() as conn:
        import json as _json
        row = conn.execute("SELECT data FROM drafts WHERE draft_id='d1'").fetchone()
        data = _json.loads(row["data"])
        data["logical_post_id"] = "not-a-uuid"
        conn.execute("UPDATE drafts SET data=? WHERE draft_id='d1'",
                     (_json.dumps(data),))
        conn.commit()
    with pytest.raises(ValueError):
        st.ensure_logical_post_id("northside_ig", "d1")


def test_ensure_revision_conflict_bounded(store):
    st, _ = store
    st.put(_draft())
    with pytest.raises(RuntimeError):
        st.ensure_logical_post_id("northside_ig", "d1",
                                  expected_updated_at="1999-01-01 00:00:00",
                                  retries=2)
    assert st.get("d1").logical_post_id == ""  # never stamped on conflict


def test_stale_put_preserves_stored_id(store):
    st, _ = store
    st.put(_draft())
    stamped = st.ensure_logical_post_id("northside_ig", "d1")
    # A stale whole-draft write built from a pre-stamp read omits the id.
    stale = _draft(caption="edited caption")
    assert stale.logical_post_id == ""
    st.put(stale)
    after = st.get("d1")
    assert after.logical_post_id == stamped
    assert after.caption == "edited caption"  # the routine write still lands


def test_conflicting_put_refused(store):
    st, _ = store
    st.put(_draft())
    stamped = st.ensure_logical_post_id("northside_ig", "d1")
    other = _draft(logical_post_id=str(_uuid.uuid4()))
    with pytest.raises(ValueError):
        st.put(other)
    assert st.get("d1").logical_post_id == stamped  # identity untouched


def test_invalid_incoming_put_refused(store):
    st, _ = store
    with pytest.raises(ValueError):
        st.put(_draft(logical_post_id="garbage"))
    assert st.get("d1") is None  # nothing written


def test_put_with_matching_id_ok(store):
    st, _ = store
    st.put(_draft())
    stamped = st.ensure_logical_post_id("northside_ig", "d1")
    st.put(_draft(logical_post_id=stamped, caption="v2"))
    assert st.get("d1").logical_post_id == stamped


def test_corrupt_stored_id_blank_put_refused_no_mutation(store):
    st, _ = store
    st.put(_draft())
    with st._conn() as conn:
        import json as _json
        row = conn.execute("SELECT data FROM drafts WHERE draft_id='d1'").fetchone()
        data = _json.loads(row["data"])
        data["logical_post_id"] = "not-a-uuid"
        conn.execute("UPDATE drafts SET data=? WHERE draft_id='d1'",
                     (_json.dumps(data),))
        conn.commit()
    # A routine stale write (blank incoming id) must NOT erase the corrupt id.
    with pytest.raises(ValueError):
        st.put(_draft(caption="edited"))
    with st._conn() as conn:
        import json as _json
        row = conn.execute("SELECT data FROM drafts WHERE draft_id='d1'").fetchone()
        data = _json.loads(row["data"])
    assert data["logical_post_id"] == "not-a-uuid"
    assert data["caption"] != "edited"


def test_cross_tenant_same_draft_id_put_refused_no_mutation(store):
    st, _ = store
    st.put(_draft())
    stamped = st.ensure_logical_post_id("northside_ig", "d1")
    other = _draft(account_key="southside_ig", caption="hostile takeover")
    with pytest.raises(ValueError):
        st.put(other)
    # Owner row untouched: id preserved, caption not replaced.
    after = st.get("d1")
    assert after.account_key == "northside_ig"
    assert after.logical_post_id == stamped
    assert after.caption != "hostile takeover"


def test_valid_same_tenant_stale_put_preserves_id(store):
    st, _ = store
    st.put(_draft())
    stamped = st.ensure_logical_post_id("northside_ig", "d1")
    st.put(_draft(caption="edited caption"))
    assert st.get("d1").logical_post_id == stamped
