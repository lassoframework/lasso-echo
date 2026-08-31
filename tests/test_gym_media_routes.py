"""gym_media_drive §8 + §1.5: check-connection cases, HIJACK refuse + alert,
ownership conflict, disconnect never deletes, hide flips pending, thumbnail refuses
cross-gym."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gym_media_routes as gm  # noqa: E402
from agent.integrations import drive_client as dc  # noqa: E402
from tests.gym_media_fakes import (FakeMediaStore, FakeDrive, make_asset,  # noqa: E402
                                   make_source)

FID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


@pytest.fixture(autouse=True)
def _arm(monkeypatch):
    # Arm the lane for every gym in these tests.
    monkeypatch.setattr("agent.config.gym_drive_connect_active_for", lambda g: True)
    # No pending-row flips over the wire in tests. Returns the real (pulled, err)
    # shape so a stub can never mask the route's honest-failure branch.
    monkeypatch.setattr("agent.gym_media_routes._flip_pending_using_asset",
                        lambda g, a: (0, None))


def _drive(meta=None, children=None):
    return FakeDrive(meta=meta or {}, files=children or [])


# ---- check-connection --------------------------------------------------------
def test_check_connection_my_drive_confirm():
    from tests.gym_media_fakes import photo
    drive = FakeDrive(meta={"name": "Team Photos", "owner_email": "o@pierce.com",
                            "case": "my_drive"},
                      files=[photo("p1", parent=FID), photo("p2", parent=FID)])
    store = FakeMediaStore()
    status, body = gm.handle_check_connection("pierce", f".../folders/{FID}",
                                              drive=drive, store=store)
    assert status == 200 and body["ok"] is True
    assert body["folder_name"] == "Team Photos"
    assert body["case"] == "my_drive"
    assert body["photos"] == 2


def test_check_connection_not_shared():
    drive = FakeDrive(meta={"case": "not_shared"})
    status, body = gm.handle_check_connection("pierce", f".../folders/{FID}",
                                              drive=drive, store=FakeMediaStore())
    assert body["ok"] is False and body["case"] == "not_shared"


def test_check_connection_bad_link():
    status, body = gm.handle_check_connection("pierce", "banana",
                                              drive=_drive(), store=FakeMediaStore())
    assert body["ok"] is False and body["case"] == "bad_link"


def test_check_connection_lane_off(monkeypatch):
    monkeypatch.setattr("agent.config.gym_drive_connect_active_for", lambda g: False)
    status, body = gm.handle_check_connection("pierce", f".../folders/{FID}",
                                              drive=_drive(), store=FakeMediaStore())
    assert status == 403


# ---- bind: HIJACK ------------------------------------------------------------
def test_bind_hijack_refused_and_alerts(monkeypatch):
    """A folder already bound to gym A: gym B's bind is HARD-refused + one ops alert
    names both gyms."""
    fired = []
    monkeypatch.setattr("agent.gym_media_index.dedup_alert",
                        lambda k, m: fired.append((k, m)) or True)
    store = FakeMediaStore(sources=[make_source("srcA", gym_id="gyma",
                                                folder_id=FID)])
    drive = FakeDrive(meta={"name": "x", "owner_email": "o@gyma.com",
                            "case": "my_drive"})
    status, body = gm.handle_bind_source("gymb", f".../folders/{FID}",
                                         drive=drive, store=store)
    assert status == 409 and body["ok"] is False and body["case"] == "already_bound"
    assert fired and "gyma" in fired[0][1] and "gymb" in fired[0][1]
    # No new source row was written.
    assert len(store.sources) == 1


def test_bind_same_gym_same_folder_idempotent():
    store = FakeMediaStore(sources=[make_source("srcA", gym_id="pierce",
                                               folder_id=FID)])
    drive = FakeDrive(meta={"name": "x", "case": "my_drive"})
    status, body = gm.handle_bind_source("pierce", f".../folders/{FID}",
                                         drive=drive, store=store)
    assert status == 200 and body["ok"] is True and body.get("already") is True


def test_bind_ownership_conflict_blocked(monkeypatch):
    """A folder owned by a domain that matches a DIFFERENT connected gym is blocked
    + alerted (likely wrong-gym folder)."""
    fired = []
    monkeypatch.setattr("agent.gym_media_index.dedup_alert",
                        lambda k, m: fired.append((k, m)) or True)
    # gym A is already connected with a business-domain owner.
    store = FakeMediaStore(sources=[make_source("srcA", gym_id="gyma",
                                               folder_id="otherfold")])
    store.sources["srcA"]["owner_email"] = "coach@acmegym.com"
    drive = FakeDrive(meta={"name": "x", "owner_email": "front@acmegym.com",
                            "case": "my_drive"})
    status, body = gm.handle_bind_source("gymb", f".../folders/{FID}",
                                         drive=drive, store=store)
    assert status == 409 and body["case"] == "owner_conflict"
    assert fired


def test_bind_personal_domain_does_not_conflict():
    """Two gyms both using gmail must NOT trip the ownership rail."""
    store = FakeMediaStore(sources=[make_source("srcA", gym_id="gyma",
                                               folder_id="otherfold")])
    store.sources["srcA"]["owner_email"] = "someone@gmail.com"
    drive = FakeDrive(meta={"name": "x", "owner_email": "coach@gmail.com",
                            "case": "my_drive"})
    status, body = gm.handle_bind_source("gymb", f".../folders/{FID}",
                                         drive=drive, store=store)
    assert status == 200 and body["ok"] is True


def test_bind_success_writes_source():
    store = FakeMediaStore()
    drive = FakeDrive(meta={"name": "Team Photos", "owner_email": "o@pierce.com",
                            "case": "my_drive"})
    status, body = gm.handle_bind_source("pierce", f".../folders/{FID}",
                                         actor_id="u1", drive=drive, store=store)
    assert status == 200 and body["ok"] is True
    src = list(store.sources.values())[0]
    assert src["gym_id"] == "pierce" and src["folder_id"] == FID
    assert src["active"] is True


# ---- disconnect never deletes ------------------------------------------------
def test_disconnect_marks_inactive_never_deletes():
    store = FakeMediaStore(
        sources=[make_source("src1", gym_id="pierce", folder_id=FID)],
        assets=[make_asset("a1", gym_id="pierce", source_id="src1")])
    status, body = gm.handle_disconnect_source("pierce", "src1", store=store)
    assert status == 200 and body["ok"] is True
    assert "src1" in store.sources                     # row still exists
    assert store.sources["src1"]["active"] is False
    assert store.assets["a1"]["excluded_by_coach"] is True


def test_disconnect_cross_gym_404s():
    store = FakeMediaStore(sources=[make_source("src1", gym_id="pierce",
                                               folder_id=FID)])
    status, body = gm.handle_disconnect_source("otherg", "src1", store=store)
    assert status == 404


# ---- hide / unhide -----------------------------------------------------------
def test_hide_flips_pending_and_rolls_back(monkeypatch):
    flipped = []
    monkeypatch.setattr("agent.gym_media_routes._flip_pending_using_asset",
                        lambda g, a: (flipped.append((g, a)), (1, None))[1])
    rolled = []
    monkeypatch.setattr("agent.gym_media_selector.rollback_asset",
                        lambda aid, store=None: rolled.append(aid) or True)
    store = FakeMediaStore(assets=[make_asset("a1", gym_id="pierce")])
    status, body = gm.handle_hide_asset("pierce", "a1", hide=True, store=store)
    assert status == 200 and store.assets["a1"]["excluded_by_coach"] is True
    assert body.get("pulled") == 1, "the client is told how many scheduled posts were pulled"
    assert flipped == [("pierce", "a1")]
    assert rolled == ["a1"]


def test_unhide_clears_flag():
    store = FakeMediaStore(assets=[make_asset("a1", gym_id="pierce",
                                             excluded_by_coach=True)])
    status, body = gm.handle_hide_asset("pierce", "a1", hide=False, store=store)
    assert status == 200 and store.assets["a1"]["excluded_by_coach"] is False


def test_hide_cross_gym_404s():
    store = FakeMediaStore(assets=[make_asset("a1", gym_id="pierce")])
    status, body = gm.handle_hide_asset("otherg", "a1", hide=True, store=store)
    assert status == 404


# ---- thumbnail proxy tenant isolation ----------------------------------------
def test_thumbnail_refuses_cross_gym():
    store = FakeMediaStore(assets=[make_asset("a1", gym_id="pierce")])
    drive = FakeDrive(files=[])
    status, ctype, data = gm.handle_thumbnail("otherg", "a1", store=store,
                                              drive=drive)
    assert status == 404


def test_thumbnail_serves_own_gym():
    store = FakeMediaStore(assets=[make_asset("a1", gym_id="pierce",
                                             mime="image/jpeg")])
    drive = FakeDrive(files=[])   # download writes fake bytes
    status, ctype, data = gm.handle_thumbnail("pierce", "a1", store=store,
                                              drive=drive)
    assert status == 200 and ctype == "image/jpeg" and data


# ---- audit #7: prefer Drive's real thumbnail (small, correctly-typed image) -------
def test_thumbnail_prefers_drive_thumbnail():
    store = FakeMediaStore(assets=[make_asset("a1", gym_id="pierce",
                                             mime="video/mp4")])
    # A video asset: the FULL asset would be a huge mislabeled stream. With a real
    # Drive thumbnail seeded, the proxy serves THAT small image/jpeg instead.
    drive = FakeDrive(files=[], thumbs={"a1": b"SMALLJPEGTHUMB"})
    status, ctype, data = gm.handle_thumbnail("pierce", "a1", store=store,
                                              drive=drive)
    assert status == 200
    assert ctype == "image/jpeg"                     # correctly typed, not video/mp4
    assert data == b"SMALLJPEGTHUMB"                 # the small rendition, not the full file
    assert "a1" not in drive.downloads               # never streamed the full original


def test_thumbnail_fallback_labels_with_asset_mime():
    # Drive made no thumbnail (thumbnail() returns None): fall back to streaming the
    # original, but labeled with the asset's OWN mime, never a mislabel.
    store = FakeMediaStore(assets=[make_asset("a1", gym_id="pierce",
                                             mime="image/png")])
    drive = FakeDrive(files=[], thumbs={})           # no thumbnail available
    status, ctype, data = gm.handle_thumbnail("pierce", "a1", store=store,
                                              drive=drive)
    assert status == 200 and ctype == "image/png" and data
    assert "a1" in drive.downloads                   # fell back to the original stream


# ---- stale fingerprint resolution (CrossFit Reverb class, live 2026-08-31) ---
def test_name_slug_of_extracts_fingerprinted_shape():
    assert gm._name_slug_of("crossfitreverb6cdf33") == "crossfitreverb"
    assert gm._name_slug_of("crossfitreverb30b5b2") == "crossfitreverb"


def test_name_slug_of_rejects_bare_keys():
    # No 6-hex tail: never treated as fingerprinted, never guessed at.
    assert gm._name_slug_of("pierce") == ""
    assert gm._name_slug_of("piercefitness") == ""
    assert gm._name_slug_of("theboltonclub") == ""
    assert gm._name_slug_of("") == ""
    assert gm._name_slug_of(None) == ""


def test_resolve_stale_fingerprint_remaps_to_the_one_registered_match():
    bases = ["crossfitreverb30b5b2", "piercefitness", "eng"]
    resolved = gm._resolve_stale_fingerprint(
        "crossfitreverb6cdf33", bases_fn=lambda: bases)
    assert resolved == "crossfitreverb30b5b2"


def test_resolve_stale_fingerprint_alerts_once(monkeypatch):
    fired = []
    monkeypatch.setattr("agent.gym_media_index.dedup_alert",
                        lambda k, m: fired.append((k, m)) or True)
    bases = ["crossfitreverb30b5b2"]
    gm._resolve_stale_fingerprint("crossfitreverb6cdf33", bases_fn=lambda: bases)
    assert fired
    assert "crossfitreverb6cdf33" in fired[0][1]
    assert "crossfitreverb30b5b2" in fired[0][1]


def test_resolve_stale_fingerprint_already_registered_is_untouched():
    bases = ["crossfitreverb30b5b2"]
    resolved = gm._resolve_stale_fingerprint(
        "crossfitreverb30b5b2", bases_fn=lambda: bases)
    assert resolved == "crossfitreverb30b5b2"


def test_resolve_stale_fingerprint_never_guesses_on_ambiguity():
    # Two registered gyms happen to share a name-slug (only the fingerprint tells
    # them apart) -- refuse to guess, leave the stray key exactly as it arrived.
    bases = ["crossfitreverb30b5b2", "crossfitreverbaaaaaa"]
    resolved = gm._resolve_stale_fingerprint(
        "crossfitreverb6cdf33", bases_fn=lambda: bases)
    assert resolved == "crossfitreverb6cdf33"


def test_resolve_stale_fingerprint_no_match_is_untouched():
    bases = ["piercefitness", "eng"]
    resolved = gm._resolve_stale_fingerprint(
        "crossfitreverb6cdf33", bases_fn=lambda: bases)
    assert resolved == "crossfitreverb6cdf33"


def test_resolve_stale_fingerprint_bare_key_never_matched():
    # A key with no 6-hex tail is never treated as a stale fingerprint, even if some
    # registered base happens to share letters with it.
    bases = ["pierce2222bb"]
    resolved = gm._resolve_stale_fingerprint("pierce", bases_fn=lambda: bases)
    assert resolved == "pierce"


def test_resolve_stale_fingerprint_registry_failure_degrades_safely():
    def _boom():
        raise RuntimeError("registry unavailable")
    resolved = gm._resolve_stale_fingerprint("crossfitreverb6cdf33", bases_fn=_boom)
    assert resolved == "crossfitreverb6cdf33"


def test_bind_remaps_stale_key_and_writes_under_the_live_gym(monkeypatch):
    """The exact CrossFit Reverb scenario: a bind request arrives on a stale
    fingerprint; the row is written under the CURRENT registered key instead."""
    monkeypatch.setattr(
        "agent.calendar_autopublish.client_gym_bases",
        lambda: ["crossfitreverb30b5b2"])
    store = FakeMediaStore()
    drive = FakeDrive(meta={"name": "Reverb LASSO Content",
                            "owner_email": "deanholcomb6@gmail.com",
                            "case": "my_drive"})
    status, body = gm.handle_bind_source(
        "crossfitreverb6cdf33", f".../folders/{FID}", drive=drive, store=store)
    assert status == 200 and body["ok"] is True
    src = list(store.sources.values())[0]
    assert src["gym_id"] == "crossfitreverb30b5b2"
