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

        def list_sources(self, _base, include_inactive=False):
            return [{"id": "src1", "gym_id": _base, "kind": "gym_drive", "active": True,
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


def test_fills_empty_days_holds_until_trusted_receipt_authority(monkeypatch):
    """GENERATED-CLIENT CONTRACT safety repair (2026-10-09): every card this
    lane builds is a generated card, and NO trusted hosted-byte receipt
    writer/lookup exists. fill_gaps must render the cards, then HOLD the whole
    batch pre-insert: filled == 0, zero rows written, insert-only preserved.
    The row-shape assertions from the pre-hold version of this test resume
    only when a real trusted receipt authority is deployed."""
    _sources()
    _stub_pipeline(monkeypatch)
    seen = _arm_astra(monkeypatch, 200, _astra_body())
    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00")
    assert out["ok"] is True and out["filled"] == 0, out
    assert "generated cards held" in (out.get("reason") or ""), out
    assert store.inserted == [], "no generated row may reach the calendar"
    assert store.deleted == [], "fill must be INSERT-only"
    assert len(seen) == cif.FILL_MAX_PER_RUN, "cards are still rendered before the hold"


def test_review_mark_survives_and_insert_is_held(monkeypatch):
    """PRODUCTION INCIDENT (2026-09-11) regression, two halves after the
    2026-10-09 generated-contract safety repair:
    (a) _with_review_mark still stamps the client-safe review SUFFIX on the
        source's real taxonomy category -- the direct regression for the fix at
        agent/client_infographic_fill.py's Draft(... category=_with_review_mark(...)).
    (b) The full fill path now HOLDS every generated card pre-insert (no
        trusted hosted-byte receipt authority exists), so the per-inserted-row
        mark assertion is superseded by: NO row is inserted at all. When a real
        trusted writer exists, reinstate per-row mark assertions on inserts."""
    suffix = cif._NEEDS_CLIENT_SAFE_REVIEW_SUFFIX
    for base in ("educational", "service"):
        marked = cif._with_review_mark(base)
        assert marked.endswith(suffix)
        assert marked[: -len(suffix)] == base
    _sources()
    _stub_pipeline(monkeypatch)
    _arm_astra(monkeypatch, 200, _astra_body())
    store = _Store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00")
    assert out["ok"] is True and out["filled"] == 0
    assert store.inserted == []


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
    rows = [{"post_date": "2026-08-26", "format": "feed", "account": "instagram",
             "status": "denied"}]
    store = _Store(rows)
    seen = _arm_astra(monkeypatch, 200, _astra_body())
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00", days_ahead=1, max_per_run=1)
    assert len(seen) == 1, "the denied day counts as empty: a card was rendered for it"
    # generated-contract hold: rendered but never inserted (safety repair)
    assert out["filled"] == 0 and store.inserted == []


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
    # Astra still draws the card; the generated-contract hold then blocks insert.
    assert len(seen) == 1
    assert out["filled"] == 0 and store.inserted == []
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
    assert out["filled"] == 0 and store.inserted == []  # held: no trusted receipt authority
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
    assert out["filled"] == 0 and store.inserted == []  # held: no trusted receipt authority


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

        def list_sources(self, _base, include_inactive=False):
            return [{"id": "src1", "gym_id": _base, "kind": "gym_drive", "active": True,
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


def test_astra_gate_holds_when_drive_claim_read_fails_with_candidate(monkeypatch):
    from agent import db, gym_media_index
    from tests.gym_media_fakes import FakeMediaStore, make_asset

    class ReadyStore(FakeMediaStore):
        def list_sources(self, _base, include_inactive=False):
            return [{"id": "src1", "gym_id": _base, "kind": "gym_drive", "active": True,
                     "sync_status": "ready", "sync_finished_at": "2026-10-02T00:00:00Z"}]

    media_store = ReadyStore(assets=[make_asset("photo", gym_id="gymx")])
    monkeypatch.setattr(gym_media_index, "default_store", lambda: media_store)
    monkeypatch.setattr(db, "drive_asset_claimed_ids",
                        lambda _base: (_ for _ in ()).throw(OSError("claim DB down")))
    assert cif.real_media_depleted("gymx", now="2026-08-25T12:00:00-04:00") is False


def test_astra_gate_holds_when_legacy_claim_hash_cannot_be_mapped(monkeypatch):
    from agent import db, gym_media_index
    from tests.gym_media_fakes import FakeMediaStore, make_asset

    class ReadyStore(FakeMediaStore):
        def list_sources(self, _base, include_inactive=False):
            return [{"id": "src1", "gym_id": _base, "kind": "gym_drive", "active": True,
                     "sync_status": "ready", "sync_finished_at": "2026-10-02T00:00:00Z"}]

    monkeypatch.setattr(gym_media_index, "default_store",
                        lambda: ReadyStore(assets=[make_asset("photo", gym_id="gymx")]))
    monkeypatch.setattr(db, "drive_asset_claimed_ids", lambda _base: {"missing-legacy"})
    assert cif.real_media_depleted("gymx", now="2026-08-25T12:00:00-04:00") is False


def test_unsynced_drive_source_holds_the_infographic_fallback(monkeypatch):
    """2026-10-02 regression (733da2b): an empty asset list is depletion proof only
    after a successful source sync. A source still syncing (or never finished) must
    fail CLOSED and hold the Astra fallback."""
    from agent import gym_media_index

    class UnsyncedDriveIndex:
        def available(self):
            return True

        def list_sources(self, _base, include_inactive=False):
            return [{"id": "src1", "gym_id": _base, "kind": "gym_drive", "active": True,
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


def test_never_connected_empty_drive_source_list_allows_last_resort(monkeypatch):
    """A successful empty source read is authoritative proof of no Drive supply."""
    from agent import gym_media_index

    class NeverConnectedIndex:
        def available(self):
            return True

        def list_sources(self, _base, include_inactive=False):
            return []

        def list_assets(self, _base):
            raise AssertionError("no source means there is no asset inventory to read")

    monkeypatch.setattr(gym_media_index, "default_store",
                        lambda: NeverConnectedIndex())
    assert cif.real_media_depleted(
        "gymx", now="2026-08-25T12:00:00-04:00") is True


def test_connected_drive_with_no_source_row_holds_until_sync_proof(monkeypatch):
    from agent import gym_media_index

    class MissingSourceIndex:
        def available(self):
            return True
        def list_sources(self, _base, include_inactive=False):
            return []

    monkeypatch.setattr(config, "gym_drive_connect_active_for", lambda _base: True)
    monkeypatch.setattr(gym_media_index, "default_store",
                        lambda: MissingSourceIndex())
    assert cif.real_media_depleted(
        "gymx", now="2026-08-25T12:00:00-04:00") is False


def test_non_drive_source_rows_still_allow_never_connected_fallback(monkeypatch):
    from agent import gym_media_index

    class NonDriveIndex:
        def available(self):
            return True
        def list_sources(self, _base, include_inactive=False):
            return [{"kind": "portal_upload", "active": True}]
        def list_assets(self, _base):
            raise AssertionError("no Drive connection means no Drive inventory read")

    monkeypatch.setattr(config, "gym_drive_connect_active_for", lambda _base: False)
    monkeypatch.setattr(gym_media_index, "default_store", lambda: NonDriveIndex())
    assert cif.real_media_depleted(
        "gymx", now="2026-08-25T12:00:00-04:00") is True


def test_drive_source_read_failure_still_holds_last_resort(monkeypatch):
    from agent import gym_media_index

    class BrokenSourceIndex:
        def available(self):
            return True

        def list_sources(self, _base, include_inactive=False):
            raise OSError("inventory unavailable")

    monkeypatch.setattr(gym_media_index, "default_store",
                        lambda: BrokenSourceIndex())
    assert cif.real_media_depleted(
        "gymx", now="2026-08-25T12:00:00-04:00") is False


def test_photos_rechecked_at_generation_time(monkeypatch):
    """A photo landing between the scan and the render always wins: the fill
    stops instead of drawing an infographic."""
    _sources()
    _stub_pipeline(monkeypatch)
    _arm_astra(monkeypatch, 200, _astra_body())
    calls = {"n": 0}
    real = cif.real_media_status

    def _flip(base, *, now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(base, now=now)   # the top-of-scan check: still depleted
        return (cif.MEDIA_AVAILABLE, "photos arrived")  # recheck: photos arrived

    monkeypatch.setattr(cif, "real_media_status", _flip)
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
        if calls["n"] <= 2:     # scan + pre-render pass; pre-insert fails
            return (cif.MEDIA_DEPLETED, "proven empty")
        return (cif.MEDIA_AVAILABLE, "photos arrived")

    monkeypatch.setattr(cif, "real_media_status", _flip)
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


TENANT = "11111111-1111-4111-8111-111111111111"


def _astra_bytes():
    """The EXACT bytes Astra returns for _astra_body() -- the bytes fill_gaps
    writes to disk and media_host uploads. The hosted object IS this byte
    string; anything else served at the row URL is a failed attestation."""
    import base64 as _b64
    import json as _json
    return _b64.b64decode(_json.loads(_astra_body())["output"][0]["result"])


class _CalendarHTTP:
    """Production-shaped PostgREST double for the REAL SupabaseCalendarStore.

    fill_gaps drives store.list_month / store.insert_rows over HTTP exactly as
    production does: the insert goes through every insert_rows stage belt and,
    with the guard armed, through visual_writer_prepare's owner-receipt RPCs
    BEFORE the content_calendar POST. Calls are recorded in order so the tests
    can assert preparation-RPC-before-insert ordering. Never touches the
    network."""

    def __init__(self, tenant):
        self.tenant = tenant
        self.calls = []                    # ordered ("get"|"post", endpoint)
        self.registered = False            # raw source registered via bundle RPC
        self.digest = None                 # fingerprint the bundle RPC attested
        self.insert_payloads = []          # content_calendar POST bodies
        self.rendition_args = []
        self.bundle_args = []

    class _Resp:
        status_code = 200

        def __init__(self, value):
            self.value = value
            self.headers = {"Content-Range": f"0-{max(0, len(value) - 1)}/{len(value)}"
                            if isinstance(value, list) else ""}

        def json(self):
            return self.value

        @property
        def text(self):
            return ""

    def get(self, url, *, params, headers, timeout):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append(("get", endpoint))
        if endpoint == "tenant_alias":
            alias = params["alias_key"]
            assert alias.startswith("eq."), alias
            return self._Resp([{"alias_key": alias[3:],
                                "tenant_id": self.tenant}])
        if endpoint == "visual_group_alias":
            if not self.registered:
                return self._Resp([])
            return self._Resp([{"group_key": "vg_same"}])
        # content_calendar / support_tickets reads: this gym's calendar is empty
        return self._Resp([])

    def post(self, url, *, headers, json, timeout):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append(("post", endpoint))
        if endpoint == "visual_global_prepare_bundle":
            self.bundle_args.append(json)
            self.digest = json["p_fingerprint"]
            self.registered = True
            return self._Resp({"group_key": "vg_same",
                               "fingerprint": self.digest})
        if endpoint == "visual_global_prepare_source_rendition":
            self.rendition_args.append(json)
            return self._Resp({"group_key": "vg_same",
                               "source_fingerprint": self.digest,
                               "delivered_fingerprint": self.digest,
                               "usage_claimed": False})
        if endpoint == "content_calendar":
            self.insert_payloads.append(json)
            import uuid as _uuid
            return self._Resp([dict(row, id=str(_uuid.uuid4()))
                               for row in json])
        raise AssertionError(f"unexpected POST: {url}")


def _production_store(tenant=TENANT):
    """The real SupabaseCalendarStore over the scripted HTTP double."""
    from agent import portal_calendar_store as pcs

    http = _CalendarHTTP(tenant)
    return pcs.SupabaseCalendarStore(url="https://db.example",
                                     service_key="test", http=http), http


def _stub_pipeline_hosting(monkeypatch):
    """Offline Astra + hosting where host_media serves the EXACT bytes written
    to disk (the real lane uploads the rendered file untransformed), so the
    served-byte reader can be bound to the actual hosted object."""
    from agent import creative_studio, media_host

    class _Client:
        def generate_image(self, prompt, model):
            return b"\x89PNG_fake_card_bytes"
    monkeypatch.setattr(creative_studio, "_default_client", lambda: _Client())
    monkeypatch.setattr(creative_studio, "_render_with_timeout", lambda fn: fn())
    served = {}

    def _host(path, key):
        with open(path, "rb") as fh:
            data = fh.read()
        url = f"https://r2/{os.path.basename(path)}"
        served[url] = data
        return url
    monkeypatch.setattr(media_host, "host_media", _host)
    return served


def test_guard_on_fill_held_until_trusted_receipt_authority(monkeypatch):
    """AGENT_VISUAL_GLOBAL_WRITER_PREP, production shape -- superseded by the
    2026-10-09 generated-contract safety repair. fill_gaps renders and hosts
    the Astra card through the SAME production pipeline, but the pre-insert
    fail-closed hold (no trusted hosted-byte receipt authority) stops the
    batch BEFORE the content_calendar POST: zero insert payloads, zero
    preparation RPCs. The hosted bytes still land in the serving stub, so the
    same-object identity property (served == written) stays asserted. The
    guarded-insert byte-proof assertions from the prior version resume only
    when a real trusted receipt authority is deployed."""
    from agent import visual_owner_receipts as owner
    import uuid

    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://r2", raising=False)
    _sources()
    served = _stub_pipeline_hosting(monkeypatch)
    _arm_astra(monkeypatch, 200, _astra_body())
    receipt_observations = []
    monkeypatch.setattr(owner, "default_same_object_writer",
                        lambda: lambda **kw: receipt_observations.append(kw)
                        or {"read_receipt": str(uuid.uuid4()), "render_receipt": None})
    store, http = _production_store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00", days_ahead=1, max_per_run=1)
    assert out["ok"] is True and out["filled"] == 0, out
    assert "generated cards held" in (out.get("reason") or ""), out
    assert served, "fill_gaps must have hosted its card before the hold"
    assert all(data == _astra_bytes() for data in served.values())
    assert http.insert_payloads == [], http.calls
    assert not http.rendition_args and not http.bundle_args, http.calls


def test_guard_off_fill_held_until_trusted_receipt_authority(monkeypatch):
    """Guard OFF through the SAME production-shaped store: the legacy row
    shape never reaches content_calendar either, because the generated-contract
    hold stops the batch pre-insert. The legacy insert-shape assertions resume
    when a real trusted receipt authority exists."""
    _sources()
    _stub_pipeline_hosting(monkeypatch)
    _arm_astra(monkeypatch, 200, _astra_body())
    store, http = _production_store()
    out = cif.fill_gaps("gymx", _acct(), store, voice=_voice(),
                        now="2026-08-25T12:00:00-04:00", days_ahead=1, max_per_run=1)
    assert out["ok"] is True and out["filled"] == 0, out
    assert http.insert_payloads == [], http.calls
    assert not any("visual_global" in ep for _m, ep in http.calls), http.calls


# ---- photo-first tri-state depletion gate (2026-10-06) ------------------------

NOW_ISO = "2026-08-25T12:00:00-04:00"


def _ready_drive_store(assets=(), asset_source_id="src1"):
    """A Drive index with one ready same-gym source (src1) and the given assets."""
    from tests.gym_media_fakes import FakeMediaStore

    class ReadyStore(FakeMediaStore):
        def list_sources(self, _base, include_inactive=False):
            return [{"id": "src1", "gym_id": _base, "kind": "gym_drive",
                     "active": True, "sync_status": "ready",
                     "sync_finished_at": "2026-10-02T00:00:00Z"}]

    return ReadyStore(sources=[{"id": "src1", "gym_id": "gymx"}],
                      assets=list(assets))


def test_media_status_available_when_eligible_photo_pickable(monkeypatch):
    """Photo-first: an eligible same-gym photo is AVAILABLE, never a fallback case."""
    from agent import gym_media_index
    from tests.gym_media_fakes import make_asset

    store = _ready_drive_store(assets=[make_asset("ph1", gym_id="gymx")])
    monkeypatch.setattr(gym_media_index, "default_store", lambda: store)
    status, detail = cif.real_media_status("gymx", now=NOW_ISO)
    assert status == cif.MEDIA_AVAILABLE, detail
    assert cif.real_media_depleted("gymx", now=NOW_ISO) is False


def test_media_status_uncertain_when_exclusion_rests_on_stale_source_id(monkeypatch):
    """A usable, never-used photo kept out of the pool ONLY because its
    media_source link is stale/unverifiable is unknown provenance: the gate must
    report UNCERTAIN, never claim the photos are exhausted, and never mark the
    asset a repeat."""
    from agent import gym_media_index
    from tests.gym_media_fakes import make_asset

    photo = make_asset("ph1", gym_id="gymx", source_id="stale-src")
    store = _ready_drive_store(assets=[photo])
    monkeypatch.setattr(gym_media_index, "default_store", lambda: store)
    status, detail = cif.real_media_status("gymx", now=NOW_ISO)
    assert status == cif.MEDIA_UNCERTAIN
    assert "stale or unverifiable" in detail
    assert cif.real_media_depleted("gymx", now=NOW_ISO) is False


def test_media_status_depleted_only_for_proven_repeats(monkeypatch):
    """A photo proven used by THIS gym's own counters is a genuine repeat; with
    nothing else usable the depletion claim is allowed."""
    from agent import gym_media_index
    from tests.gym_media_fakes import make_asset

    photo = make_asset("ph1", gym_id="gymx", used_count=1,
                       last_used_at="2026-08-20T00:00:00Z")
    store = _ready_drive_store(assets=[photo])
    monkeypatch.setattr(gym_media_index, "default_store", lambda: store)
    status, detail = cif.real_media_status("gymx", now=NOW_ISO)
    assert status == cif.MEDIA_DEPLETED, detail
    assert cif.real_media_depleted("gymx", now=NOW_ISO) is True


def test_media_status_uncertain_when_use_counters_unreadable(monkeypatch):
    """An unreadable used_count is unknown repeat-use provenance, not a repeat."""
    from agent import gym_media_index
    from tests.gym_media_fakes import make_asset

    photo = make_asset("ph1", gym_id="gymx")
    photo["used_count"] = "bogus"
    store = _ready_drive_store(assets=[photo])
    monkeypatch.setattr(gym_media_index, "default_store", lambda: store)
    status, detail = cif.real_media_status("gymx", now=NOW_ISO)
    assert status == cif.MEDIA_UNCERTAIN
    assert "counters are unreadable" in detail


def test_media_status_uncertain_when_usable_photo_claimed_in_flight(monkeypatch):
    """An in-flight claim reserves the photo; it is not exhaustion evidence."""
    from agent import db, gym_media_index
    from tests.gym_media_fakes import make_asset

    photo = make_asset("ph1", gym_id="gymx")
    store = _ready_drive_store(assets=[photo])
    monkeypatch.setattr(gym_media_index, "default_store", lambda: store)
    monkeypatch.setattr(db, "drive_asset_claimed_ids", lambda _base: {"ph1"})
    status, detail = cif.real_media_status("gymx", now=NOW_ISO)
    assert status == cif.MEDIA_UNCERTAIN
    assert "in-flight claim" in detail


def test_fill_gaps_holds_explicitly_when_inventory_uncertain(monkeypatch):
    """fill_gaps must surface the hold reason instead of proceeding or claiming
    'usable media available' when the inventory cannot be proven."""
    from agent import gym_media_index
    from tests.gym_media_fakes import make_asset

    photo = make_asset("ph1", gym_id="gymx", source_id="stale-src")
    store = _ready_drive_store(assets=[photo])
    monkeypatch.setattr(gym_media_index, "default_store", lambda: store)
    _sources()
    cal = _Store()
    out = cif.fill_gaps("gymx", _acct(), cal, voice=_voice(), now=NOW_ISO)
    assert out["ok"] is False and out["filled"] == 0 and out.get("held") is True, out
    assert "uncertain" in out["reason"]
    assert cal.inserted == []


def test_media_status_uncertain_for_malformed_hidden_same_byte_alias(monkeypatch):
    """A hidden alias with bad counters cannot prove an eligible photo was used."""
    from agent import gym_media_index
    from tests.gym_media_fakes import make_asset

    photo = make_asset("ph1", gym_id="gymx")
    alias = make_asset("ph2", gym_id="gymx")
    alias["content_hash"] = photo["content_hash"]
    alias["excluded_by_coach"] = True
    alias["used_count"] = "bogus"
    store = _ready_drive_store(assets=[photo, alias])
    monkeypatch.setattr(gym_media_index, "default_store", lambda: store)
    status, detail = cif.real_media_status("gymx", now=NOW_ISO)
    assert status == cif.MEDIA_UNCERTAIN, detail
    assert "same-byte alias" in detail


def test_fill_gaps_holds_when_generation_inventory_becomes_uncertain(monkeypatch):
    """A depleted first read cannot mask a later uncertain inventory read."""
    _sources()
    statuses = iter(((cif.MEDIA_DEPLETED, "proven empty"),
                     (cif.MEDIA_UNCERTAIN, "Drive read failed")))
    monkeypatch.setattr(cif, "real_media_status", lambda *_a, **_k: next(statuses))
    cal = _Store()
    out = cif.fill_gaps("gymx", _acct(), cal, voice=_voice(),
                        now=NOW_ISO, days_ahead=1, max_per_run=1)
    assert out["ok"] is False and out["filled"] == 0 and out["held"] is True
    assert "Drive read failed" in out["reason"]
    assert cal.inserted == []
