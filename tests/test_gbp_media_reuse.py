"""
Nine-month media-reuse guard at the GBP outbound boundary.

calendar_autopublish.py skips googlebusiness rows, so the policy added there never
covered GBP. This pins the same publish_hold_reason check inside agent/gbp_worker.py:
  * a reuse-policy gym (zanshinfitness630e22) is HELD before the live Zernio call,
    for BOTH the posts API (publish_gbp_row) and the gallery photo drop
    (publish_photo_drop), and the held marker survives publish_one so publish_due_gbp
    releases the exactly-once claim and counts the row as held;
  * gyms without a reuse policy never touch the history store (no database reads);
  * draft rehearsals never touch the history store either.
Offline: the Zernio client and both stores are fakes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gbp_worker as gw  # noqa: E402

_GYM = "zanshinfitness630e22"     # reuse_months() == 9
_OTHER_GYM = "eng"                # reuse_months() == 0
_GOOD_CAPTION = ("Carmel strength for busy parents: a plan that fits real life and "
                 "actually sticks. Come see the floor and meet the coaches this week.")
_IMAGE = "https://cdn.example/zanshin/photo_a.jpg"


def _conn():
    return {"zernio_account_id": "acc_gbp_1", "gbp_location_id": "locations/123"}


def _row(**over):
    r = {"id": "row_1", "gym_id": _GYM, "caption": _GOOD_CAPTION,
         "image_url": _IMAGE, "gbp_topic_type": "STANDARD",
         "gbp_cta_type": "LEARN_MORE", "gbp_cta_url": "https://gym.com/start",
         "pillar": "Local Update", "gbp_event": None, "gbp_offer": None,
         "format": "update"}
    r.update(over)
    return r


class _FakeClient:
    """Records outbound calls; raises if anything tries to actually reach Zernio
    semantics the test did not intend (both methods are expected to be blocked)."""

    def __init__(self):
        self.post_calls = []
        self.media_calls = []

    def create_post_raw(self, payload, *, draft=False, publish_now=True):
        self.post_calls.append({"payload": payload, "draft": draft})
        return {"_id": "zpost_gbp_1"}

    def create_gmb_media(self, account_id, image_url):
        self.media_calls.append({"account_id": account_id, "image_url": image_url})
        return {"_id": "zmedia_1"}


class _HistoryStore:
    """Stand-in for SupabaseCalendarStore. `history` rows are returned for any
    cutoff; touch() counts reads so a test can assert the DB was never queried."""

    def __init__(self, history=()):
        self.history = list(history)
        self.reads = 0

    def list_media_publish_history(self, gym_id, cutoff_iso):
        self.reads += 1
        return list(self.history)


class _ExplodingHistoryStore(_HistoryStore):
    def list_media_publish_history(self, gym_id, cutoff_iso):
        raise AssertionError("history store must not be touched for this client/run")


class _MediaStore:
    def __init__(self, assets=()):
        self.assets = list(assets)
        self.reads = 0

    def list_assets(self, gym_id):
        self.reads += 1
        return list(self.assets)


def _reused_history():
    """A prior published row with the SAME image basename -> the nine-month hold."""
    return [{"id": "row_old", "gym_id": _GYM, "image_url": _IMAGE}]


# ---- posts API -------------------------------------------------------------

def test_live_post_reused_media_is_held_before_any_outbound_call():
    c = _FakeClient()
    store = _HistoryStore(_reused_history())
    out = gw.publish_gbp_row(_row(), _conn(), client=c, draft=False,
                             history_store=store, media_store=_MediaStore())
    assert out.get("held") == "media_reuse"
    assert out["status"] == "approved" and not out["ok"]
    assert out["reject_reason"] == "media_reuse_nine_month_hold"
    assert c.post_calls == [], "a held row must never reach Zernio create_post_raw"
    assert store.reads == 1


def test_live_post_fresh_media_sends_normally():
    c = _FakeClient()
    store = _HistoryStore([])   # no prior use of this media
    out = gw.publish_gbp_row(_row(), _conn(), client=c, draft=False,
                             history_store=store, media_store=_MediaStore())
    assert out["ok"] and out["status"] == "published" and out["mode"] == "live"
    assert len(c.post_calls) == 1


def test_non_reuse_gym_live_post_never_reads_history():
    c = _FakeClient()
    out = gw.publish_gbp_row(_row(gym_id=_OTHER_GYM), _conn(), client=c, draft=False,
                             history_store=_ExplodingHistoryStore(),
                             media_store=_MediaStore())
    assert out["ok"] and out["status"] == "published"
    assert len(c.post_calls) == 1, "other clients publish exactly as before"


def test_draft_post_never_reads_history():
    c = _FakeClient()
    out = gw.publish_gbp_row(_row(), _conn(), client=c, draft=True,
                             history_store=_ExplodingHistoryStore(),
                             media_store=_MediaStore())
    assert out["ok"] and out["mode"] == "draft"
    assert len(c.post_calls) == 1 and c.post_calls[0]["draft"] is True


# ---- gallery photo drop (§6.4) ---------------------------------------------

def _photo_row(**over):
    return _row(format="photo", caption="", **over)


def test_live_photo_drop_reused_media_is_held_before_gmb_media():
    c = _FakeClient()
    store = _HistoryStore(_reused_history())
    out = gw.publish_photo_drop(_photo_row(), _conn(), client=c, draft=False,
                                history_store=store, media_store=_MediaStore())
    assert out.get("held") == "media_reuse" and not out["ok"]
    assert out["reject_reason"] == "media_reuse_nine_month_hold"
    assert c.media_calls == [], "a held photo must never reach Zernio gmb-media"
    assert store.reads == 1


def test_non_reuse_gym_photo_drop_never_reads_history():
    c = _FakeClient()
    out = gw.publish_photo_drop(_photo_row(gym_id=_OTHER_GYM), _conn(), client=c,
                                draft=False, history_store=_ExplodingHistoryStore(),
                                media_store=_MediaStore())
    assert out["ok"] and out["status"] == "published" and out["mode"] == "live"
    assert len(c.media_calls) == 1


def test_draft_photo_drop_never_reads_history():
    c = _FakeClient()
    out = gw.publish_photo_drop(_photo_row(), _conn(), client=c, draft=True,
                                history_store=_ExplodingHistoryStore(),
                                media_store=_MediaStore())
    assert out["ok"] and out["mode"] == "draft"
    assert c.media_calls == [], "draft photo drop never calls gmb-media at all"


# ---- held marker survives publish_one / publish_due_gbp ---------------------

def test_publish_one_carries_held_for_post_and_photo():
    store = _HistoryStore(_reused_history())
    conns = [dict(_conn(), status="connected")]
    c = _FakeClient()
    res = gw.publish_one(_row(), conns, client=c, draft=False, history_store=store,
                         media_store=_MediaStore())
    assert res.get("held") == "media_reuse" and res["status"] == "approved"
    res = gw.publish_one(_photo_row(), conns, client=c, draft=False,
                         history_store=store, media_store=_MediaStore())
    assert res.get("held") == "media_reuse" and res["status"] == "approved"
    assert c.post_calls == [] and c.media_calls == []


class _DueStore:
    """Minimum publish_due_gbp surface: one approved GBP row + claim handling."""

    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.claimed = []
        self.released = []

    def approved_gbp_rows(self, run_date):
        return [dict(r) for r in self.rows.values()
                if r["status"] == "approved"]

    def connections_for(self, gym):
        return [dict(_conn(), status="connected")]

    def claim_publishing(self, row_id):
        self.claimed.append(row_id)
        return True

    def mark_status(self, row_id, status):
        self.released.append((row_id, status))
        self.rows[row_id]["status"] = status

    def mark_failed(self, row_id, reason):
        self.rows[row_id]["status"] = "failed"

    def mark_published(self, *a, **k):
        raise AssertionError("a held row must never be marked published")


def test_publish_due_gbp_counts_held_and_releases_claim():
    store = _DueStore([_row(id="row_held", status="approved")])
    c = _FakeClient()
    summary = gw.publish_due_gbp(store, c, run_date="2026-09-27", draft=False,
                                 history_store=_HistoryStore(_reused_history()),
                                 media_store=_MediaStore())
    assert summary["held"] == 1 and summary["published"] == 0
    assert store.claimed == ["row_held"], "claim flips approved -> publishing first"
    assert ("row_held", "approved") in store.released, \
        "a held row must be released back to approved, never stranded in publishing"
    assert c.post_calls == []
