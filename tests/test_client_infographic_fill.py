"""
client_infographic_fill (agent/client_infographic_fill.py), fully offline.

Blake 2026-08-25: a gym that stops uploading photos gets on-brand infographic posts
built from its own APPROVED sources instead of going dark. Asserts: flag gate, gap
detection (a day with any active IG feed is never touched), per-run cap, PENDING
insert-only rows with IG+FB mirror, source grounding, and the A+ caption gate.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_infographic_fill as cif  # noqa: E402
from agent import client_sources as cs  # noqa: E402
from agent import config  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


GYMX_BRAND_COLORS = ["#1B2A3C", "#F2EDDE", "#D7263D"]


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_INFOGRAPHIC_FILL", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path / "lib"), raising=False)
    # Verified brand colors for the test gym (Blake 2026-10-02: the fill lane
    # fails CLOSED without them). Tests that exercise the missing-palette hold
    # point AGENT_CLIENT_VOICE_DIR at an empty dir.
    voice_dir = tmp_path / "voice"
    gym_dir = voice_dir / "gymx"
    gym_dir.mkdir(parents=True, exist_ok=True)
    import json as _json
    (gym_dir / "brand_colors.json").write_text(
        _json.dumps({"colors": GYMX_BRAND_COLORS,
                     "source_url": "https://gymx.example/brand-guide"}))
    monkeypatch.setenv("AGENT_CLIENT_VOICE_DIR", str(voice_dir))
    from agent import gym_media_index

    class EmptyDriveIndex:
        def available(self):
            return True

        def list_sources(self, _base):
            return [{"kind": "gym_drive", "active": True,
                     "revoked_externally": False, "sync_status": "ready",
                     "sync_finished_at": "2026-10-02T00:00:00Z"}]

        def list_assets(self, _base):
            return []

    monkeypatch.setattr(gym_media_index, "default_store", lambda: EmptyDriveIndex())
    # hosting + nano stubbed per test


def _acct():
    return Account(key="gymx_ig", display_name="Gym X", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="G")


def _voice():
    return VoiceDoc(raw="We help busy people win.\n#GymX",
                    hashtags=["#GymX"], ctas=["Book your intro session."])


def _sources():
    cs.add_source("gymx_ig", "educational",
                  "Strength training twice a week protects your joints as you age and "
                  "keeps everyday tasks feeling easy well into your sixties",
                  "website /blog")
    cs.add_source("gymx_ig", "service",
                  "Small group personal training built for beginners who want real "
                  "coaching without the intimidation of a big box gym floor",
                  "website /services")


class _Store:
    """In-memory calendar: existing rows + records inserts. INSERT-only assertable."""

    def __init__(self, rows=()):
        self._rows = [dict(r) for r in rows]
        self.inserted = []
        self.deleted = []

    def list_month(self, base, month):
        return [dict(r) for r in self._rows
                if str(r.get("post_date", "")).startswith(month)]

    def insert_rows(self, base, rows):
        self.inserted.extend(rows)
        return rows

    def delete_month(self, *a, **k):       # must never be called
        self.deleted.append(a)
        return 0


def _stub_pipeline(monkeypatch):
    """Stub nano + hosting so the test is offline; captions go through the REAL
    make_caption (template path, no LLM key) and the REAL A+ gate."""
    from agent import creative_studio, media_host

    class _Client:
        def generate_image(self, prompt, model):
            return b"\x89PNG_fake_card_bytes"
    monkeypatch.setattr(creative_studio, "_default_client", lambda: _Client())
    monkeypatch.setattr(creative_studio, "_render_with_timeout", lambda fn: fn())
    monkeypatch.setattr(media_host, "host_media",
                        lambda path, key: f"https://r2/{os.path.basename(path)}")


def test_flag_off_is_noop(monkeypatch):
    monkeypatch.setenv("AGENT_CLIENT_INFOGRAPHIC_FILL", "false")
    out = cif.fill_gaps("gymx", _acct(), _Store(), voice=_voice())
    assert out["ok"] is False and out["reason"] == "flag off"


def test_fills_empty_days_with_pending_infographic_rows(monkeypatch):
    _sources()
    _stub_pipeline(monkeypatch)
    _arm_astra(monkeypatch, 200, _astra_body())
    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00")
    assert out["ok"] is True and out["filled"] == cif.FILL_MAX_PER_RUN, out
    assert store.deleted == [], "fill must be INSERT-only"
    feeds_ig = [r for r in store.inserted
                if r["format"] == "feed" and r["account"] == "instagram"]
    feeds_fb = [r for r in store.inserted
                if r["format"] == "feed" and r["account"] == "facebook"]
    assert len(feeds_ig) == cif.FILL_MAX_PER_RUN and len(feeds_fb) == len(feeds_ig)
    for r in store.inserted:
        assert r["status"] == "pending", "every card awaits the owner's approval"
        assert r["gym_id"] == "gymx"
        assert (r.get("image_url") or "").startswith("https://r2/igfill_")
        assert "id" not in r
        assert len(r.get("caption") or "") >= 40           # a real caption, not a stub


def test_every_inserted_row_carries_the_client_safe_review_mark(monkeypatch):
    """PRODUCTION INCIDENT (2026-09-11): rows this lane inserted before the mark
    existed reached crossfitnewtown, district_h, toughtemple52040e and
    crossfitreverb30b5b2 unmarked -- 3 of them were already status='approved' and
    would have cleared calendar_autopublish's CLIENT-SAFE REVIEW HARD BLOCK
    (agent/calendar_autopublish.py) silently, since that block trusts the pillar
    string alone. Every row fill_gaps() ever inserts MUST carry the mark, no
    exceptions -- this is the regression test for the fix at
    agent/client_infographic_fill.py's Draft(... category=_with_review_mark(...)).
    Fails on revert: swap that call back to the pre-fix
    `getattr(source, "category", "") or "educational"` and every assertion below
    fails."""
    _sources()
    _stub_pipeline(monkeypatch)
    _arm_astra(monkeypatch, 200, _astra_body())
    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00")
    assert out["ok"] is True and out["filled"] > 0
    assert store.inserted, "the fixture must actually exercise an insert"
    for r in store.inserted:
        pillar = r.get("pillar") or r.get("category") or ""
        assert pillar.endswith(cif._NEEDS_CLIENT_SAFE_REVIEW_SUFFIX), (
            f"unmarked row would silently clear the autopublish hard block: {r!r}")
        # the mark is a SUFFIX, not a replacement: the source's real taxonomy
        # category must still be legible to a human reviewer at a glance.
        base = pillar[: -len(cif._NEEDS_CLIENT_SAFE_REVIEW_SUFFIX)]
        assert base in ("educational", "service"), base


def test_days_with_existing_feeds_are_never_touched(monkeypatch):
    _sources()
    _stub_pipeline(monkeypatch)
    # every upcoming day already has an active IG feed -> zero gaps -> zero inserts
    rows = [{"post_date": f"2026-08-{d:02d}", "format": "feed",
             "account": "instagram", "status": "pending"} for d in range(26, 32)] + \
           [{"post_date": f"2026-09-{d:02d}", "format": "feed",
             "account": "instagram", "status": "approved"} for d in range(1, 3)]
    store = _Store(rows)
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00")
    assert out["ok"] is True and out["filled"] == 0
    assert store.inserted == []


def test_denied_days_count_as_empty(monkeypatch):
    _sources()
    _stub_pipeline(monkeypatch)
    _arm_astra(monkeypatch, 200, _astra_body())
    rows = [{"post_date": "2026-08-26", "format": "feed", "account": "instagram",
             "status": "denied"}]
    store = _Store(rows)
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00", days_ahead=1, max_per_run=1)
    assert out["filled"] == 1
    assert store.inserted[0]["post_date"] == "2026-08-26"


def test_no_sources_is_noop(monkeypatch):
    _stub_pipeline(monkeypatch)
    out = cif.fill_gaps("gymx", _acct(), _Store(), voice=_voice())
    assert out["ok"] is False and out["reason"] == "no sources"


# ---- image engine: Astra draws these cards, and a dead chain is never silent ----

def _astra_body():
    import base64 as _b64
    import json as _json
    return _json.dumps({"output": [{
        "type": "image_generation_call",
        "result": _b64.b64encode(b"\x89PNG_astra_card_bytes_padded_long").decode(),
    }]})


def _arm_astra(monkeypatch, status, body):
    """Give the fill path a live Astra key and a scripted Responses reply."""
    from agent import image_engine
    monkeypatch.setenv(image_engine.OPENAI_API_KEY_ENV, "sk-test-not-real")
    monkeypatch.setattr(image_engine, "ASTRA_RETRY_BACKOFF_SECS", 0.0)
    seen = []

    def _post(self, payload):
        seen.append(payload)
        return status, body

    monkeypatch.setattr(image_engine.AstraImageEngine, "_post", _post)
    return seen


def test_fill_cards_are_drawn_by_astra(monkeypatch):
    _sources()
    _stub_pipeline(monkeypatch)
    seen = _arm_astra(monkeypatch, 200, _astra_body())
    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00", days_ahead=1, max_per_run=1)
    assert out["filled"] == 1
    assert len(seen) == 1
    assert seen[0]["model"] == "gpt-6-astra"
    assert seen[0]["tools"][0]["model"] == "gpt-image-2.5-sunburst"


def test_astra_down_holds_the_day_no_gemini_fallback(monkeypatch):
    """Blake 2026-10-02: NO silent Gemini rung and no generic LASSO fallback for
    a client gym's infographic. Astra down = the day stays empty and the slot
    is marked NEEDS HUMAN, never filled by a second engine."""
    _sources()
    _stub_pipeline(monkeypatch)
    seen = _arm_astra(monkeypatch, 503, "astra unavailable")
    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda msg: alerts.append(msg))
    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00", days_ahead=1, max_per_run=1)
    assert len(seen) == 2, "Astra is retried once, then the day is held"
    assert out["filled"] == 0 and store.inserted == []
    assert any("NEEDS HUMAN" in a for a in alerts), alerts


def test_a_dead_chain_marks_the_calendar_slot_needs_human(monkeypatch):
    """A calendar slot may NEVER fail silently."""
    _sources()
    _stub_pipeline(monkeypatch)
    _arm_astra(monkeypatch, 503, "astra unavailable")
    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda msg: alerts.append(msg))

    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00", days_ahead=1, max_per_run=1)
    assert out["filled"] == 0 and store.inserted == []
    assert any("NEEDS HUMAN" in a for a in alerts), alerts

    from agent import db
    rows = [r for r in db.audit_rows() if r["kind"] == "image_needs_human"]
    assert rows and rows[0]["account_key"] == "gymx_ig"


# ---- Blake 2026-10-02: photos first, Astra route, verified gym palette ----

def test_missing_brand_colors_fails_closed(monkeypatch, tmp_path):
    """No verified palette on file -> the infographic fallback HELDS and says
    why. Colors are never invented from the voice doc's tone."""
    _sources()
    _stub_pipeline(monkeypatch)
    monkeypatch.setenv("AGENT_CLIENT_VOICE_DIR", str(tmp_path / "empty_voice"))
    logs = []
    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00",
                        logger=logs.append)
    assert out["ok"] is False
    assert "no verified brand colors" in out["reason"]
    assert store.inserted == []
    assert any("brand colors" in m for m in logs)


