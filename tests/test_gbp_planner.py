"""
GBP planner (agent/gbp_planner.py): offer resolver, and plan_gbp_month cadence +
row shape. Offline: caption_fn + image_fn injected (no LLM, no real images), sources
seeded in the sqlite client_sources, a fake store captures inserted rows.
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gbp_planner as gp, client_sources as cs, visual_owner_receipts as owner  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, make_asset  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    yield


class _Store:
    def __init__(self):
        self.rows = []

    def insert_rows(self, key, rows):
        self.rows.extend(rows)
        return rows


def _voice():
    return VoiceDoc(raw="We coach busy Carmel parents back to strong.", hashtags=[],
                   ctas=["Book a free intro."])


def _seed(acct="lasso_ig"):
    cs.add_source(acct, "service", "Small group strength coaching for busy parents", "intake")
    cs.add_source(acct, "about", "Coaching the Carmel community for years", "intake")
    cs.add_source(acct, "faq", "New here? Your first session is a easy on-ramp", "intake")


def _cap(fact):
    # a valid GBP caption naming the city, no dashes/hashtags/phone
    return ("Carmel parents: real strength on a schedule that fits your life, coached "
            "step by step so you actually stick with it and feel the difference.")


def _img(day_key, used):
    used.add(day_key)
    return f"https://r2/gbp/{day_key}.jpg"


# ---- offer resolver --------------------------------------------------------

def test_resolve_offer_from_jsonb_array_and_link():
    name, d = gp.resolve_offer(["12 Week Strength", "Free Trial"], "https://ghl/join")
    assert name == "12 Week Strength"
    assert d == {"redeemOnlineUrl": "https://ghl/join"}


def test_resolve_offer_skips_without_url_or_name():
    assert gp.resolve_offer(["12 Week Strength"], "") == (None, None)
    assert gp.resolve_offer([], "https://ghl/join") == (None, None)
    assert gp.resolve_offer(None, None) == (None, None)


def test_resolve_offer_dict_element_uses_name_not_stringified_dict():
    # a jsonb offer element that is an object must yield its name, never "{'name': ...}"
    name, d = gp.resolve_offer([{"name": "Free Trial", "id": 7}], "https://ghl/join")
    assert name == "Free Trial" and d == {"redeemOnlineUrl": "https://ghl/join"}
    # an object with no name-ish field -> skip (never fabricate a name from the dict repr)
    assert gp.resolve_offer([{"id": 7}], "https://ghl/join") == (None, None)


def test_plan_reports_failure_when_store_cannot_persist():
    _seed()

    class _NoPersist:
        pass  # no insert_rows

    out = gp.plan_gbp_month("lasso", "lasso_ig", voice=_voice(), library_path="/x",
                            city="Carmel", store=_NoPersist(), start=date(2026, 9, 1),
                            offer=None, events=[], caption_fn=_cap, image_fn=_img)
    assert out["ok"] is False and out["planned"] == 0   # never a phantom success


# ---- cadence + row shape ---------------------------------------------------

def test_full_cadence_with_offer_and_event():
    _seed()
    store = _Store()
    out = gp.plan_gbp_month(
        "lasso", "lasso_ig", voice=_voice(), library_path="/nope", city="Carmel",
        store=store, start=date(2026, 9, 1), days=30, cta_url="https://gym.com/start",
        offer=("12 Week Strength", {"redeemOnlineUrl": "https://ghl/join"}),
        offer_confirmed=True,     # GATE 1: OFFER only when the live offer is confirmed
        events=[{"title": "Open House", "fact": "Open house this month in Carmel",
                 "schedule": {"startDate": "2026-09-20", "endDate": "2026-09-20"}}],
        caption_fn=_cap, image_fn=_img)
    assert out["ok"]
    assert out["standard"] == 8 and out["offer"] == 1 and out["event"] == 1
    assert out["photo"] == 4
    # every row is a pending googlebusiness row keyed to the portal gym
    for r in store.rows:
        assert r["account"] == "googlebusiness" and r["status"] == "pending"
        assert r["gym_id"] == "lasso"
    # OFFER row: NO cta fields, carries offer + window
    offer_rows = [r for r in store.rows if r["gbp_topic_type"] == "OFFER"]
    assert len(offer_rows) == 1
    o = offer_rows[0]
    assert "gbp_cta_type" not in o and "gbp_cta_url" not in o
    assert o["gbp_offer"]["redeemOnlineUrl"] == "https://ghl/join"
    # the offer window opens on the offer's own post day and runs OFFER_WINDOW_DAYS
    assert o["gbp_event"]["schedule"]["startDate"] == o["post_date"]
    from datetime import date as _d, timedelta as _td
    _s = _d.fromisoformat(o["gbp_event"]["schedule"]["startDate"])
    _e = _d.fromisoformat(o["gbp_event"]["schedule"]["endDate"])
    assert (_e - _s).days == gp.OFFER_WINDOW_DAYS <= 30
    # STANDARD rows carry the CTA
    std = [r for r in store.rows if r["gbp_topic_type"] == "STANDARD" and r["format"] == "update"]
    assert len(std) == 8 and all(r["gbp_cta_type"] == "LEARN_MORE" for r in std)
    # photo drops: format photo, no caption gate applied
    photos = [r for r in store.rows if r["format"] == "photo"]
    assert len(photos) == 4 and all(r["caption"] == "" for r in photos)


def test_injected_facts_drive_standard_without_client_sources():
    # a tenant with NO client_sources (present==[]) still plans a full STANDARD run from an
    # injected real fact list (e.g. LASSO's lasso_now.md copy bank). Gates still apply.
    store = _Store()
    facts = [("All in one offer", "Ads, nurture, site, social, reporting in one place."),
             ("Sales are now", "The job is closing members, not building funnels."),
             ("Proof", "71.9% booked vs an 18.5% industry average.")]
    out = gp.plan_gbp_month(
        "lasso", "lasso_ig", voice=_voice(), library_path="/x", city="Carmel",
        store=store, start=date(2026, 9, 1), offer=None, events=[],
        facts=facts, caption_fn=_cap, image_fn=_img)
    assert out["ok"] and out["standard"] == 8      # cycled facts fill all 8 slots
    # the injected pillar names ride onto the rows
    pillars = {r["pillar"] for r in store.rows if r["gbp_topic_type"] == "STANDARD"
               and r["format"] == "update"}
    assert pillars <= {"All in one offer", "Sales are now", "Proof"}


def test_client_gbp_uses_drive_photo_before_local_library(monkeypatch):
    _seed("gymx_ig")
    store = _Store()
    calls = []
    monkeypatch.setattr(gp, "_drive_photo_candidate",
                        lambda account, day, used: calls.append((account, day))
                        or {"url": f"https://r2/drive/{day}.jpg",
                            "kind": "injected", "day_key": day})
    monkeypatch.setattr(gp.client_content, "pick_image",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("local picker must not outrank Drive")))
    out = gp.plan_gbp_month(
        "gymx", "gymx_ig", voice=_voice(), library_path="/x", city="Carmel",
        store=store, start=date(2026, 9, 1), offer=None, events=[],
        caption_fn=_cap)
    assert out["ok"] and calls
    assert all("/drive/" in row["image_url"] for row in store.rows)


def test_client_gbp_local_fallback_explicitly_prefers_photos(monkeypatch):
    from types import SimpleNamespace
    _seed("gymx_ig")
    store = _Store()
    seen = []
    monkeypatch.setattr(gp, "_drive_photo_candidate", lambda *a, **k: None)

    def _pick(*args, **kwargs):
        seen.append(kwargs.get("prefer_photos"))
        return SimpleNamespace(path=f"/tmp/photo-{len(seen)}.jpg", media_type="image")

    monkeypatch.setattr(gp.client_content, "pick_image", _pick)
    monkeypatch.setattr(gp, "_cropped_image_url",
                        lambda account, image, day: f"https://r2/local/{day}.jpg")
    out = gp.plan_gbp_month(
        "gymx", "gymx_ig", voice=_voice(), library_path="/x", city="Carmel",
        store=store, start=date(2026, 9, 1), offer=None, events=[],
        caption_fn=_cap)
    assert out["ok"] and seen
    assert all(value is True for value in seen)


def test_drive_photo_is_cropped_then_stamped_once(monkeypatch):
    from agent import config, gym_media_index, gym_media_selector
    from agent.integrations import drive_client

    asset = {"id": "drive-photo-1", "gym_id": "gymx", "kind": "photo",
             "title": "class.jpg"}
    stamps = []

    class Store:
        def available(self):
            return True

        def list_sources(self, _base):
            return [{"kind": "gym_drive", "active": True,
                     "revoked_externally": False, "sync_status": "ready",
                     "sync_finished_at": "2026-10-03T00:00:00Z"}]

        def list_assets(self, _base):
            return [asset]

    class Drive:
        def available(self):
            return True

        def download(self, _asset_id, path):
            path.write_bytes(b"photo bytes")

    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: True)
    monkeypatch.setattr(config, "gym_drive_connect_active_for", lambda _key: True)
    monkeypatch.setattr(gym_media_index, "default_store", lambda: Store())
    monkeypatch.setattr(gym_media_index, "needs_rendition", lambda _asset: False)
    monkeypatch.setattr(drive_client, "DriveClient", lambda: Drive())
    monkeypatch.setattr(gym_media_selector, "pick_media",
                        lambda gym, kind_preference, **kwargs: asset
                        if kind_preference == "photo" else None)
    monkeypatch.setattr(gym_media_selector, "stamp_use",
                        lambda selected, gym, day, **kwargs:
                        stamps.append((selected["id"], gym, day)))

    def _crop(account, image, day):
        assert os.path.isfile(image.path)
        return f"https://r2/{account}/{day}.jpg"

    monkeypatch.setattr(gp, "_cropped_image_url", _crop)
    used = set()
    pick = gp._drive_photo_candidate("gymx_ig", "2026-10-03", used)
    assert pick["url"] == "https://r2/gymx_ig/2026-10-03.jpg"
    assert used == {"drive-photo-1"}
    assert stamps == [], "candidate materialization must not burn the asset"


def test_active_drive_uncertainty_holds_local_gbp_fallback(monkeypatch):
    from agent import config, gym_media_index
    from agent.integrations import drive_client

    class Store:
        def available(self):
            return False

    class Drive:
        def available(self):
            return True

    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: True)
    monkeypatch.setattr(config, "gym_drive_connect_active_for", lambda _key: True)
    monkeypatch.setattr(gym_media_index, "default_store", lambda: Store())
    monkeypatch.setattr(drive_client, "DriveClient", lambda: Drive())
    assert gp._drive_photo_candidate("gymx_ig", "2026-10-03", set()) == {"hold": True}


def test_drive_claim_is_single_winner_and_releasable():
    asset = make_asset("drive-1", gym_id="gymx")
    media_store = FakeMediaStore(assets=[asset])
    first = {"base": "gymx", "asset": asset, "store": media_store}
    second = {"base": "gymx", "asset": asset, "store": media_store}
    assert gp._claim_drive_pick(first, "gymx_gbp") is True
    assert gp._claim_drive_pick(second, "gymx_gbp") is False
    assert gp._release_drive_claim(first) is True
    assert gp._claim_drive_pick(second, "gymx_gbp") is True


def test_concurrent_same_byte_aliases_share_one_atomic_claim():
    first_asset = make_asset("alias-a", gym_id="gymx", content_hash="a" * 64)
    second_asset = make_asset("alias-b", gym_id="gymx", content_hash="a" * 64)
    media_store = FakeMediaStore(assets=[first_asset, second_asset])
    barrier = Barrier(2)

    def claim(asset):
        pick = {"base": "gymx", "asset": asset, "store": media_store}
        barrier.wait()
        return gp._claim_drive_pick(pick, "gymx_gbp"), pick

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (first_asset, second_asset)))
    assert sum(won for won, _ in results) == 1
    assert {pick["claim_id"] for won, pick in results if won} == {
        "gbp_media:gymx:hash:" + "a" * 64}


def test_gbp_batch_skips_same_byte_alias_before_claims(monkeypatch):
    _seed("gymx_ig")
    first = make_asset("alias-a", gym_id="gymx", content_hash="a" * 64)
    alias = make_asset("alias-b", gym_id="gymx", content_hash="a" * 64)
    other = make_asset("other", gym_id="gymx", content_hash="b" * 64)
    media_store = FakeMediaStore(assets=[first, alias, other])

    def candidate(_account, day, used):
        asset = next((a for a in (first, alias, other)
                      if a["id"] not in used), None)
        if asset is None:
            return None
        return {"url": f"https://r2/{asset['id']}.jpg", "kind": "drive",
                "day_key": day, "asset": asset, "base": "gymx",
                "store": media_store}

    monkeypatch.setattr(gp, "_drive_photo_candidate", candidate)
    monkeypatch.setattr(gp.gym_media_selector, "stamp_use", lambda *a, **k: None)
    store = _Store()
    out = gp.plan_gbp_month(
        "gymx", "gymx_ig", voice=_voice(), library_path="/x", city="Carmel",
        store=store, start=date(2026, 9, 1), days=12,
        offer=None, events=[], caption_fn=_cap)
    assert out["ok"] is True
    urls = {row["image_url"] for row in store.rows}
    assert "https://r2/alias-a.jpg" in urls
    assert "https://r2/other.jpg" in urls
    assert "https://r2/alias-b.jpg" not in urls


def test_insert_readback_requires_exact_count_for_absence():
    row = {"post_date": "2026-09-01", "format": "update",
           "image_url": "https://r2/photo.jpg", "account": "googlebusiness"}

    class Response:
        status_code = 200
        def __init__(self, content_range):
            self.headers = {"content-range": content_range}
        def json(self):
            return []

    class Client:
        response = Response("*/0")
        def get(self, *args, **kwargs):
            assert kwargs["headers"]["Prefer"] == "count=exact"
            return self.response

    client = Client()
    class Base:
        def _client(self):
            return client
        def _rest(self, table):
            assert table == "content_calendar"
            return "/content_calendar"
        def _headers(self, extra=None):
            return dict(extra or {})

    store = type("Store", (), {"_s": Base()})()
    assert gp._readback_inserted_rows(store, "gymx", [row]) == []
    client.response = Response("")
    assert gp._readback_inserted_rows(store, "gymx", [row]) is None


def test_drive_stamp_happens_only_after_caption_and_durable_insert(monkeypatch):
    from agent import gym_media_selector
    _seed("gymx_ig")
    events = []
    asset = make_asset("drive-1", gym_id="gymx")

    class MediaStore:
        def list_assets(self, _base):
            return [asset]

    class Store(_Store):
        def insert_rows(self, key, rows):
            events.append("insert")
            return super().insert_rows(key, rows)

    monkeypatch.setattr(
        gp, "_drive_photo_candidate",
        lambda account, day, used: {"url": f"https://r2/{day}.jpg",
                                    "kind": "drive", "day_key": day,
                                    "asset": asset, "base": "gymx",
                                    "store": MediaStore()})
    monkeypatch.setattr(gym_media_selector, "stamp_use",
                        lambda *a, **k: events.append("stamp"))
    out = gp.plan_gbp_month(
        "gymx", "gymx_ig", voice=_voice(), library_path="/x", city="Carmel",
        store=Store(), start=date(2026, 9, 1), days=1,
        offer=None, events=[], caption_fn=_cap)
    assert out["ok"] is True
    assert events == ["insert", "stamp"]


def test_caption_rejection_and_failed_insert_do_not_burn_drive_asset(monkeypatch):
    from agent import gym_media_selector
    _seed("gymx_ig")
    stamps = []
    asset = make_asset("drive-1", gym_id="gymx")
    candidate = {"url": "https://r2/drive.jpg", "kind": "drive",
                 "day_key": "2026-09-01", "asset": asset,
                 "base": "gymx", "store": FakeMediaStore(assets=[asset])}
    monkeypatch.setattr(gp, "_drive_photo_candidate",
                        lambda *a, **k: dict(candidate))
    monkeypatch.setattr(gym_media_selector, "stamp_use",
                        lambda *a, **k: stamps.append(True))
    rejected = gp.plan_gbp_month(
        "gymx", "gymx_ig", voice=_voice(), library_path="/x", city="Carmel",
        store=_Store(), start=date(2026, 9, 1), days=1,
        offer=None, events=[], caption_fn=lambda _fact: None)
    assert rejected["ok"] is False and stamps == []

    class FailedStore:
        def insert_rows(self, _key, _rows):
            raise OSError("write failed")

    with pytest.raises(OSError, match="write failed"):
        gp.plan_gbp_month(
            "gymx", "gymx_ig", voice=_voice(), library_path="/x", city="Carmel",
            store=FailedStore(), start=date(2026, 9, 1), days=1,
            offer=None, events=[], caption_fn=_cap)
    assert stamps == []


def test_drive_stamp_retries_once_after_a_prewrite_failure(monkeypatch):
    from agent import gym_media_selector
    _seed("gymx_ig")
    asset = make_asset("drive-1", gym_id="gymx")

    class MediaStore:
        def list_assets(self, _base):
            return [asset]

    monkeypatch.setattr(
        gp, "_drive_photo_candidate",
        lambda account, day, used: {"url": f"https://r2/{day}.jpg",
                                    "kind": "drive", "day_key": day,
                                    "asset": asset, "base": "gymx",
                                    "store": MediaStore()})
    attempts = []

    def _stamp(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("transient before write")

    monkeypatch.setattr(gym_media_selector, "stamp_use", _stamp)
    out = gp.plan_gbp_month(
        "gymx", "gymx_ig", voice=_voice(), library_path="/x", city="Carmel",
        store=_Store(), start=date(2026, 9, 1), days=1,
        offer=None, events=[], caption_fn=_cap)
    assert out["ok"] is True
    assert len(attempts) == 2


def test_after_insert_stamp_failure_keeps_atomic_claim_in_flight(monkeypatch):
    from agent import db, gym_media_selector
    _seed("gymx_ig")
    asset = make_asset("drive-1", gym_id="gymx")

    class MediaStore:
        def list_assets(self, _base):
            return [asset]

    monkeypatch.setattr(
        gp, "_drive_photo_candidate",
        lambda account, day, used: {"url": f"https://r2/{day}.jpg",
                                    "kind": "drive", "day_key": day,
                                    "asset": asset, "base": "gymx",
                                    "store": MediaStore()})
    monkeypatch.setattr(gym_media_selector, "stamp_use",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("stamp down")))
    out = gp.plan_gbp_month(
        "gymx", "gymx_ig", voice=_voice(), library_path="/x", city="Carmel",
        store=_Store(), start=date(2026, 9, 1), days=1,
        offer=None, events=[], caption_fn=_cap)
    assert out["ok"] is False and out["planned"] == 1
    assert out["operational_hold"] is True and out["claim_ids"]
    state, _ = db.socialapi_claim(
        gp._drive_claim_id("gymx", asset), "gymx_gbp")
    assert state == "in_flight", "durable row must leave a non-reofferable claim"


def test_ambiguous_insert_exception_retains_drive_claim(monkeypatch):
    from agent import db, gym_media_selector
    _seed("gymx_ig")
    asset = make_asset("drive-1", gym_id="gymx")
    monkeypatch.setattr(
        gp, "_drive_photo_candidate",
        lambda account, day, used: {"url": f"https://r2/{day}.jpg",
                                    "kind": "drive", "day_key": day,
                                    "asset": asset, "base": "gymx",
                                    "store": FakeMediaStore(assets=[asset])})
    monkeypatch.setattr(gym_media_selector, "stamp_use",
                        lambda *a, **k: pytest.fail("ambiguous insert must not stamp"))

    class AmbiguousStore:
        def insert_rows(self, _key, _rows):
            raise TimeoutError("response lost after request")
        def list_month(self, _key, _month):
            return []  # filtered/non-counted reads are never absence proof

    with pytest.raises(TimeoutError, match="response lost"):
        gp.plan_gbp_month(
            "gymx", "gymx_ig", voice=_voice(), library_path="/x", city="Carmel",
            store=AmbiguousStore(), start=date(2026, 9, 1), days=1,
            offer=None, events=[], caption_fn=_cap)
    state, _ = db.socialapi_claim(
        gp._drive_claim_id("gymx", asset), "gymx_gbp")
    assert state == "in_flight"


def test_insert_exception_readback_stamps_visible_row_and_completes_claim(monkeypatch):
    from agent import db, gym_media_selector
    _seed("gymx_ig")
    asset = make_asset("drive-1", gym_id="gymx")
    stamps = []
    monkeypatch.setattr(
        gp, "_drive_photo_candidate",
        lambda account, day, used: {"url": f"https://r2/{day}.jpg",
                                    "kind": "drive", "day_key": day,
                                    "asset": asset, "base": "gymx",
                                    "store": FakeMediaStore(assets=[asset])})
    monkeypatch.setattr(gym_media_selector, "stamp_use",
                        lambda *a, **k: stamps.append(True))

    class LostResponseStore:
        proposed = []
        def insert_rows(self, _key, rows):
            self.proposed = list(rows)
            raise TimeoutError("response lost after commit")
        def authoritative_rows_for_keys(self, _key, _proposed):
            return self.proposed

    out = gp.plan_gbp_month(
        "gymx", "gymx_ig", voice=_voice(), library_path="/x", city="Carmel",
        store=LostResponseStore(), start=date(2026, 9, 1), days=1,
        offer=None, events=[], caption_fn=_cap)
    assert out["ok"] is True and out["planned"] == 1
    assert stamps == [True]
    state, _ = db.socialapi_claim(
        gp._drive_claim_id("gymx", asset), "gymx_gbp")
    assert state == "done"


def test_local_gbp_photo_is_once_used_across_planner_runs(monkeypatch, tmp_path):
    from agent import dam, rotation
    _seed("gymx_ig")
    photo = tmp_path / "class.jpg"
    photo.write_bytes(b"photo")
    monkeypatch.setattr(gp, "_drive_photo_candidate", lambda *a, **k: None)
    monkeypatch.setattr(gp, "_cropped_image_url",
                        lambda account, image, day: f"https://r2/{day}.jpg")
    first = gp.plan_gbp_month(
        "gymx", "gymx_ig", voice=_voice(), library_path=str(tmp_path), city="Carmel",
        store=_Store(), start=date(2026, 9, 1), days=1,
        offer=None, events=[], caption_fn=_cap)
    assert first["ok"] is True
    assert rotation.local_photo_served(
        dam.rotation_key(str(photo)), "gymx_ig", "2026-09-01") is True
    second_store = _Store()
    second = gp.plan_gbp_month(
        "gymx", "gymx_ig", voice=_voice(), library_path=str(tmp_path), city="Carmel",
        store=second_store, start=date(2026, 10, 1), days=1,
        offer=None, events=[], caption_fn=_cap)
    assert second["ok"] is False and second_store.rows == []


def test_failed_local_insert_keeps_reservation_even_when_readback_is_zero(monkeypatch, tmp_path):
    from agent import dam, rotation
    _seed("gymx_ig")
    photo = tmp_path / "class.jpg"
    photo.write_bytes(b"photo")
    monkeypatch.setattr(gp, "_drive_photo_candidate", lambda *a, **k: None)
    monkeypatch.setattr(gp, "_cropped_image_url",
                        lambda account, image, day: f"https://r2/{day}.jpg")

    class FailedStore:
        def insert_rows(self, _key, _rows):
            raise OSError("write failed")
        def authoritative_rows_for_keys(self, _key, _proposed):
            return []

    with pytest.raises(OSError, match="write failed"):
        gp.plan_gbp_month(
            "gymx", "gymx_ig", voice=_voice(), library_path=str(tmp_path), city="Carmel",
            store=FailedStore(), start=date(2026, 9, 1), days=1,
            offer=None, events=[], caption_fn=_cap)
    assert rotation.local_photo_served(
        dam.rotation_key(str(photo)), "gymx_ig", "2026-09-01") is True


def test_drive_insert_exception_zero_readback_keeps_claim(monkeypatch):
    from agent import db
    _seed("gymx_ig")
    asset = make_asset("drive-1", gym_id="gymx")
    monkeypatch.setattr(
        gp, "_drive_photo_candidate",
        lambda account, day, used: {"url": "https://r2/drive.jpg",
                                    "kind": "drive", "day_key": day,
                                    "asset": asset, "base": "gymx",
                                    "store": FakeMediaStore(assets=[asset])})

    class ZeroReadbackStore:
        def insert_rows(self, _key, _rows):
            raise TimeoutError("POST may finish later")
        def authoritative_rows_for_keys(self, _key, _rows):
            return []

    with pytest.raises(TimeoutError, match="finish later"):
        gp.plan_gbp_month(
            "gymx", "gymx_ig", voice=_voice(), library_path="/x", city="Carmel",
            store=ZeroReadbackStore(), start=date(2026, 9, 1), days=1,
            offer=None, events=[], caption_fn=_cap)
    assert db.socialapi_claim(
        gp._drive_claim_id("gymx", asset), "gymx_gbp")[0] == "in_flight"


def test_local_photo_reservation_has_one_concurrent_winner():
    from agent import rotation
    gate = Barrier(2)

    def reserve(lane):
        gate.wait()
        return rotation.reserve_local_photo_once(
            f"gymx_{lane}", "class.jpg", "photo", "2026-09-01")

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(reserve, ("gbp", "ig")))
    assert sum(rid is not None for rid in ids) == 1


def test_gbp_and_ig_concurrent_same_byte_local_aliases_have_one_winner(
        monkeypatch, tmp_path):
    from types import SimpleNamespace
    from agent import dam, rotation

    _seed("gymx_ig")
    gbp_photo = tmp_path / "gbp-class.jpg"
    ig_photo = tmp_path / "ig-class.jpg"
    gbp_photo.write_bytes(b"same local photo bytes")
    ig_photo.write_bytes(gbp_photo.read_bytes())
    assert dam.rotation_key(str(gbp_photo)) != dam.rotation_key(str(ig_photo))

    monkeypatch.setattr(gp, "_drive_photo_candidate", lambda *a, **k: None)
    monkeypatch.setattr(gp.client_content, "pick_image",
                        lambda *a, **k: SimpleNamespace(
                            path=str(gbp_photo), media_type="image"))
    monkeypatch.setattr(gp, "_cropped_image_url",
                        lambda *a, **k: "https://r2/gbp-class.jpg")
    gate = Barrier(2)
    original_reserve = rotation.reserve_local_photo_once

    def concurrent_reserve(*args, **kwargs):
        gate.wait(timeout=5)
        return original_reserve(*args, **kwargs)

    monkeypatch.setattr(rotation, "reserve_local_photo_once", concurrent_reserve)
    store = _Store()

    def plan_gbp():
        return gp.plan_gbp_month(
            "gymx", "gymx_ig", voice=_voice(), library_path=str(tmp_path),
            city="Carmel", store=store, start=date(2026, 9, 1), days=1,
            offer=None, events=[], caption_fn=_cap)

    def reserve_ig():
        return rotation.reserve_local_photo_once(
            "gymx_ig", dam.rotation_key(str(ig_photo)), "photo", "2026-09-01",
            path=str(ig_photo))

    with ThreadPoolExecutor(max_workers=2) as pool:
        gbp_future = pool.submit(plan_gbp)
        ig_future = pool.submit(reserve_ig)
        gbp_result = gbp_future.result()
        ig_id = ig_future.result()

    assert int(gbp_result["ok"]) + int(ig_id is not None) == 1
    assert len(store.rows) == int(gbp_result["ok"])


@pytest.mark.parametrize("lane", ["ig", "fb", "gbp"])
def test_local_photo_reservation_respects_prior_gym_lane(lane):
    from agent import rotation
    assert rotation.record_served(f"gymx_{lane}", "class.jpg", "photo", "2026-08-01")
    assert rotation.reserve_local_photo_once(
        "gymx_gbp", "class.jpg", "photo", "2026-09-01") is None
    assert rotation.reserve_local_photo_once(
        "other_gbp", "class.jpg", "photo", "2026-09-01") is not None


def test_gate1_offer_skipped_when_not_confirmed():
    # a REAL offer resolves but is not confirmed -> OFFER slot skipped (never a wrong
    # offer to Google). Local updates + photo drops unaffected.
    _seed()
    store = _Store()
    out = gp.plan_gbp_month(
        "lasso", "lasso_ig", voice=_voice(), library_path="/x", city="Carmel",
        store=store, start=date(2026, 9, 1),
        offer=("12 Week Strength", {"redeemOnlineUrl": "https://ghl/join"}),
        offer_confirmed=False, caption_fn=_cap, image_fn=_img)
    assert out["ok"] and out["offer"] == 0 and out["standard"] == 8 and out["photo"] == 4
    assert not any(r["gbp_topic_type"] == "OFFER" for r in store.rows)


def test_legacy_initial_status_cannot_create_new_coach_review_rows():
    # New rows always reach the gym's own approval, even through the lower-level planner.
    _seed()
    store = _Store()
    out = gp.plan_gbp_month(
        "lasso", "lasso_ig", voice=_voice(), library_path="/x", city="Carmel",
        store=store, start=date(2026, 9, 1), offer=None, events=[],
        initial_status="coach_review", caption_fn=_cap, image_fn=_img)
    assert out["ok"] and store.rows
    assert all(r["status"] == "pending" for r in store.rows)


def test_no_offer_skips_offer_slot():
    _seed()
    store = _Store()
    out = gp.plan_gbp_month("lasso", "lasso_ig", voice=_voice(), library_path="/x",
                            city="Carmel", store=store, start=date(2026, 9, 1),
                            offer=None, events=[], caption_fn=_cap, image_fn=_img)
    assert out["ok"] and out["offer"] == 0     # never fabricate an offer


def test_sub_a_plus_caption_skips_slot_not_ships():
    _seed()
    store = _Store()
    # caption_fn returns a dash-laden (non-A+) caption -> every STANDARD slot skips
    out = gp.plan_gbp_month(
        "lasso", "lasso_ig", voice=_voice(), library_path="/x", city="Carmel",
        store=store, start=date(2026, 9, 1), offer=None, events=[],
        caption_fn=lambda f: None, image_fn=_img)   # None = not A+, skip
    assert out["standard"] == 0
    # photo drops still land (no caption gate)
    assert out["photo"] == 4


# ---- B9: an empty GBP month must name what is MISSING -----------------------
# ENG, 2026-09: the month sweep failed for four gyms with the single line
# "nothing planned (no A+ captions or media)" -- true, and useless. It names two
# possible causes and tells the gym which one applies for neither. The A+ gate is
# NOT relaxed by any of this and nothing is fabricated to fill a slot; only the
# sentence the gym reads changes.

def test_empty_month_reason_names_missing_media_not_a_blanket_line():
    msg = gp.empty_month_reason({"no_media": 6})
    assert "no A+ captions or media" not in msg          # the useless line is gone
    assert "6" in msg and "photo" in msg.lower()
    assert "Drive" in msg or "upload" in msg             # tells them where to act


def test_empty_month_reason_ranks_the_biggest_gap_first_and_lists_the_rest():
    msg = gp.empty_month_reason({"no_media": 6, "no_fact": 2, "caption_rejected": 1})
    assert msg.index("photo") < msg.index("sources")     # biggest cause leads
    assert "9" in msg                                    # the honest total
    assert "quality gate" in msg                         # the small causes survive


def test_empty_month_reason_falls_back_when_nothing_was_even_attempted():
    # No slot was tried at all -> the historical wording, so no caller loses a reason.
    assert gp.empty_month_reason({}) == "nothing planned (no A+ captions or media)"
    assert gp.empty_month_reason(None) == "nothing planned (no A+ captions or media)"


def test_no_media_month_tells_the_gym_it_is_photos_and_never_fabricates_a_row():
    _seed()
    store = _Store()
    out = gp.plan_gbp_month("lasso", "lasso_ig", voice=_voice(), library_path="/x",
                            city="Carmel", store=store, start=date(2026, 9, 1),
                            offer=None, events=[], caption_fn=_cap,
                            image_fn=lambda day_key, used: None)   # library is empty
    assert out["ok"] is False and out["planned"] == 0
    assert store.rows == []                          # nothing invented to fill a slot
    assert out["skips"]["no_media"] > 0
    assert "photo" in out["reason"].lower()
    assert "no A+ captions or media" not in out["reason"]


def test_caption_gate_rejection_is_reported_as_a_caption_problem_not_a_photo_one():
    _seed()
    store = _Store()
    out = gp.plan_gbp_month("lasso", "lasso_ig", voice=_voice(), library_path="/x",
                            city="Carmel", store=store, start=date(2026, 9, 1),
                            offer=None, events=[], caption_fn=lambda fact: None,
                            image_fn=_img)           # media is fine, captions are not
    # Photo drops carry no caption, so they still land; every CAPTIONED slot is lost.
    assert out["standard"] == 0 and out["offer"] == 0 and out["event"] == 0
    assert out["skips"]["caption_rejected"] > 0 and out["skips"]["no_media"] == 0
    assert gp.empty_month_reason(out["skips"])
    assert "quality gate" in gp.empty_month_reason(out["skips"])


def test_a_month_that_planned_rows_still_reports_what_it_lost():
    # A PARTIAL month is the common case: the ledger must ride along on success too,
    # so a gym that got 4 posts instead of 13 can still be told why.
    _seed()
    store = _Store()
    calls = {"n": 0}

    def _some_media(day_key, used):
        calls["n"] += 1
        return f"https://r2/gbp/{day_key}.jpg" if calls["n"] <= 3 else None

    out = gp.plan_gbp_month("lasso", "lasso_ig", voice=_voice(), library_path="/x",
                            city="Carmel", store=store, start=date(2026, 9, 1),
                            offer=None, events=[], caption_fn=_cap,
                            image_fn=_some_media)
    assert out["ok"] is True and out["planned"] > 0
    assert out["skips"]["no_media"] > 0


# ---- global media guard: exact source/delivered lineage (2026-10-03) ------

class _PrepStore:
    """Insert store with the prepared-writer render_evidence_by_url kwarg."""

    def __init__(self):
        self.rows = []
        self.evidence_by_url = None

    def insert_rows(self, key, rows, render_evidence_by_url=None):
        self.rows.extend(rows)
        self.evidence_by_url = render_evidence_by_url
        return rows


_FACTS = [("update", "Small group strength coaching for busy parents in Carmel")]


def test_same_object_row_names_delivered_url_as_raw_source(monkeypatch):
    """Guard ON + untransformed (injected) image: the staged row's source_media_url IS
    the delivered URL (raw same-object), and no render evidence is invented."""
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    store = _PrepStore()
    out = gp.plan_gbp_month("gymx", "gymx_gbp", voice=_voice(),
                            library_path="/tmp/none", city="Carmel", store=store,
                            start=date(2026, 10, 5), facts=_FACTS,
                            caption_fn=_cap, image_fn=_img)
    assert out["planned"] > 0
    assert store.rows
    for row in store.rows:
        assert row["source_media_url"] == row["image_url"]
        assert "render_evidence" not in row
    assert store.evidence_by_url == {}


def test_guard_off_keeps_rows_without_lineage_keys(monkeypatch):
    """Default OFF: no source_media_url key, no evidence kwarg — legacy behavior."""
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    store = _Store()
    out = gp.plan_gbp_month("gymx", "gymx_gbp", voice=_voice(),
                            library_path="/tmp/none", city="Carmel", store=store,
                            start=date(2026, 10, 5), facts=_FACTS,
                            caption_fn=_cap, image_fn=_img)
    assert out["planned"] > 0
    assert all("source_media_url" not in row for row in store.rows)


class _Img:
    def __init__(self, path):
        self.path = str(path)
        self.media_type = "image"


def _stub_local_pick(monkeypatch, tmp_path, raw_bytes=b"raw-source-bytes",
                     crop_bytes=b"cropped-delivered-bytes"):
    raw = tmp_path / "photo.jpg"
    raw.write_bytes(raw_bytes)
    crop = tmp_path / "photo_gbp.jpg"
    crop.write_bytes(crop_bytes)
    monkeypatch.setattr(gp.client_content, "pick_image",
                        lambda *a, **k: _Img(raw))
    monkeypatch.setattr(gp, "_cropped_image",
                        lambda *a, **k: ("https://r2/gbp/crop.jpg", str(crop)))
    monkeypatch.setattr(gp.config, "hosting_enabled", lambda: True)
    monkeypatch.setattr(gp.media_host, "host_media",
                        lambda path, acct: "https://r2/gbp/raw.jpg")
    monkeypatch.setattr(gp.rotation, "reserve_local_photo_once",
                        lambda *a, **k: "rid-1")
    monkeypatch.setattr(gp.rotation, "release_served", lambda *a, **k: None)
    return raw, crop


def test_cropped_transform_carries_raw_source_and_byte_bound_evidence(monkeypatch,
                                                                      tmp_path):
    """Guard ON + cropped local photo: row names the hosted RAW source (never the
    cropped URL) and insert receives render evidence bound to the exact bytes."""
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    import hashlib
    raw, crop = _stub_local_pick(monkeypatch, tmp_path)
    store = _PrepStore()
    out = gp.plan_gbp_month("gymx", "gymx_gbp", voice=_voice(),
                            library_path="/tmp/lib", city="Carmel", store=store,
                            start=date(2026, 10, 5), facts=_FACTS,
                            caption_fn=_cap)
    assert out["planned"] > 0
    assert store.rows
    for row in store.rows:
        assert row["image_url"] == "https://r2/gbp/crop.jpg"
        assert row["source_media_url"] == "https://r2/gbp/raw.jpg"
        assert "render_evidence" not in row
    ev = store.evidence_by_url["https://r2/gbp/crop.jpg"]
    assert ev["operation"] == "render"
    assert ev["source_exact_url"] == "https://r2/gbp/raw.jpg"
    assert ev["delivered_exact_url"] == "https://r2/gbp/crop.jpg"
    assert ev["source_fingerprint"] == "md5:" + hashlib.md5(b"raw-source-bytes").hexdigest()
    assert ev["delivered_fingerprint"] == "md5:" + hashlib.md5(b"cropped-delivered-bytes").hexdigest()
    assert ev["source_byte_length"] == len(b"raw-source-bytes")
    assert ev["delivered_byte_length"] == len(b"cropped-delivered-bytes")
    # Exercise the production owner-receipt contract, which validates provenance
    # fields as well as the byte-bound lineage values above.
    monkeypatch.setattr(gp.config, "S3_PUBLIC_BASE_URL", "https://r2")
    receipt_identity = owner._validate(
        "11111111-1111-4111-8111-111111111111", "vg_gbp_crop",
        b"raw-source-bytes", b"cropped-delivered-bytes", ev, None)
    assert receipt_identity[6:10] == (
        "render", ev["evidence_ref"], "gbp_planner", "gbp_planner")
    assert ev["evidence_ref"].startswith(
        "gbp_planner:render:"
        + hashlib.md5(b"raw-source-bytes").hexdigest()
        + ":" + hashlib.md5(b"cropped-delivered-bytes").hexdigest() + ":")


def test_cropped_transform_holds_when_raw_source_cannot_be_hosted(monkeypatch,
                                                                  tmp_path):
    """Guard ON, raw source hosting fails -> the transformed write is HELD (slot
    skipped, nothing staged), never staged with the cropped URL as its own source."""
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "1")
    raw, _crop = _stub_local_pick(monkeypatch, tmp_path)
    monkeypatch.setattr(gp.media_host, "host_media", lambda path, acct: None)
    store = _PrepStore()
    out = gp.plan_gbp_month("gymx", "gymx_gbp", voice=_voice(),
                            library_path="/tmp/lib", city="Carmel", store=store,
                            start=date(2026, 10, 5), facts=_FACTS,
                            caption_fn=_cap)
    assert out["planned"] == 0
    assert store.rows == []
    assert out["skips"]["no_media"] > 0


def test_transformed_evidence_uses_real_bytes_not_invented(monkeypatch, tmp_path):
    """Evidence helper returns None (hold upstream) when byte objects are unreadable."""
    assert gp._render_evidence_dict("https://r2/a", "https://r2/b",
                                    tmp_path / "missing-a",
                                    tmp_path / "missing-b") is None
    src = tmp_path / "s"
    src.write_bytes(b"s")
    assert gp._render_evidence_dict("https://r2/a", "https://r2/b",
                                    src, tmp_path / "missing") is None
