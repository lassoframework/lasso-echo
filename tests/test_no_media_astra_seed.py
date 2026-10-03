"""
no_media_astra_seed (agent/no_media_astra_seed.py), fully offline.

Blake 2026-09-11: a gym with ZERO real media AND no approved sources gets
Echo-generated Astra infographics unique to it, scraped from its own website +
Instagram (gym_deep_brain), instead of sitting on the generic sample month
forever. Asserts: flag gate, no-op with no facts, one-scrape-per-gym marking,
gap detection reuse, per-run cap, PENDING insert-only rows, and that a scrape
or generate failure never raises.
"""

import hashlib
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import no_media_astra_seed as nmas  # noqa: E402
from agent import config, db  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(lib_dir), raising=False)
    monkeypatch.setenv("AGENT_NO_MEDIA_ASTRA_SEED", "true")
    monkeypatch.setenv("AGENT_GYM_DEEP_BRAIN", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    # 733da2b: depletion may only be inferred from an indexed Drive source that
    # finished a successful sync and indexed zero assets. These gyms have no
    # Drive media at all, so the fixture proves exactly that.
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


def _acct():
    return Account(key="chateau_ig", display_name="CrossFit Chateau",
                  platform=Platform.INSTAGRAM, token_env="T", target_id_env="G")


class _Fact:
    def __init__(self, text, source_url="https://crossfitchateau.com/about"):
        self.text = text
        self.source_url = source_url
        self.category = "service"


class _Store:
    def __init__(self, rows=()):
        self._rows = [dict(r) for r in rows]
        self.inserted = []

    def list_month(self, base, month):
        return [dict(r) for r in self._rows
                if str(r.get("post_date", "")).startswith(month)]

    def insert_rows(self, base, rows):
        self.inserted.extend(rows)
        return rows


class _AstraResult:
    engine = "astra"
    model = "gpt-image-test"

    def __init__(self):
        self.image_bytes = b"\x89PNG_fake_card_bytes"


def _stub_pipeline(monkeypatch, palette=None):
    """Stub the Astra-only render path: verified gym palette on file, the
    shared Astra-only generator, and hosting. creative_studio is deliberately
    NOT stubbed -- the seed must never touch it."""
    from agent import astra_prompt, client_infographic_fill, media_host

    monkeypatch.setattr(astra_prompt, "load_gym_brand_palette",
                        lambda key: palette if palette is not None else {
                            "canvas": "#111111", "ink": "#FFFFFF",
                            "accent": "#C8102E"})
    monkeypatch.setattr(client_infographic_fill, "_generate_astra_only",
                        lambda prompt, opts, **kw: _AstraResult())
    monkeypatch.setattr(media_host, "host_media",
                        lambda path, key: f"https://r2/{os.path.basename(path)}")


def _stub_deep_brain(monkeypatch, facts):
    from agent import gym_deep_brain

    def _fake_build(base, **kw):
        return {"ok": True, "base": base, "facts": facts}
    monkeypatch.setattr(gym_deep_brain, "build_deep_brain", _fake_build)


def test_flag_off_is_noop(monkeypatch):
    monkeypatch.setenv("AGENT_NO_MEDIA_ASTRA_SEED", "false")
    _stub_deep_brain(monkeypatch, [_Fact("Real fact about Chateau")])
    store = _Store()
    n = nmas.seed_gaps("chateau", _acct(), store)
    assert n == 0
    assert store.inserted == []


def test_no_facts_is_noop(monkeypatch):
    def _blocked(base, **kw):
        return {"ok": False, "blocked": True, "base": base, "reason": "no domain on record"}
    from agent import gym_deep_brain
    monkeypatch.setattr(gym_deep_brain, "build_deep_brain", _blocked)
    store = _Store()
    n = nmas.seed_gaps("chateau", _acct(), store)
    assert n == 0
    assert store.inserted == []


def test_generates_grounded_pending_rows(monkeypatch):
    _stub_pipeline(monkeypatch)
    _stub_deep_brain(monkeypatch, [
        _Fact("Small group coaching built for beginners who never touched a barbell"),
        _Fact("Free childcare during every morning class"),
    ])
    store = _Store()
    n = nmas.seed_gaps("chateau", _acct(), store, max_rows=2, days_ahead=5)
    assert n == 2
    assert len(store.inserted) == 2
    for row in store.inserted:
        assert row["status"] == "pending"
        assert row["gym_id"] == "chateau"
        assert row["image_url"].startswith("https://r2/")
        assert row["caption"]
        # client-safe-review mark: real, visible, distinct from an ordinary
        # pending draft -- a reviewer scanning the queue can tell this one is
        # machine-scraped, not human-authored/approved.
        assert row["pillar"] == nmas.NEEDS_CLIENT_SAFE_REVIEW_PILLAR
        assert row["pillar"] != "pending"


def test_global_visual_writer_guard_stamps_truthful_same_object_source(monkeypatch):
    """Hosted seed PNG is the exact rendered object, so guard-on rows identify
    the hosted URL as both delivered image and raw source."""
    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    _stub_pipeline(monkeypatch)
    _stub_deep_brain(monkeypatch, [_Fact("Real fact about Chateau")])
    store = _Store()

    assert nmas.seed_gaps("chateau", _acct(), store, max_rows=1,
                          days_ahead=1) == 1
    row = store.inserted[0]
    assert row["source_media_url"] == row["image_url"]


def test_global_visual_writer_guard_off_preserves_legacy_seed_payload(monkeypatch):
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    _stub_pipeline(monkeypatch)
    _stub_deep_brain(monkeypatch, [_Fact("Real fact about Chateau")])
    store = _Store()

    assert nmas.seed_gaps("chateau", _acct(), store, max_rows=1,
                          days_ahead=1) == 1
    assert "source_media_url" not in store.inserted[0]


def test_seed_output_passes_prepared_writer_as_same_object(monkeypatch):
    """Exercise the real prepared-row boundary on a guard-on Astra seed, with
    exact hosted bytes and owner receipt stubs. The configured gym palette still
    passes through the brief builder before that row is prepared."""
    from agent import astra_prompt, client_infographic_fill, media_host
    from agent import visual_owner_receipts, visual_writer_prepare as prep

    monkeypatch.setenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", "true")
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://r2")
    palette = {"canvas": "#112233", "ink": "#FFFFFF", "accent": "#C8102E"}
    _stub_pipeline(monkeypatch, palette=palette)
    brief_seen = {}
    monkeypatch.setattr(astra_prompt, "build_infographic_brief",
                        lambda headline, facts, **kw: brief_seen.update(
                            palette=kw["gym_palette"]) or "verified brief")
    _stub_deep_brain(monkeypatch, [_Fact("Real fact about Chateau")])
    monkeypatch.setattr(media_host, "host_media",
                        lambda path, key: "https://r2/no_media_test.png")

    store = _Store()
    assert nmas.seed_gaps("chateau", _acct(), store, max_rows=1,
                          days_ahead=1) == 1
    candidate = store.inserted[0]
    assert brief_seen["palette"] == palette
    assert candidate["source_media_url"] == candidate["image_url"]

    exact_bytes = _AstraResult().image_bytes
    digest = "md5:" + hashlib.md5(exact_bytes).hexdigest()
    receipt_id = str(uuid.uuid4())
    receipt_calls = []

    class Response:
        status_code = 200

        def __init__(self, value):
            self.value = value

        def json(self):
            return self.value

    class HTTP:
        def get(self, url, *, params, headers, timeout):
            if url.endswith("tenant_alias"):
                key = params["alias_key"][3:]
                return Response([{"alias_key": key,
                                  "tenant_id": "11111111-1111-4111-8111-111111111111"}])
            if url.endswith("visual_group_alias"):
                return Response([{"group_key": "vg_seed_card"}])
            raise AssertionError(f"unexpected lookup: {url}")

        def post(self, url, *, headers, json, timeout):
            assert url.endswith("visual_global_prepare_source_rendition")
            assert json["p_source_read_receipt"] == receipt_id
            assert json["p_delivered_read_receipt"] == receipt_id
            assert json["p_render_receipt"] is None
            return Response({"group_key": "vg_seed_card",
                             "source_fingerprint": digest,
                             "delivered_fingerprint": digest,
                             "usage_claimed": False})

    class PreparedStore:
        def _client(self):
            return http

        @staticmethod
        def _rest(path):
            return "https://db.example/rest/v1/" + path

        @staticmethod
        def _headers(extra=None):
            return extra or {}

    http = HTTP()
    monkeypatch.setattr(prep, "_bytes_for_url", lambda url: exact_bytes)

    def owner_writer(**kwargs):
        receipt_calls.append(kwargs)
        return {"read_receipt": receipt_id, "render_receipt": None}

    monkeypatch.setattr(visual_owner_receipts, "default_same_object_writer",
                        lambda: owner_writer)
    prepared = prep.prepare(PreparedStore(), "chateau_ig", candidate)
    assert prepared["source_media_url"] == prepared["image_url"]
    assert prepared["visual_group_key"] == "vg_seed_card"
    assert prepared["byte_hash"] == "derived:" + digest
    assert len(receipt_calls) == 1
    assert receipt_calls[0]["exact_bytes"] == exact_bytes
    assert receipt_calls[0]["evidence"]["exact_url"] == candidate["image_url"]


def test_needs_client_safe_review_pillar_is_never_a_known_taxonomy_pillar():
    """The marker must not collide with any real pillar list content/day-shape
    logic checks against -- otherwise a scraped fallback card could silently
    get treated as ordinary proof/invitation content."""
    from agent import content_categories, day_shape
    marker = nmas.NEEDS_CLIENT_SAFE_REVIEW_PILLAR
    assert marker not in content_categories.GYM_PILLARS
    assert marker not in day_shape.PROOF_PILLARS
    assert marker not in day_shape.INVITATION_PILLARS


def test_existing_active_day_is_skipped(monkeypatch):
    """Gap detection reuses client_infographic_fill's _empty_upcoming_days: a day
    with an existing active IG feed row is never touched.

    PINS `today` and derives 'tomorrow' from THAT SAME value, both passed
    through to seed_gaps -- this used to compute 'tomorrow' from the test
    process's naive datetime.date.today() (implicitly UTC/system-local) while
    seed_gaps resolves 'today' through _local_now(tz_name) (the gym's posting
    timezone). Those two disagree on which calendar date is 'tomorrow'
    whenever the wall clock sits near a timezone boundary (e.g. late evening
    UTC is already the next gym-local day), producing a flaky off-by-one that
    depends on what time of day the test happens to run -- exactly what
    tripped CI here, unrelated to any change in this PR."""
    _stub_pipeline(monkeypatch)
    _stub_deep_brain(monkeypatch, [_Fact("Real fact about Chateau")])
    from datetime import datetime, timedelta, timezone
    pinned_now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    tomorrow = (pinned_now + timedelta(days=1)).date().isoformat()
    store = _Store(rows=[{"post_date": tomorrow, "format": "feed",
                         "account": "instagram", "status": "pending"}])
    n = nmas.seed_gaps("chateau", _acct(), store, max_rows=1, days_ahead=1,
                       today=pinned_now)
    assert n == 0
    assert store.inserted == []


def test_scrapes_once_per_gym(monkeypatch):
    """A second call reuses the pending sources already landed instead of
    re-scraping (kv marker set by the first call)."""
    _stub_pipeline(monkeypatch)
    calls = {"n": 0}
    from agent import gym_deep_brain

    def _counting_build(base, **kw):
        calls["n"] += 1
        return {"ok": True, "base": base,
               "facts": [_Fact("Real fact about Chateau")]}
    monkeypatch.setattr(gym_deep_brain, "build_deep_brain", _counting_build)
    from agent import client_sources
    monkeypatch.setattr(client_sources, "pending_sources",
                        lambda base, category=None: [_Fact("Real fact about Chateau")])

    store1 = _Store()
    nmas.seed_gaps("chateau", _acct(), store1, max_rows=1, days_ahead=1)
    store2 = _Store()
    nmas.seed_gaps("chateau", _acct(), store2, max_rows=1, days_ahead=2)
    assert calls["n"] == 1


def test_generate_failure_never_raises(monkeypatch):
    _stub_pipeline(monkeypatch)
    _stub_deep_brain(monkeypatch, [_Fact("Real fact about Chateau")])
    from agent import client_infographic_fill

    def _none(*a, **k):
        return None  # Astra failed on every attempt -> NEEDS HUMAN, day held
    monkeypatch.setattr(client_infographic_fill, "_generate_astra_only", _none)
    store = _Store()
    n = nmas.seed_gaps("chateau", _acct(), store, max_rows=1, days_ahead=1)
    assert n == 0
    assert store.inserted == []


def test_missing_verified_palette_fails_closed(monkeypatch):
    """No verified gym brand colors on file -> seed nothing, never a generic
    or LASSO palette (2026-10-02 ruling)."""
    _stub_pipeline(monkeypatch, palette=None)
    _stub_deep_brain(monkeypatch, [_Fact("Real fact about Chateau")])
    from agent import astra_prompt
    monkeypatch.setattr(astra_prompt, "load_gym_brand_palette", lambda key: None)
    store = _Store()
    n = nmas.seed_gaps("chateau", _acct(), store, max_rows=1, days_ahead=1)
    assert n == 0
    assert store.inserted == []


def test_account_gym_mismatch_fails_closed_before_deep_brain(monkeypatch):
    """A seed never borrows another tenant's account, voice, or palette."""
    from agent import gym_deep_brain

    monkeypatch.setattr(gym_deep_brain, "build_deep_brain",
                        lambda *args, **kwargs: pytest.fail("must not scrape"))
    account = Account(key="othergym_ig", display_name="Other Gym",
                      platform=Platform.INSTAGRAM, token_env="T", target_id_env="G")
    store = _Store()
    assert nmas.seed_gaps("chateau", account, store) == 0
    assert store.inserted == []


def test_astra_only_with_gym_palette_in_brief(monkeypatch):
    """The render must go through the Astra-only generator and the brief must
    carry the gym's OWN verified palette -- creative_studio.generate (the
    Gemini/generic lane) must never be reached."""
    from agent import astra_prompt, client_infographic_fill, creative_studio
    seen = {}

    def _fake_brief(headline, facts, **kw):
        seen["gym_palette"] = kw.get("gym_palette")
        return "BRIEF:" + headline
    monkeypatch.setattr(astra_prompt, "build_infographic_brief", _fake_brief)
    monkeypatch.setattr(astra_prompt, "load_gym_brand_palette",
                        lambda key: {"canvas": "#0A1B2C", "accent": "#C8102E"})

    def _no_gemini(*a, **k):
        raise AssertionError("creative_studio.generate must not be called")
    monkeypatch.setattr(creative_studio, "generate", _no_gemini)

    monkeypatch.setattr(client_infographic_fill, "_generate_astra_only",
                        lambda prompt, opts, **kw: _AstraResult())
    _stub_deep_brain(monkeypatch, [_Fact("Real fact about Chateau")])
    from agent import media_host
    monkeypatch.setattr(media_host, "host_media",
                        lambda path, key: f"https://r2/{os.path.basename(path)}")
    store = _Store()
    n = nmas.seed_gaps("chateau", _acct(), store, max_rows=1, days_ahead=1)
    assert n == 1
    assert seen["gym_palette"] == {"canvas": "#0A1B2C", "accent": "#C8102E"}


def test_media_depletion_rechecked_before_each_render(monkeypatch):
    """An approved photo landing mid-run wins: the second render is held."""
    _stub_pipeline(monkeypatch)
    _stub_deep_brain(monkeypatch, [
        _Fact("Real fact about Chateau"),
        _Fact("Another real fact about Chateau"),
    ])
    from agent import client_infographic_fill
    calls = {"n": 0}

    def _depleted(base, *, now=None):
        calls["n"] += 1
        return calls["n"] <= 1  # True at the gate, False at the first render
    monkeypatch.setattr(client_infographic_fill, "real_media_depleted",
                        _depleted)
    store = _Store()
    n = nmas.seed_gaps("chateau", _acct(), store, max_rows=2, days_ahead=5)
    assert n == 0
    assert store.inserted == []


def test_photo_arriving_after_seed_render_holds_before_insert(monkeypatch):
    """A card cannot enter the calendar after a new client photo is known."""
    _stub_pipeline(monkeypatch)
    _stub_deep_brain(monkeypatch, [_Fact("Real fact about Chateau")])
    from agent import client_infographic_fill
    calls = {"n": 0}

    def _depleted(base, *, now=None):
        calls["n"] += 1
        return calls["n"] <= 2  # initial + pre-render; post-render fails

    monkeypatch.setattr(client_infographic_fill, "real_media_depleted", _depleted)
    store = _Store()
    assert nmas.seed_gaps("chateau", _acct(), store, max_rows=1, days_ahead=1) == 0
    assert calls["n"] >= 3
    assert store.inserted == []


def test_slot_taken_after_seed_render_holds_before_insert(monkeypatch):
    """A competing calendar writer wins a seed slot after the render."""
    _stub_pipeline(monkeypatch)
    _stub_deep_brain(monkeypatch, [_Fact("Real fact about Chateau")])
    from agent import client_infographic_fill
    real = client_infographic_fill._empty_upcoming_days
    calls = {"n": 0}

    def _empty(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs) if calls["n"] == 1 else []

    monkeypatch.setattr(client_infographic_fill, "_empty_upcoming_days", _empty)
    store = _Store()
    assert nmas.seed_gaps("chateau", _acct(), store, max_rows=1, days_ahead=1) == 0
    assert calls["n"] >= 2
    assert store.inserted == []


def test_deep_brain_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("AGENT_GYM_DEEP_BRAIN", "false")
    store = _Store()
    n = nmas.seed_gaps("chateau", _acct(), store)
    assert n == 0
    assert store.inserted == []