def test_unproven_brand_colors_fail_closed(monkeypatch, tmp_path):
    """Hex values alone are not evidence of this gym's actual palette."""
    _sources()
    _stub_pipeline(monkeypatch)
    voice_dir = tmp_path / "voice"
    gym_dir = voice_dir / "gymx"
    gym_dir.mkdir(parents=True, exist_ok=True)
    (gym_dir / "brand_colors.json").write_text(
        '{"colors": ["#1B2A3C", "#F2EDDE"]}')
    monkeypatch.setenv("AGENT_CLIENT_VOICE_DIR", str(voice_dir))
    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00")
    assert out["ok"] is False
    assert "no verified brand colors" in out["reason"]
    assert store.inserted == []


def test_account_gym_mismatch_fails_closed(monkeypatch):
    """A client fill never inherits a palette exception from another account."""
    _sources()
    _stub_pipeline(monkeypatch)
    account = Account(key="lasso_ig", display_name="LASSO",
                      platform=Platform.INSTAGRAM, token_env="T", target_id_env="G")
    store = _Store()
    out = cif.fill_gaps("gymx", account, store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00")
    assert out["ok"] is False
    assert "account/gym mismatch" in out["reason"]
    assert store.inserted == []


def test_astra_brief_carries_the_verified_gym_palette(monkeypatch):
    """The brief Astra actually receives names THIS gym's verified hex colors
    and forbids inventing others."""
    _sources()
    _stub_pipeline(monkeypatch)
    seen = _arm_astra(monkeypatch, 200, _astra_body())
    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00", days_ahead=1, max_per_run=1)
    assert out["filled"] == 1
    brief = seen[0]["input"]
    if not isinstance(brief, str):
        brief = " ".join(str(c.get("text", "")) for c in brief
                         if isinstance(c, dict))
    for hex_color in GYMX_BRAND_COLORS:
        assert hex_color in brief, brief
    assert "VERIFIED FOR THIS GYM" in brief


def test_client_gym_never_initializes_the_gemini_lane(monkeypatch):
    """The client fallback stays Astra-only even if a quality flag is misrouted."""
    from agent import creative_studio

    _sources()
    monkeypatch.setattr(config, "lasso_infographic_quality_enabled", lambda _key: True)
    monkeypatch.setattr(
        creative_studio, "_default_client",
        lambda: (_ for _ in ()).throw(AssertionError("Gemini lane must stay unused")))
    _arm_astra(monkeypatch, 200, _astra_body())
    from agent import media_host
    monkeypatch.setattr(media_host, "host_media",
                        lambda path, key: f"https://r2/{os.path.basename(path)}")
    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00", days_ahead=1, max_per_run=1)
    assert out["filled"] == 1


def test_indexed_drive_photo_holds_infographic_even_with_drive_flags_off(
        monkeypatch):
    """2026-10-02 regression (8e06dbf): the indexed Drive inventory is authoritative
    even when the staging lane is disabled. A pickable approved client photo must
    hold the Astra fallback with BOTH Drive flags off."""
    from agent import gym_media_index
    from tests.gym_media_fakes import make_asset, bound_review_fields

    photo = make_asset("ph1", gym_id="gymx", kind="photo", title="team.jpg")
    photo.update(bound_review_fields("ph1", "gymx"))

    class DriveIndexWithPhoto:
        def available(self):
            return True

        def list_sources(self, _base):
            return [{"kind": "gym_drive", "active": True,
                     "revoked_externally": False, "sync_status": "ready",
                     "sync_finished_at": "2026-10-02T00:00:00Z"}]

        def list_assets(self, _base):
            return [photo]

    monkeypatch.setattr(gym_media_index, "default_store",
                        lambda: DriveIndexWithPhoto())
    monkeypatch.setenv("GYM_DRIVE_STAGE", "false")
    monkeypatch.setenv("GYM_DRIVE_CONNECT", "false")
    _sources()
    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00")
    assert out["filled"] == 0 and store.inserted == [], out
    assert not cif.real_media_depleted("gymx", now="2026-08-25T12:00:00-04:00")


def test_unsynced_drive_source_holds_the_infographic_fallback(monkeypatch):
    """2026-10-02 regression (733da2b): an empty asset list is depletion proof only
    after a successful source sync. A source still syncing (or never finished) must
    fail CLOSED and hold the Astra fallback."""
    from agent import gym_media_index

    class UnsyncedDriveIndex:
        def available(self):
            return True

        def list_sources(self, _base):
            return [{"kind": "gym_drive", "active": True,
                     "revoked_externally": False, "sync_status": "syncing"}]

        def list_assets(self, _base):
            return []

    monkeypatch.setattr(gym_media_index, "default_store",
                        lambda: UnsyncedDriveIndex())
    _sources()
    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00")
    assert out["filled"] == 0 and store.inserted == [], out
    assert not cif.real_media_depleted("gymx", now="2026-08-25T12:00:00-04:00")


def test_photos_rechecked_at_generation_time(monkeypatch):
    """A photo landing between the scan and the render always wins: the fill
    stops instead of drawing an infographic."""
    _sources()
    _stub_pipeline(monkeypatch)
    _arm_astra(monkeypatch, 200, _astra_body())
    calls = {"n": 0}
    real = cif.real_media_depleted

    def _flip(base, *, now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(base, now=now)   # the top-of-scan check: still depleted
        return False                     # generation-time recheck: photos arrived

    monkeypatch.setattr(cif, "real_media_depleted", _flip)
    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00", days_ahead=1, max_per_run=1)
    assert calls["n"] >= 2, "the generation-time recheck must actually run"
    assert out["filled"] == 0 and store.inserted == []


def test_pending_client_photo_blocks_last_resort_infographic(monkeypatch):
    """An indexed Drive photo awaiting moderation is not an empty photo pool."""
    from agent import gym_media_index

    class MediaStore:
        def available(self):
            return True

        def list_assets(self, gym):
            return [{"id": "photo-1", "gym_id": gym, "kind": "photo",
                     "eligible": True, "excluded_by_coach": False,
                     "review_status": "pending_review",
                     "moderation_status": "pending", "content_hash": "hash"}]

    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: True)
    monkeypatch.setattr(config, "gym_drive_connect_active_for", lambda base: True)
    monkeypatch.setattr(gym_media_index, "default_store", lambda: MediaStore())
    assert cif.real_media_depleted("gymx") is False


def test_gym_astra_brief_never_inherits_lasso_color_or_footer(monkeypatch):
    from agent import astra_prompt

    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM", "false")
    brief = astra_prompt.build_infographic_brief(
        "Train with confidence", ["Coached small group training"],
        account_key="gymx_ig", gym_palette={"colors": GYMX_BRAND_COLORS})
    assert "for this gym's own brand" in brief
    assert "BRAND COLORS, VERIFIED FOR THIS GYM" in brief
    assert all(color in brief for color in GYMX_BRAND_COLORS)
    assert "URL FOOTER TEXT" not in brief
    assert "LASSOFRAMEWORK.COM" not in brief
    assert "red is used exactly one time" not in brief


def test_existing_days_rechecked_at_generation_time(monkeypatch):
    """A day another lane filled between the scan and the render is skipped."""
    _sources()
    _stub_pipeline(monkeypatch)
    _arm_astra(monkeypatch, 200, _astra_body())
    calls = {"n": 0}
    real = cif._empty_upcoming_days

    def _flip(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(*a, **k)         # the scan: day is empty
        return []                        # generation-time recheck: now filled

    monkeypatch.setattr(cif, "_empty_upcoming_days", _flip)
    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00", days_ahead=1, max_per_run=1)
    assert calls["n"] >= 2
    assert out["filled"] == 0 and store.inserted == []


def test_photo_arriving_after_render_holds_before_insert(monkeypatch):
    """The final pre-insert guard wins over a card rendered moments earlier."""
    _sources()
    _stub_pipeline(monkeypatch)
    _arm_astra(monkeypatch, 200, _astra_body())
    calls = {"n": 0}

    def _flip(base, *, now=None):
        calls["n"] += 1
        return calls["n"] <= 2  # scan + pre-render pass; pre-insert fails

    monkeypatch.setattr(cif, "real_media_depleted", _flip)
    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00", days_ahead=1, max_per_run=1)
    assert calls["n"] >= 3
    assert out["filled"] == 0 and store.inserted == []


def test_slot_taken_after_render_holds_before_insert(monkeypatch):
    """A competing feed row wins even after Astra has already rendered."""
    _sources()
    _stub_pipeline(monkeypatch)
    _arm_astra(monkeypatch, 200, _astra_body())
    real = cif._empty_upcoming_days
    calls = {"n": 0}

    def _flip(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs) if calls["n"] <= 2 else []

    monkeypatch.setattr(cif, "_empty_upcoming_days", _flip)
    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00", days_ahead=1, max_per_run=1)
    assert calls["n"] >= 3
    assert out["filled"] == 0 and store.inserted == []
