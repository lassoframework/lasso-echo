"""
caption-only recreate — the missing other half of B6 (Blake, 2026-09-07): media_swap
made "the photo is wrong" free by keeping the caption and swapping only the pixels.
This module is "the caption is wrong": rewrite ONLY the copy, on the gym's EXACT SAME
photo, instead of today's full deny/recreate which can (and does) hand back a
different photo even though only the words were wrong.

Fully offline: SB7/make_caption is injected, a real tmp-dir library stands in for
the gym's photo folder (so row-photo -> local-creative resolution is exercised for
real, not mocked away), and a fake Supabase-shaped store stands in for PostgREST.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import caption_swap as cs          # noqa: E402
from agent import portal_social as ps         # noqa: E402
from agent import client_content, client_sources, media_guard  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.voice import VoiceDoc              # noqa: E402


class _Source:
    def __init__(self, text, sid="s1", citation="c1"):
        self.text = text
        self.id = sid
        self.citation = citation


def _acct(key="zanshin_ig"):
    return Account(key=key, display_name="Zanshin", platform=Platform.INSTAGRAM,
                  token_env="T", target_id_env="TID")


def _voice():
    return VoiceDoc(raw="Zanshin voice.", hashtags=["#zanshin"], ctas=["Book now."])


def _row(row_id="p1", gym_id="zanshin", status="pending", caption="old caption body",
         image="old.jpg"):
    return {"id": row_id, "gym_id": gym_id, "post_date": "2026-09-20",
            "account": "instagram", "status": status, "format": "feed",
            "caption": caption, "image_url": f"https://cdn/{image}",
            "source_media_url": image}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-secret")
    monkeypatch.setenv("AGENT_SOCIAL_BILLING_DELEGATED", "true")
    yield


@pytest.fixture
def lib(tmp_path):
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "old.jpg").write_bytes(b"fake-photo-bytes")
    return str(lib_dir)


def _wire_source(monkeypatch, source_text="Our members love the community."):
    """Point the deterministic day-source resolution at one fake, non-numeric
    (so the fabrication gate is trivially clean) approved source."""
    monkeypatch.setattr(client_sources, "categories_present",
                        lambda account_key, status="approved": ["general"])
    monkeypatch.setattr(client_content, "category_for_day",
                        lambda account_key, day_key, present=None: "general")
    monkeypatch.setattr(client_content, "_pillars_for",
                        lambda account_key, present=None: ["general"])
    monkeypatch.setattr(client_content, "_source_for_day",
                        lambda account_key, day_key, category, present:
                        _Source(source_text))
    monkeypatch.setattr(client_sources, "approved_claims",
                        lambda account_key: [source_text])


# A caption must clear the REAL post_quality gate in most tests below (>=40 chars,
# >=12 words, no dash, no banned word) -- these two are used throughout as clean,
# already-qualifying bodies.
_NEW_CAPTION = ("A brand new caption about the community you have built here "
               "together every single week.")
_ANOTHER_CAPTION = ("A genuinely different caption this time about showing up "
                    "for yourself and your people.")


# ---- caption_swap.recreate_caption: the recipe itself -----------------------

def test_recreates_the_caption_and_keeps_the_same_photo(monkeypatch, lib):
    _wire_source(monkeypatch)
    calls = []

    def _make_caption(account, source, voice, creative_key, creative=None,
                      avoid_openings=(), verified=None, angle=""):
        calls.append({"creative_key": creative_key, "creative": creative,
                     "angle": angle, "avoid_openings": list(avoid_openings)})
        return _NEW_CAPTION, ["#zanshin"]

    result = cs.recreate_caption(
        "zanshin", _row(), account=_acct(), voice=_voice(), library_path=lib,
        make_caption_fn=_make_caption)

    assert result["ok"] is True
    assert result["caption"] == _NEW_CAPTION
    assert result["hashtags"] == ["#zanshin"]
    # Exactly one call needed (the first candidate cleared the real A+ gate).
    assert len(calls) == 1
    assert calls[0]["creative_key"] == "old.jpg"
    assert calls[0]["creative"] is not None
    assert os.path.basename(calls[0]["creative"].path) == "old.jpg"
    # The row's own CURRENT caption seeded the avoid-openings list, so the regen
    # is nudged away from repeating its own opening hook.
    assert calls[0]["avoid_openings"]
    assert "old" in calls[0]["avoid_openings"][0]


def test_never_reproduces_the_same_caption_verbatim(monkeypatch, lib):
    _wire_source(monkeypatch)
    seen = {"n": 0}

    def _make_caption(account, source, voice, creative_key, creative=None,
                      avoid_openings=(), verified=None, angle=""):
        seen["n"] += 1
        if seen["n"] == 1:
            # SB7 (rarely) echoes back exactly what is already on the post.
            return "the old caption that is already on this post today", ["#zanshin"]
        return _ANOTHER_CAPTION, ["#zanshin"]

    result = cs.recreate_caption(
        "zanshin",
        _row(caption="the old caption that is already on this post today"),
        account=_acct(), voice=_voice(), library_path=lib,
        make_caption_fn=_make_caption)

    assert result["ok"] is True
    assert result["caption"] == _ANOTHER_CAPTION
    assert seen["n"] == 2, "an identical re-ask must be retried, not shipped"


def test_bounded_retries_then_leaves_the_caption_unchanged(monkeypatch, lib):
    _wire_source(monkeypatch)
    from agent import post_quality
    monkeypatch.setattr(post_quality, "is_a_plus", lambda draft, banned=(): False)
    attempts = {"n": 0}

    def _make_caption(account, source, voice, creative_key, creative=None,
                      avoid_openings=(), verified=None, angle=""):
        attempts["n"] += 1
        return f"attempt number {attempts['n']} caption body text here", ["#z"]

    result = cs.recreate_caption(
        "zanshin", _row(), account=_acct(), voice=_voice(), library_path=lib,
        make_caption_fn=_make_caption)

    assert result == {"ok": False, "reason": cs.REASON_GATE_EXHAUSTED}
    assert attempts["n"] == cs.MAX_ATTEMPTS, "retries must be bounded, not unlimited"


def test_no_approved_source_left_refuses_rather_than_fabricate(monkeypatch, lib):
    monkeypatch.setattr(client_sources, "categories_present",
                        lambda account_key, status="approved": [])
    result = cs.recreate_caption(
        "zanshin", _row(), account=_acct(), voice=_voice(), library_path=lib,
        make_caption_fn=lambda *a, **k: ("x", []))
    assert result == {"ok": False, "reason": cs.REASON_NO_SOURCE}


def test_no_library_refuses_rather_than_borrow_another_gyms_photo(monkeypatch):
    _wire_source(monkeypatch)
    result = cs.recreate_caption(
        "zanshin", _row(), account=_acct(), voice=_voice(), library_path="",
        make_caption_fn=lambda *a, **k: ("x", []))
    assert result == {"ok": False, "reason": cs.REASON_NO_LIBRARY}


def test_missing_local_photo_refuses_rather_than_ship_ungrounded(monkeypatch, tmp_path):
    _wire_source(monkeypatch)
    empty_lib = tmp_path / "empty"
    empty_lib.mkdir()
    result = cs.recreate_caption(
        "zanshin", _row(), account=_acct(), voice=_voice(),
        library_path=str(empty_lib), make_caption_fn=lambda *a, **k: ("x", []))
    assert result == {"ok": False, "reason": cs.REASON_NO_PHOTO}


def test_vision_gyms_re_verify_the_same_photo_before_shipping(monkeypatch, lib):
    _wire_source(monkeypatch)
    monkeypatch.setattr(cs.config, "vision_enabled_for", lambda key: True)
    from agent import vision
    verify_calls = []
    monkeypatch.setattr(vision, "stored_analysis", lambda path: {"labels": []})
    monkeypatch.setattr(vision, "crop_verify",
                        lambda img_bytes, analysis, reader=None:
                        verify_calls.append(1) or {"ok": True})
    monkeypatch.setattr(vision, "context_usable", lambda ctx, banned_words=(): (True, []))

    def _make_caption(account, source, voice, creative_key, creative=None,
                      avoid_openings=(), verified=None, angle=""):
        assert verified == {"ok": True}, "the fresh crop-verify result must be threaded through"
        return ("A fresh caption grounded in the same verified photo that "
                "shows exactly what it always showed."), ["#z"]

    result = cs.recreate_caption(
        "zanshin", _row(), account=_acct(), voice=_voice(), library_path=lib,
        make_caption_fn=_make_caption)
    assert result["ok"] is True
    assert verify_calls == [1], "crop-verify must run exactly once for an unchanged photo"


def test_vision_gym_missing_local_file_refuses_never_skips_the_check(monkeypatch,
                                                                    tmp_path):
    _wire_source(monkeypatch)
    monkeypatch.setattr(cs.config, "vision_enabled_for", lambda key: True)
    empty_lib = tmp_path / "empty2"
    empty_lib.mkdir()
    result = cs.recreate_caption(
        "zanshin", _row(), account=_acct(), voice=_voice(),
        library_path=str(empty_lib), make_caption_fn=lambda *a, **k: ("x", []))
    assert result == {"ok": False, "reason": cs.REASON_NO_PHOTO}


# ---- portal_social wiring ----------------------------------------------------

class _Store:
    def __init__(self, rows=None):
        self._rows = {r["id"]: dict(r) for r in (rows or [])}
        self.caption_patches = []

    def get_row(self, account_key, row_id):
        r = self._rows.get(row_id)
        return dict(r) if r and str(r.get("gym_id")) == str(account_key) else None

    def patch_caption(self, account_key, row_id, new_caption):
        self.caption_patches.append((row_id, new_caption))
        r = self._rows.get(row_id)
        if not r or str(r.get("gym_id")) != str(account_key):
            return None
        if r.get("status") in ("publishing", "published"):
            return None
        r["caption"] = new_caption
        r["status"] = "pending"
        return dict(r)

    def set_status(self, account_key, row_id, status):
        r = self._rows.get(row_id)
        if not r or str(r.get("gym_id")) != str(account_key):
            return None
        r["status"] = status
        return dict(r)

    def list_month(self, account_key, month):
        return [dict(r) for r in self._rows.values()]


def _wire_store(monkeypatch, store):
    monkeypatch.setattr(ps._pcs, "SupabaseCalendarStore", lambda *a, **k: store)


def test_handle_recreate_caption_flag_off_403s_and_never_reads_the_store(monkeypatch):
    store = _Store([_row("p1")])
    _wire_store(monkeypatch, store)
    status, body = ps.handle_recreate_caption("zanshin", "p1", "u1")
    assert status == 403 and store.caption_patches == []
    assert body["ok"] is False


def test_handle_recreate_caption_persists_via_patch_caption_and_charges_the_budget(
        monkeypatch, lib):
    monkeypatch.setenv("ECHO_CAPTION_RECREATE_SCOPED", "true")
    _wire_source(monkeypatch)
    store = _Store([_row("p1")])
    _wire_store(monkeypatch, store)
    monkeypatch.setattr(ps, "_account_for", lambda key: _acct())
    monkeypatch.setattr(ps, "_voice_for", lambda key, account=None: _voice())
    monkeypatch.setattr(ps, "recreate_remaining", lambda key: 15)
    import agent.client_media_sync as _cms
    monkeypatch.setattr(_cms, "_banned_words_for", lambda key: ())

    fixed = {"ok": True, "caption": "A brand new caption.", "hashtags": ["#z"]}
    monkeypatch.setattr(cs, "recreate_caption", lambda *a, **k: fixed)

    before = ps.recreate_remaining("zanshin")
    status, body = ps.handle_recreate_caption("zanshin", "p1", "u1")
    assert status == 200
    assert body["caption"] == "A brand new caption."
    assert body["free"] is False
    assert store._rows["p1"]["caption"] == "A brand new caption."
    assert store._rows["p1"]["status"] == "pending"
    assert store.caption_patches == [("p1", "A brand new caption.")]


def test_handle_recreate_caption_gate_exhausted_is_a_409_that_changes_nothing(
        monkeypatch):
    monkeypatch.setenv("ECHO_CAPTION_RECREATE_SCOPED", "true")
    store = _Store([_row("p1", caption="unchanged caption")])
    _wire_store(monkeypatch, store)
    monkeypatch.setattr(ps, "_account_for", lambda key: _acct())
    monkeypatch.setattr(ps, "_voice_for", lambda key, account=None: _voice())
    monkeypatch.setattr(ps, "recreate_remaining", lambda key: 15)
    monkeypatch.setattr(cs, "recreate_caption",
                        lambda *a, **k: {"ok": False, "reason": cs.REASON_GATE_EXHAUSTED})

    status, body = ps.handle_recreate_caption("zanshin", "p1", "u1")
    assert status == 409
    assert store.caption_patches == []
    assert store._rows["p1"]["caption"] == "unchanged caption"


def test_handle_recreate_caption_budget_exhausted_is_409(monkeypatch):
    monkeypatch.setenv("ECHO_CAPTION_RECREATE_SCOPED", "true")
    store = _Store([_row("p1")])
    _wire_store(monkeypatch, store)
    monkeypatch.setattr(ps, "recreate_remaining", lambda key: 0)
    status, body = ps.handle_recreate_caption("zanshin", "p1", "u1")
    assert status == 409
    assert body["reason"] == "budget_exhausted"
    assert store.caption_patches == []


def test_a_cross_gym_post_id_is_a_404(monkeypatch):
    monkeypatch.setenv("ECHO_CAPTION_RECREATE_SCOPED", "true")
    store = _Store([_row("p1", gym_id="pierce")])
    _wire_store(monkeypatch, store)
    monkeypatch.setattr(ps, "_account_for", lambda key: _acct())
    monkeypatch.setattr(ps, "_voice_for", lambda key, account=None: _voice())
    monkeypatch.setattr(ps, "recreate_remaining", lambda key: 15)
    status, body = ps.handle_recreate_caption("zanshin", "p1", "u1")
    assert status == 404
    assert store.caption_patches == []


def test_an_approved_post_can_still_be_recreated_and_resets_to_pending(monkeypatch):
    # Matches patch_caption's own contract (used by a plain human caption edit too):
    # rewriting the caption resets approval so the owner re-approves the new words.
    # Only publishing/published are truly final (see _published_is_final).
    monkeypatch.setenv("ECHO_CAPTION_RECREATE_SCOPED", "true")
    store = _Store([_row("p1", status="approved")])
    _wire_store(monkeypatch, store)
    monkeypatch.setattr(ps, "_account_for", lambda key: _acct())
    monkeypatch.setattr(ps, "_voice_for", lambda key, account=None: _voice())
    monkeypatch.setattr(ps, "recreate_remaining", lambda key: 15)
    monkeypatch.setattr(cs, "recreate_caption",
                        lambda *a, **k: {"ok": True, "caption": _NEW_CAPTION,
                                        "hashtags": []})
    status, body = ps.handle_recreate_caption("zanshin", "p1", "u1")
    assert status == 200
    assert store._rows["p1"]["caption"] == _NEW_CAPTION
    assert store._rows["p1"]["status"] == "pending"


def test_a_published_post_keeps_the_caption_the_gym_approved(monkeypatch):
    monkeypatch.setenv("ECHO_CAPTION_RECREATE_SCOPED", "true")
    store = _Store([_row("p1", status="published")])
    _wire_store(monkeypatch, store)
    monkeypatch.setattr(ps, "_account_for", lambda key: _acct())
    monkeypatch.setattr(ps, "_voice_for", lambda key, account=None: _voice())
    monkeypatch.setattr(ps, "recreate_remaining", lambda key: 15)
    monkeypatch.setattr(cs, "recreate_caption",
                        lambda *a, **k: {"ok": True, "caption": "new", "hashtags": []})
    status, body = ps.handle_recreate_caption("zanshin", "p1", "u1")
    assert status == 409
    assert store._rows["p1"]["caption"] == "old caption body"


# ---- the "caption" deny intent -----------------------------------------------

def test_the_caption_needs_work_chip_routes_to_the_scoped_recreate(monkeypatch, lib):
    monkeypatch.setenv("ECHO_CAPTION_RECREATE_SCOPED", "true")
    store = _Store([_row("p1")])
    _wire_store(monkeypatch, store)
    monkeypatch.setattr(ps, "_account_for", lambda key: _acct())
    monkeypatch.setattr(ps, "_voice_for", lambda key, account=None: _voice())
    monkeypatch.setattr(ps, "recreate_remaining", lambda key: 15)
    monkeypatch.setattr(cs, "recreate_caption",
                        lambda *a, **k: {"ok": True, "caption": "scoped caption",
                                        "hashtags": []})

    status, body = ps.handle_deny("zanshin", "p1", "u1", intent="caption",
                                  sb_store=store)
    assert status == 200
    assert body["action"] == "recreate-caption"
    assert store._rows["p1"]["caption"] == "scoped caption"
    assert store._rows["p1"]["status"] == "pending", "must never flip to denied"


def test_flag_off_the_caption_intent_falls_through_to_the_full_deny(monkeypatch):
    store = _Store([_row("p1")])
    _wire_store(monkeypatch, store)
    before = ps.recreate_remaining("zanshin")
    status, body = ps.handle_deny("zanshin", "p1", "u1", intent="caption",
                                  sb_store=store)
    assert status == 200
    assert body["action"] == "deny"
    assert ps.recreate_remaining("zanshin") == before - 1


def test_a_scoped_recreate_that_cannot_ship_falls_through_to_the_full_deny(
        monkeypatch):
    monkeypatch.setenv("ECHO_CAPTION_RECREATE_SCOPED", "true")
    store = _Store([_row("p1")])
    _wire_store(monkeypatch, store)
    monkeypatch.setattr(ps, "_account_for", lambda key: _acct())
    monkeypatch.setattr(ps, "_voice_for", lambda key, account=None: _voice())
    monkeypatch.setattr(ps, "recreate_remaining", lambda key: 15)
    monkeypatch.setattr(cs, "recreate_caption",
                        lambda *a, **k: {"ok": False, "reason": cs.REASON_NO_SOURCE})

    status, body = ps.handle_deny("zanshin", "p1", "u1", intent="caption",
                                  sb_store=store)
    assert status == 200
    assert body["action"] == "deny", "no clean scoped result -> the full recreate still runs"
    assert store._rows["p1"]["status"] == "denied"


# ---- P-11: the exact contract the PORTAL relays against ---------------------

def test_the_portal_relay_path_for_recreate_caption_is_routable():
    import re

    from agent import intake_web

    actions = set(intake_web.PORTAL_POST_ACTIONS)
    assert "recreate-caption" in actions

    pattern = (r"^/portal/([A-Za-z0-9_.-]{8,})/posts/([A-Za-z0-9_-]+)/"
               r"(" + "|".join(intake_web.PORTAL_POST_ACTIONS) + r")$")
    m = re.match(pattern,
                "/portal/eyJhIjoiZW5nIn0.sig/posts/abc-123/recreate-caption")
    assert m and m.group(3) == "recreate-caption"


def test_client_messages_carry_no_dashes_and_never_say_vendor():
    for reason in (cs.REASON_NO_LIBRARY, cs.REASON_NO_PHOTO, cs.REASON_NO_SOURCE,
                   cs.REASON_GATE_EXHAUSTED):
        msg = cs.client_message(reason)
        assert "—" not in msg and "–" not in msg and "-" not in msg
        assert "vendor" not in msg.lower()
