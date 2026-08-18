"""
Per-client MONTH builder (client_month_run). Fully OFFLINE: an injected store + a tmp
media library (fake image files). NEW RULE: a CLIENT gym's calendar is built ONLY from
its OWN uploaded photos/videos. Echo NEVER renders an infographic-only calendar for a
client; a client with no media WAITS. Asserts:
  * flag OFF -> ok:False and the store is never touched
  * NO MEDIA -> Echo WAITS: ok:False, awaiting_media True, 0 rows, store UNTOUCHED
    (no delete, no insert), and NO infographic is ever produced
  * a stocked media library -> PAUSED real-photo rows, gym_id = the BASE, IG+FB for
    feeds and IG-only for stories, image_url is the gym's OWN photo url, NO id, status
    'pending'
  * a day with no photo is SKIPPED (never infographic-filled)
  * a source whose caption carries a banned word is DROPPED (never in the output),
    and a different clean source still fills the day
  * the four gritx/topfuel accounts exist and are inactive
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_content, client_month_run as cmr, client_sources as cs  # noqa: E402
from agent.accounts import Account, Platform, get_account  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setenv("AGENT_CLIENT_MONTH", "true")
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    yield


class _FakeStore:
    def __init__(self):
        self.deleted = []
        self.inserted = []

    def delete_month(self, base_key, month):
        self.deleted.append((base_key, month))
        return 0

    def insert_rows(self, base_key, rows):
        self.inserted.extend(rows)
        return rows            # echo back so upserted counts


def _voice():
    return VoiceDoc(raw="We help members win.\n#GetFit",
                    hashtags=["#GetFit"], ctas=["Save this post."])


def _account():
    return Account(key="gritx_ig", display_name="GritX", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="TID")


def _lib(tmp_path, n=6):
    """A gym's OWN uploaded media library: n fake image files, each with a .json sidecar
    carrying a public_url (as Blake-by-hand hosting sets). The sidecar public_url is what
    makes the draft a portal-ready real-photo card offline (no S3 / network needed)."""
    import json
    lib = tmp_path / "gritx_lib"
    lib.mkdir(exist_ok=True)
    for i in range(n):
        (lib / f"photo_{i:02d}.jpg").write_bytes(b"\xff\xd8\xffFAKEJPEG")
        (lib / f"photo_{i:02d}.json").write_text(
            json.dumps({"public_url": f"https://gritx.media/photo_{i:02d}.jpg"}))
    return str(lib)


def _stock_clean(account_key):
    cs.add_source(account_key, "offer", "21 day kickstart for busy parents", "client social intake")
    cs.add_source(account_key, "service", "Small group training", "client social intake")
    cs.add_source(account_key, "about", "Who we help: parents in their 40s", "client social intake")


# ---- 1. flag OFF -> nothing touched ----------------------------------------------
def test_flag_off_touches_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CLIENT_MONTH", "false")
    _stock_clean("gritx_ig")
    lib = _lib(tmp_path)
    store = _FakeStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=5, voice=_voice(),
        library_path=lib, store=store, banned_words=())
    assert out["ok"] is False
    assert store.deleted == [] and store.inserted == []


# ---- 2. NO MEDIA -> Echo WAITS, store untouched, no infographic -------------------
def test_no_media_waits_and_touches_nothing(tmp_path):
    _stock_clean("gritx_ig")
    store = _FakeStore()
    empty = tmp_path / "empty_lib"      # exists but no media files
    empty.mkdir()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=10, voice=_voice(),
        library_path=str(empty), store=store, banned_words=())
    assert out["ok"] is False
    assert out["awaiting_media"] is True
    assert out["upserted"] == 0
    assert out["days"] == 0
    assert out["skipped_banned"] == 0
    # store COMPLETELY untouched: no delete, no insert
    assert store.deleted == []
    assert store.inserted == []


def test_missing_library_path_waits(tmp_path):
    _stock_clean("gritx_ig")
    store = _FakeStore()
    # library_path=None (never uploaded) and a non-existent path both count as no media
    out_none = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=5, voice=_voice(),
        library_path=None, store=store, banned_words=())
    out_missing = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=5, voice=_voice(),
        library_path=str(tmp_path / "does_not_exist"), store=store, banned_words=())
    for out in (out_none, out_missing):
        assert out["ok"] is False and out["awaiting_media"] is True
    assert store.deleted == [] and store.inserted == []


def test_client_awaiting_media_helper(tmp_path):
    assert cmr.client_awaiting_media("gritx", None) is True
    empty = tmp_path / "e"
    empty.mkdir()
    assert cmr.client_awaiting_media("gritx", str(empty)) is True
    lib = _lib(tmp_path)
    assert cmr.client_awaiting_media("gritx", lib) is False


def test_media_count_counts_images_and_videos(tmp_path):
    lib = tmp_path / "mixed"
    lib.mkdir()
    (lib / "a.jpg").write_bytes(b"x")
    (lib / "b.PNG").write_bytes(b"x")
    (lib / "c.mp4").write_bytes(b"x")
    (lib / "d.mov").write_bytes(b"x")
    (lib / "notes.txt").write_bytes(b"x")     # sidecar, not counted
    (lib / "sub").mkdir()                       # dir, not counted
    assert cmr._client_media_count(str(lib)) == 4


# ---- 3. stocked media library -> PAUSED real-photo rows, IG+FB feed, IG story -----
def test_builds_paused_real_photo_rows_with_fb_mirror(tmp_path):
    _stock_clean("gritx_ig")
    lib = _lib(tmp_path, n=6)
    store = _FakeStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=10, voice=_voice(),
        library_path=lib, store=store, banned_words=())
    assert out["ok"] is True
    rows = store.inserted
    assert rows, "no rows inserted"
    # every row: gym_id = BASE, PAUSED, no id, image_url is the gym's OWN uploaded photo
    for r in rows:
        assert r["gym_id"] == "gritx"
        assert r["status"] == "pending"          # PAUSED, never approved/published
        assert "id" not in r
        assert r["image_url"], "row must carry the gym's real photo url"
        # NEVER an infographic/template card: the url is a real hosted/public library url
        assert "cdn.example" not in r["image_url"]
    # feeds appear on BOTH instagram and facebook; stories instagram-only
    feed_ig = [r for r in rows if r["format"] == "feed" and r["account"] == "instagram"]
    feed_fb = [r for r in rows if r["format"] == "feed" and r["account"] == "facebook"]
    story_rows = [r for r in rows if r["format"] == "story"]
    assert len(feed_ig) == len(feed_fb) and len(feed_ig) > 0
    assert all(r["account"] == "instagram" for r in story_rows)
    assert story_rows, "no story rows"
    # delete-then-insert swept the month
    assert ("gritx", "2026-08") in store.deleted


def test_feed_autofit_reframes_feed_but_never_the_story(monkeypatch, tmp_path):
    # AGENT_FEED_AUTOFIT on + STORY_FORMAT off: the FEED gets the 1080x1080 square, but the
    # paired STORY must keep the RAW photo (never a square pillarboxed into a 9:16 slot).
    monkeypatch.setenv("AGENT_FEED_AUTOFIT", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.delenv("AGENT_STORY_FORMAT", raising=False)   # story-format OFF (baseline)
    from agent import feed_image, media_host
    # every feed photo is treated as out-of-spec -> reframed to a sentinel square asset
    monkeypatch.setattr(feed_image, "get_or_make_feed_image",
                        lambda p, lib, logger=None: "/REFRAMED__feed.jpg")
    # host_media: the square asset -> a SQUARE url; any other path -> a raw-photo url
    monkeypatch.setattr(media_host, "host_media",
                        lambda path, key, client=None: ("https://cdn/SQUARE.jpg"
                                                        if str(path).endswith("__feed.jpg")
                                                        else f"https://cdn/{os.path.basename(str(path))}"))
    _stock_clean("gritx_ig")
    lib = _lib(tmp_path, n=6)
    store = _FakeStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=10, voice=_voice(),
        library_path=lib, store=store, banned_words=())
    assert out["ok"] is True
    feeds = [r for r in store.inserted if r["format"] == "feed"]
    stories = [r for r in store.inserted if r["format"] == "story"]
    assert feeds and stories
    # FEED carries the reframed square...
    assert all(r["image_url"] == "https://cdn/SQUARE.jpg" for r in feeds)
    # ...but the STORY never does — it keeps the raw photo url.
    assert all(r["image_url"] != "https://cdn/SQUARE.jpg" for r in stories)
    assert all("__feed.jpg" not in r["image_url"] for r in stories)


# ---- 3b. GATE 2 coach-screens-first-month (FB/IG client month) -------------------

class _StoreWithHistory(_FakeStore):
    """A store that reports whether the gym already has owner-visible rows (the real
    Supabase signal), so the first-month gate can engage."""
    def __init__(self, has_visible):
        super().__init__()
        self._has_visible = has_visible

    def has_owner_visible_rows(self, base_key):
        return self._has_visible


def test_gate2_first_month_withheld_as_coach_review(tmp_path):
    _stock_clean("gritx_ig")
    lib = _lib(tmp_path, n=6)
    store = _StoreWithHistory(has_visible=False)   # brand-new gym, no prior rows
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=10, voice=_voice(),
        library_path=lib, store=store, banned_words=())
    assert out["ok"] is True and store.inserted
    assert all(r["status"] == "coach_review" for r in store.inserted), \
        "a gym's first month must be withheld from the owner until a coach releases it"


def test_gate2_established_gym_grandfathered_pending(tmp_path):
    _stock_clean("gritx_ig")
    lib = _lib(tmp_path, n=6)
    store = _StoreWithHistory(has_visible=True)     # already has owner-visible rows
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=10, voice=_voice(),
        library_path=lib, store=store, banned_words=())
    assert out["ok"] is True and store.inserted
    assert all(r["status"] == "pending" for r in store.inserted), \
        "an established gym is grandfathered, never re-withheld on a rebuild"


def test_gate2_off_writes_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COACH_SCREEN_FIRST_MONTH", "false")
    _stock_clean("gritx_ig")
    lib = _lib(tmp_path, n=6)
    store = _StoreWithHistory(has_visible=False)
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=10, voice=_voice(),
        library_path=lib, store=store, banned_words=())
    assert out["ok"] is True and all(r["status"] == "pending" for r in store.inserted)


def test_gate2_store_without_signal_defaults_pending(tmp_path):
    # a store lacking has_owner_visible_rows (legacy/tests) never withholds by accident
    _stock_clean("gritx_ig")
    lib = _lib(tmp_path, n=6)
    store = _FakeStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=10, voice=_voice(),
        library_path=lib, store=store, banned_words=())
    assert out["ok"] is True and all(r["status"] == "pending" for r in store.inserted)


# ---- 4. a day with no photo is SKIPPED, never infographic-filled ------------------
def test_day_with_no_photo_is_skipped_never_infographic(tmp_path, monkeypatch):
    _stock_clean("gritx_ig")
    lib = _lib(tmp_path, n=6)
    store = _FakeStore()

    real = client_content.build_client_draft

    def _sometimes_no_photo(account, day_key, voice, library_path, **kw):
        # Force the 2nd calendar day (2026-08-02) to have NO usable photo, so its draft
        # comes back as needs-media. Every other day builds a real-photo draft.
        d = real(account, day_key, voice, library_path, **kw)
        if d is not None and str(day_key).startswith("2026-08-02"):
            d.needs_media = True
            d.creative_public_url = ""
        return d

    monkeypatch.setattr(cmr.client_content, "build_client_draft", _sometimes_no_photo)

    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=3, voice=_voice(),
        library_path=lib, store=store, banned_words=())
    assert out["ok"] is True
    # 2026-08-02 produced NO row at all (skipped), and certainly no infographic card
    for r in store.inserted:
        assert r["post_date"] != "2026-08-02", "a no-photo day must be skipped, not filled"
        assert r["image_url"], "no blank/infographic card ever emitted"
    # the other two days still produced real rows
    assert any(r["post_date"] == "2026-08-01" for r in store.inserted)


# ---- 5. a banned-word caption is DROPPED; a clean source still fills the day ------
def test_banned_word_dropped_never_emitted(tmp_path):
    cs.add_source("gritx_ig", "service", "High Intensity CrossFit style Cardio", "client social intake")
    _stock_clean("gritx_ig")
    lib = _lib(tmp_path, n=6)
    store = _FakeStore()
    banned = ["crossfit", "bootcamp", "cardio", "hyrox", "intensity", "compete"]
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=14, voice=_voice(),
        library_path=lib, store=store, banned_words=banned)
    assert out["ok"] is True
    for r in store.inserted:
        cap = r["caption"].lower()
        for w in banned:
            assert w not in cap, f"banned word {w!r} leaked: {r['caption']!r}"
    assert out["upserted"] > 0


def test_all_sources_banned_drops_every_day(tmp_path):
    cs.add_source("gritx_ig", "service", "CrossFit Cardio Intensity", "client social intake")
    cs.add_source("gritx_ig", "offer", "Bootcamp Hyrox Compete", "client social intake")
    lib = _lib(tmp_path, n=6)
    store = _FakeStore()
    banned = ["crossfit", "cardio", "intensity", "bootcamp", "hyrox", "compete"]
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=5, voice=_voice(),
        library_path=lib, store=store, banned_words=banned)
    assert out["ok"] is True
    assert out["skipped_banned"] == 5      # the guard fired every day
    assert out["upserted"] == 0
    assert store.inserted == []


# ---- 6. no infographic is EVER produced (no template card url in any row) ---------
def test_never_produces_an_infographic(tmp_path):
    _stock_clean("gritx_ig")
    lib = _lib(tmp_path, n=6)
    store = _FakeStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=12, voice=_voice(),
        library_path=lib, store=store, banned_words=())
    assert out["ok"] is True
    for r in store.inserted:
        # every image is a real uploaded photo url, never a template_card fallback
        assert r["image_url"]
        assert "template" not in r["image_url"].lower()


# ---- 7. the four client accounts exist and are inactive --------------------------
def test_accounts_exist_inactive():
    for key in ("gritx_ig", "gritx_fb", "topfuel_ig", "topfuel_fb"):
        a = get_account(key)
        assert a is not None, f"{key} missing"
        assert a.active is False, f"{key} must be inactive"


# ---- 8. banned-word matcher is word-boundary (no false positives) ----------------
def test_banned_word_boundary():
    assert cmr._has_banned_word("we compete weekly", ["compete"])
    assert not cmr._has_banned_word("we are competent coaches", ["compete"])
    assert not cmr._has_banned_word("clean caption", [])


# ---- 9. polluted served-ledger collapse regression (Dale's 1-feed month) ---------
def test_polluted_ledger_still_places_distinct_photos(tmp_path, monkeypatch):
    """Repeated plan-then-delete rebuild passes leave EVERY photo 'served' with a
    future date. The old pick then returned the SAME photo for every day (its own
    re-record kept it the least-recently-served minimum) and the month collapsed to
    ONE feed day. The exclude_keys threading must keep every feed on a distinct photo."""
    _stock_clean("gritx_ig")
    lib = _lib(tmp_path, n=5)
    # pollute: all 5 photos served, dates spread across the FUTURE month
    served = [{"key": f"photo_{i:02d}.jpg", "date": f"2026-08-{15 + i:02d}",
               "pillar": "service"} for i in range(5)]
    monkeypatch.setattr(client_content.rotation, "load_served",
                        lambda: {"gritx_ig": list(served)})
    store = _FakeStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=5, voice=_voice(),
        library_path=lib, store=store, banned_words=())
    assert out["ok"] is True
    feed_ig = [r for r in store.inserted
               if r["format"] == "feed" and r["account"] == "instagram"]
    urls = [r["image_url"] for r in feed_ig]
    assert len(urls) == 5, f"collapsed to {len(urls)} feed day(s)"
    assert len(set(urls)) == 5, "a photo was reused across feeds"


# ---- 10. locked (approved) days are skipped; their photos never re-picked --------
class _LockedStore(_FakeStore):
    """FakeStore that also reports existing rows, like the live list_month."""

    def __init__(self, existing):
        super().__init__()
        self._existing = list(existing)

    def list_month(self, base_key, month):
        return [dict(r) for r in self._existing
                if str(r.get("post_date", "")).startswith(month)]


def test_locked_day_skipped_and_photo_never_repicked(tmp_path):
    _stock_clean("gritx_ig")
    lib = _lib(tmp_path, n=6)
    locked = [
        {"gym_id": "gritx", "post_date": "2026-08-02", "account": "instagram",
         "format": "feed", "status": "approved",
         "image_url": "https://gritx.media/photo_01.jpg"},
        {"gym_id": "gritx", "post_date": "2026-08-02", "account": "instagram",
         "format": "story", "status": "approved",
         "image_url": "https://gritx.media/photo_01.jpg"},
    ]
    store = _LockedStore(locked)
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=6, voice=_voice(),
        library_path=lib, store=store, banned_words=())
    assert out["ok"] is True
    # no new row lands on the locked day
    assert not [r for r in store.inserted if r["post_date"] == "2026-08-02"], \
        "planned a competing row on an approved day"
    # the approved photo is never re-placed on another day
    assert not [r for r in store.inserted
                if r["image_url"].endswith("photo_01.jpg")], \
        "re-picked a photo already locked to an approved post"
    # cap: 6 media minus 1 locked photo -> at most 5 new feed days
    feed_ig = [r for r in store.inserted
               if r["format"] == "feed" and r["account"] == "instagram"]
    assert 1 <= len(feed_ig) <= 5


# ---- 11. uploaded VIDEOS are placeable (not silently skipped forever) ------------
def test_videos_are_placed(tmp_path):
    import json as _json
    _stock_clean("gritx_ig")
    lib = tmp_path / "vids"
    lib.mkdir()
    for i in range(2):
        (lib / f"clip_{i}.mp4").write_bytes(b"\x00\x00FAKEMP4")
        (lib / f"clip_{i}.json").write_text(
            _json.dumps({"public_url": f"https://gritx.media/clip_{i}.mp4"}))
    store = _FakeStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=4, voice=_voice(),
        library_path=str(lib), store=store, banned_words=())
    assert out["ok"] is True
    feed_ig = [r for r in store.inserted
               if r["format"] == "feed" and r["account"] == "instagram"]
    assert feed_ig, "videos were never placed"
    assert all(r["image_url"].endswith(".mp4") for r in feed_ig)


# ---- 12. a DENIED photo stays available (only live photos are consumed) ----------
def test_denied_photo_is_not_excluded(tmp_path):
    _stock_clean("gritx_ig")
    lib = _lib(tmp_path, n=3)
    locked = [
        # denied feed on 08-02: its DAY stays locked, but its PHOTO is reusable
        {"gym_id": "gritx", "post_date": "2026-08-02", "account": "instagram",
         "format": "feed", "status": "denied",
         "image_url": "https://gritx.media/photo_01.jpg"},
    ]
    store = _LockedStore(locked)
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=4, voice=_voice(),
        library_path=lib, store=store, banned_words=())
    assert out["ok"] is True
    # the denied day itself is not re-planned
    assert not [r for r in store.inserted if r["post_date"] == "2026-08-02"]
    # but photo_01 IS placeable on another day (3 photos -> 3 feed days possible)
    used = {r["image_url"] for r in store.inserted if r["format"] == "feed"
            and r["account"] == "instagram"}
    assert "https://gritx.media/photo_01.jpg" in used


# ---- 13. cross-day OPENING variety with LOW source variety (Ryan Parr) -----------
def test_low_variety_month_diversifies_openings(tmp_path, monkeypatch):
    """Ryan Parr, 2026-08-17: with few facts and one photo shoot, several planned days
    in a row led with the SAME opener ('You're juggling too much' x3, then 'You're
    swamped' x3). The month builder must thread each accepted day's opening into the
    NEXT day's generation so the openings diversify. This simulates a repetition-prone
    model (SB7 armed, LLM stubbed): unguided it always opens the same way; once the
    avoid list reaches the prompt it varies. Asserts (a) the accepted captions do NOT
    all share one opening and (b) the avoid-openings signal reached the generator."""
    from agent import drafter
    monkeypatch.setenv("AGENT_SB7_ENABLED", "true")

    # LOW source variety: a couple of same-theme facts (like Ryan's account).
    cs.add_source("gritx_ig", "service", "Busy schedule fitness for working parents",
                  "client social intake")
    cs.add_source("gritx_ig", "offer", "Swamped parent kickstart program",
                  "client social intake")

    seen_avoid_blocks = []

    def _fake_llm(system, user):
        # record whether the cross-day avoid block reached the prompt
        seen_avoid_blocks.append("OPENINGS ALREADY USED" in user)
        # A repetition-prone model: default to the SAME stock opener every time. When
        # the prompt tells it which openings to avoid, it varies the entry point so the
        # opening is genuinely different (12+ content words, no figures, no dashes).
        if "OPENINGS ALREADY USED" in user:
            return ("Ready to feel strong again and finally enjoy your workouts every "
                    "single week with people who cheer you on")
        return ("You are juggling too much and there is never enough time in your day "
                "to take proper care of yourself")

    monkeypatch.setattr(drafter, "_call_llm_caption", _fake_llm)

    lib = _lib(tmp_path, n=6)
    store = _FakeStore()
    out = cmr.build_client_month(
        _account(), "gritx", "2026-08-01", days=6, voice=_voice(),
        library_path=lib, store=store, banned_words=())
    assert out["ok"] is True

    feed_ig = [r for r in store.inserted
               if r["format"] == "feed" and r["account"] == "instagram"]
    assert len(feed_ig) >= 3, "not enough feed days built to observe repetition"

    # (a) the accepted feed captions do NOT all lead with the same opening
    sigs = {drafter.opening_signature(r["caption"]) for r in feed_ig}
    assert len(sigs) > 1, f"all feed openings collapsed to one hook: {sigs}"

    # (b) the avoid-openings signal reached the generator on later days (day 1 has no
    # prior opening, so it is absent then; it must be present on at least one later day)
    assert any(seen_avoid_blocks), "the avoid-openings signal never reached the prompt"
