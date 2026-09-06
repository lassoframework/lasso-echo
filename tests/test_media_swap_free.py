"""
B6 — swapping a wrong photo is FREE; recreating a caption still costs one of 15.

THE BUDGET DESIGN BUG (Pete, zanshin): the portal's only levers are approve / edit /
deny / kill, so the "Use a different photo" chip and the "Caption needs work" chip
are BOTH a deny, and both burn one of the 15 monthly recreates
(portal_social.MONTHLY_RECREATE_BUDGET). Pete ran out of recreates swapping PHOTOS
and then could not fix a caption. The counter was never wrong -- the two actions
were never separated.

Everything offline: a fake store stands in for PostgREST, the photo picker is
injected, no hosting, no network, no library on disk.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import media_swap as msw          # noqa: E402
from agent import portal_social as ps        # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-secret")
    monkeypatch.setenv("AGENT_SOCIAL_BILLING_DELEGATED", "true")
    yield


def _row(row_id="p1", gym_id="zanshin", status="pending", fmt="feed"):
    return {"id": row_id, "gym_id": gym_id, "post_date": "2026-09-20",
            "account": "instagram", "status": status, "format": fmt,
            "caption": "a caption the gym is happy with",
            "image_url": "https://cdn/old.jpg", "source_media_url": "old.jpg",
            "pillar": "community", "scheduled_at": None}


class _Store:
    """Enforces the same gym isolation and the same pending/coach_review write guard
    the real SupabaseCalendarStore.swap_media applies server-side."""

    def __init__(self, rows=None):
        self._rows = {r["id"]: dict(r) for r in (rows or [])}
        self.swaps = []
        self.patches = []

    def get_row(self, account_key, row_id):
        r = self._rows.get(row_id)
        return dict(r) if r and str(r.get("gym_id")) == str(account_key) else None

    def set_status(self, account_key, row_id, status):
        self.patches.append((row_id, status))
        r = self._rows.get(row_id)
        if not r or str(r.get("gym_id")) != str(account_key):
            return None
        r["status"] = status
        return dict(r)

    def swap_media(self, account_key, row_id, image_url, source_media_url=None):
        self.swaps.append((row_id, image_url, source_media_url))
        r = self._rows.get(row_id)
        if not r or str(r.get("gym_id")) != str(account_key):
            return None
        if r.get("status") not in ("pending", "coach_review"):
            return None                      # the server-side status guard
        r["image_url"] = image_url
        if source_media_url is not None:
            r["source_media_url"] = source_media_url
        return dict(r)

    def list_month(self, account_key, month):
        return [dict(r) for r in self._rows.values()]


def _picker(_gym, _row, **_kw):
    return {"ok": True, "image_url": "https://cdn/new.jpg",
            "source_media_url": None, "key": "new.jpg"}


def _wire(monkeypatch, store):
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: store)


# ---- the split itself -------------------------------------------------------

def test_media_swap_is_free_and_repeatable_while_a_caption_recreate_costs_one(
        monkeypatch):
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    store = _Store([_row("p1"), _row("p2")])
    _wire(monkeypatch, store)

    before = ps.recreate_remaining("zanshin")
    for _ in range(5):                       # far more swaps than a month's budget
        status, body = ps.handle_swap_media("zanshin", "p1", "u1", sb_store=store,
                                            picker=_picker)
        assert status == 200 and body["free"] is True
    assert ps.recreate_remaining("zanshin") == before, "a photo swap must cost nothing"

    status, _ = ps.handle_deny("zanshin", "p2", "u1", sb_store=store)
    assert status == 200
    assert ps.recreate_remaining("zanshin") == before - 1, "a caption recreate costs 1"


def test_the_different_photo_chip_routes_to_the_free_swap(monkeypatch):
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    store = _Store([_row("p1")])
    _wire(monkeypatch, store)
    monkeypatch.setattr(msw, "pick_replacement", _picker)

    before = ps.recreate_remaining("zanshin")
    status, body = ps.handle_deny("zanshin", "p1", "u1", intent="media",
                                  sb_store=store)
    assert status == 200 and body["action"] == "swap-media"
    assert ps.recreate_remaining("zanshin") == before
    assert store.patches == [], "a photo swap must never flip the row to denied"
    assert store._rows["p1"]["image_url"] == "https://cdn/new.jpg"


def test_caption_intent_still_charges_exactly_as_before(monkeypatch):
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    store = _Store([_row("p1")])
    _wire(monkeypatch, store)
    before = ps.recreate_remaining("zanshin")
    status, _ = ps.handle_deny("zanshin", "p1", "u1", intent="caption",
                               sb_store=store)
    assert status == 200
    assert ps.recreate_remaining("zanshin") == before - 1


# ---- the flag ---------------------------------------------------------------

def test_flag_off_the_swap_endpoint_403s_and_never_reads_the_store(monkeypatch):
    store = _Store([_row("p1")])
    _wire(monkeypatch, store)
    status, body = ps.handle_swap_media("zanshin", "p1", "u1", sb_store=store,
                                        picker=_picker)
    assert status == 403 and store.swaps == []
    assert body["ok"] is False


def test_flag_off_the_media_intent_is_ignored_and_the_deny_charges(monkeypatch):
    store = _Store([_row("p1")])
    _wire(monkeypatch, store)
    before = ps.recreate_remaining("zanshin")
    status, _ = ps.handle_deny("zanshin", "p1", "u1", intent="media", sb_store=store)
    assert status == 200
    assert ps.recreate_remaining("zanshin") == before - 1, "flag off = today's behavior"


# ---- what a swap may never do ----------------------------------------------

def test_an_approved_post_keeps_the_pixels_the_gym_approved(monkeypatch):
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    store = _Store([_row("p1", status="approved")])
    _wire(monkeypatch, store)
    status, body = ps.handle_swap_media("zanshin", "p1", "u1", sb_store=store,
                                        picker=_picker)
    assert status == 409
    assert store._rows["p1"]["image_url"] == "https://cdn/old.jpg"


def test_a_cross_gym_post_id_is_a_404_and_no_swap_is_attempted(monkeypatch):
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    store = _Store([_row("p1", gym_id="pierce")])
    _wire(monkeypatch, store)
    status, _ = ps.handle_swap_media("zanshin", "p1", "u1", sb_store=store,
                                     picker=_picker)
    assert status == 404 and store.swaps == []


def test_no_fresh_photo_is_a_409_that_costs_nothing_and_says_what_to_do(monkeypatch):
    monkeypatch.setenv("ECHO_MEDIA_SWAP_FREE", "true")
    store = _Store([_row("p1")])
    _wire(monkeypatch, store)
    before = ps.recreate_remaining("zanshin")
    status, body = ps.handle_swap_media(
        "zanshin", "p1", "u1", sb_store=store,
        picker=lambda *a, **k: {"ok": False, "reason": msw.REASON_NO_FRESH_PHOTO})
    assert status == 409
    assert ps.recreate_remaining("zanshin") == before
    assert "recreates were not touched" in body["error"]
    assert store._rows["p1"]["image_url"] == "https://cdn/old.jpg"


# ---- the picker itself ------------------------------------------------------

def test_pick_replacement_never_returns_the_photo_the_row_already_has():
    seen = {}

    def _fresh(lib, state, exclude):
        seen["exclude"] = set(exclude)
        return "new.jpg", "/lib/new.jpg"

    out = msw.pick_replacement(
        "zanshin", _row("p1"), store=_Store(), library_path="/lib",
        book_state={"old.jpg": {("2026-09-20", "pending")}},
        fresh_fn=_fresh, host_fn=lambda p: "https://cdn/new.jpg",
        feed_fn=lambda p: "https://cdn/new__feed.jpg")
    assert out["ok"] is True and out["image_url"] == "https://cdn/new__feed.jpg"
    assert "old.jpg" in seen["exclude"], "the row's own photo must be excluded"


def test_pick_replacement_reports_no_library_instead_of_borrowing_another_gyms():
    out = msw.pick_replacement("zanshin", _row("p1"), store=_Store(),
                               library_path="")
    assert out == {"ok": False, "reason": msw.REASON_NO_LIBRARY}


def test_pick_replacement_refuses_when_hosting_is_down_rather_than_half_swapping():
    out = msw.pick_replacement(
        "zanshin", _row("p1"), store=_Store(), library_path="/lib", book_state={},
        fresh_fn=lambda *a: ("new.jpg", "/lib/new.jpg"), host_fn=lambda p: "")
    assert out == {"ok": False, "reason": msw.REASON_HOSTING}


def test_a_story_swap_reburns_its_caption_and_never_ships_a_bare_photo(monkeypatch):
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    monkeypatch.setenv("AGENT_STORY_SOURCE_MEDIA", "true")
    out = msw.pick_replacement(
        "zanshin", _row("p1", fmt="story"), store=_Store(), library_path="/lib",
        book_state={}, fresh_fn=lambda *a: ("new.jpg", "/lib/new.jpg"),
        host_fn=lambda p: "https://cdn/new.jpg",
        reburn_fn=lambda *a: "https://cdn/new__story.jpg")
    assert out["image_url"] == "https://cdn/new__story.jpg"
    assert out["source_media_url"] == "https://cdn/new.jpg"


def test_a_failed_story_reburn_changes_nothing(monkeypatch):
    monkeypatch.setenv("AGENT_STORY_FORMAT", "true")
    out = msw.pick_replacement(
        "zanshin", _row("p1", fmt="story"), store=_Store(), library_path="/lib",
        book_state={}, fresh_fn=lambda *a: ("new.jpg", "/lib/new.jpg"),
        host_fn=lambda p: "https://cdn/new.jpg", reburn_fn=lambda *a: None)
    assert out == {"ok": False, "reason": msw.REASON_STORY_REBURN}


def test_client_messages_carry_no_dashes_and_never_say_vendor():
    for reason in (msw.REASON_NO_LIBRARY, msw.REASON_NO_FRESH_PHOTO,
                   msw.REASON_HOSTING, msw.REASON_STORY_REBURN):
        msg = msw.client_message(reason)
        assert "—" not in msg and "–" not in msg and "-" not in msg
        assert "vendor" not in msg.lower()


# ---- P-11: the exact contract the PORTAL relays against ---------------------
# The portal builds
#   `${base}/portal/${token}/posts/${postId}/${action}`
# in src/lib/echo/portal-content.ts postPostAction, and its ACTIONS set is
# {approve, edit, deny, kill, requeue}. "Photo swap is free" could not be built
# there because Echo had no swap action to relay to. This pins the path shape so
# the portal can add "swap-media" to that set and have it land.

def test_the_portal_relay_path_for_swap_media_is_routable():
    import re

    from agent import intake_web

    actions = set(intake_web.PORTAL_POST_ACTIONS)
    # Everything the portal can already relay, plus the new one.
    assert {"approve", "edit", "deny", "kill"} <= actions
    assert "swap-media" in actions, \
        "the portal relays /posts/<id>/swap-media; Echo must route it"

    # And the path the portal actually builds must match the live route regex.
    pattern = (r"^/portal/([A-Za-z0-9_.-]{8,})/posts/([A-Za-z0-9_-]+)/"
               r"(" + "|".join(intake_web.PORTAL_POST_ACTIONS) + r")$")
    m = re.match(pattern, "/portal/eyJhIjoiZW5nIn0.sig/posts/abc-123/swap-media")
    assert m and m.group(3) == "swap-media"
