"""Behavioral logical_post_id coverage at the generated calendar insert boundary."""

import os
import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from agent import client_infographic_fill as infographic
from agent import no_media_astra_seed as no_media
from agent import config
from agent.accounts import Account, Platform


class _Store:
    def __init__(self):
        self.inserted = []

    def list_month(self, _base, _month):
        return []

    def insert_rows(self, _base, rows):
        self.inserted.extend(dict(row) for row in rows)
        return rows


class _GeneratedImage:
    engine = "astra"
    model = "test"
    image_bytes = b"png-test-bytes"


class _Fact:
    text = "Small group coaching helps beginners build strength with patient guidance"
    source_url = "https://example.test/about"


@pytest.fixture
def writer_environment(monkeypatch, tmp_path):
    """Keep the actual planners and row mapping, with only external services faked."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_INFOGRAPHIC_FILL", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.setenv("AGENT_NO_MEDIA_ASTRA_SEED", "true")
    monkeypatch.setenv("AGENT_GYM_DEEP_BRAIN", "true")
    monkeypatch.delenv("AGENT_VISUAL_GLOBAL_WRITER_PREP", raising=False)
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setattr(config, "LIBRARY_PATH", str(library), raising=False)

    from agent import astra_prompt, client_content, client_sources
    from agent import gym_media_index, gym_deep_brain, media_bridge, media_host
    from agent import post_quality

    voice_dir = tmp_path / "voice" / "gymx"
    voice_dir.mkdir(parents=True)
    (voice_dir / "brand_colors.json").write_text(
        '{"colors":["#112233","#FFFFFF","#C8102E"],'
        '"source_url":"https://example.test/brand"}')
    monkeypatch.setenv("AGENT_CLIENT_VOICE_DIR", str(tmp_path / "voice"))

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
    # Media inventory is outside this identity contract. Keep the caller's
    # rechecks in the real planner while providing a deterministic empty-media fact.
    monkeypatch.setattr(infographic, "real_media_status",
                         lambda *_a, **_k: (infographic.MEDIA_DEPLETED, "proven empty"))
    monkeypatch.setattr(astra_prompt, "build_infographic_brief", lambda *a, **k: "grounded test brief")
    monkeypatch.setattr(astra_prompt, "load_gym_brand_palette", lambda *_, **__: {
        "canvas": "#112233", "ink": "#FFFFFF", "accent": "#C8102E"})
    monkeypatch.setattr(infographic, "_generate_astra_only", lambda *a, **k: _GeneratedImage())
    monkeypatch.setattr(media_host, "host_media",
                        lambda path, _key: f"https://cdn.test/{os.path.basename(path)}")
    monkeypatch.setattr(client_content, "make_caption",
                        lambda *_a, **_k: (_Fact.text, ["#GymX"]))
    monkeypatch.setattr(post_quality, "post_issues", lambda _draft: [])
    monkeypatch.setattr(media_bridge, "episode", lambda *_a, **_k: None)
    monkeypatch.setattr(media_bridge, "notify_bridge", lambda *_a, **_k: None)
    monkeypatch.setattr(media_bridge, "retry_existing_notice", lambda *_a, **_k: None)

    def bridge_days(_base, *, now=None, days_ahead=2):
        today = datetime.fromisoformat(str(now).replace("Z", "+00:00")).date()
        return {(today + timedelta(days=i)).isoformat()
                for i in range(1, days_ahead + 1)}

    monkeypatch.setattr(media_bridge, "bridge_days", bridge_days)
    monkeypatch.setattr(gym_deep_brain, "build_deep_brain",
                        lambda _base: {"ok": True, "facts": [_Fact(), _Fact()]})

    # Use the real approved-source store for the infographic path.
    client_sources.add_source(
        "gymx_ig", "educational", _Fact.text, "https://example.test/about")
    client_sources.add_source(
        "gymx_ig", "service", _Fact.text + " with consistent practice",
        "https://example.test/services")


def _gym_account():
    return Account(key="gymx_ig", display_name="Gym X", platform=Platform.INSTAGRAM,
                   token_env="TOKEN_UNUSED", target_id_env="TARGET_UNUSED")


def _voice():
    from agent.voice import VoiceDoc
    return VoiceDoc(raw="We help busy people win.\n#GymX",
                    hashtags=["#GymX"], ctas=["Book your intro session."])


def _run_infographic(monkeypatch, enabled):
    monkeypatch.setattr(config, "logical_post_id_enabled", lambda: enabled,
                        raising=False)
    store = _Store()
    out = infographic.fill_gaps(
        "gymx", _gym_account(), store, voice=_voice(),
        now="2026-10-04T12:00:00-04:00", days_ahead=2, max_per_run=2)
    assert out["ok"] is True and out["filled"] == 2, out
    return store.inserted


def _run_no_media(monkeypatch, enabled):
    monkeypatch.setattr(config, "logical_post_id_enabled", lambda: enabled,
                        raising=False)
    # The no-media writer has its own generation gate; the fake inventory and
    # bridge facts let the real row construction, validation, and insert run.
    store = _Store()
    count = no_media.seed_gaps(
        "gymx", _gym_account(), store, today=date(2026, 10, 4),
        max_rows=2, days_ahead=2)
    assert count == 2
    return store.inserted


def _assert_uuid(value):
    assert isinstance(value, str)
    assert str(uuid.UUID(value)) == value


def test_enabled_infographic_insert_shares_ids_only_for_explicit_fb_mirrors(
        monkeypatch, writer_environment):
    rows = _run_infographic(monkeypatch, True)
    instagram = [row for row in rows if row["account"] == "instagram"]
    facebook = [row for row in rows if row["account"] == "facebook"]
    assert len(instagram) == len(facebook) == 2
    ids_by_day = {}
    for row in rows:
        _assert_uuid(row["logical_post_id"])
        ids_by_day.setdefault(row["post_date"], {})[row["account"]] = row["logical_post_id"]
    assert len(ids_by_day) == 2
    for pair in ids_by_day.values():
        assert pair["instagram"] == pair["facebook"]
    assert len({pair["instagram"] for pair in ids_by_day.values()}) == 2


def test_enabled_no_media_insert_assigns_distinct_ids_per_generated_row(
        monkeypatch, writer_environment):
    rows = _run_no_media(monkeypatch, True)
    assert len(rows) == 2
    ids = [row["logical_post_id"] for row in rows]
    for value in ids:
        _assert_uuid(value)
    assert len(set(ids)) == 2


def test_flag_off_preserves_legacy_insert_shapes_for_both_writers(
        monkeypatch, writer_environment):
    infographic_rows = _run_infographic(monkeypatch, False)
    no_media_rows = _run_no_media(monkeypatch, False)
    assert infographic_rows and no_media_rows
    assert all("logical_post_id" not in row
               for row in infographic_rows + no_media_rows)


@pytest.mark.parametrize("writer", ["infographic", "no_media"])
def test_invalid_generated_identity_prevents_insert(monkeypatch, writer_environment, writer):
    monkeypatch.setattr(config, "logical_post_id_enabled", lambda: True,
                        raising=False)
    if writer == "infographic":
        monkeypatch.setattr(infographic.uuid, "uuid4", lambda: "not-a-uuid")
        store = _Store()
        out = infographic.fill_gaps(
            "gymx", _gym_account(), store, voice=_voice(),
            now="2026-10-04T12:00:00-04:00", days_ahead=1, max_per_run=1)
        assert out["filled"] == 0
    else:
        monkeypatch.setattr(no_media.uuid, "uuid4", lambda: "not-a-uuid")
        store = _Store()
        count = no_media.seed_gaps(
            "gymx", _gym_account(), store, today=date(2026, 10, 4),
            max_rows=1, days_ahead=1)
        assert count == 0
    assert store.inserted == []


# ---- infographic fill: empty-string default is ABSENT identity (P1) --------

def test_infographic_fill_treats_empty_string_logical_post_id_as_absent(monkeypatch):
    """Mirror-built Drafts default logical_post_id to ""; that must mint a fresh
    UUID, not be held as a malformed existing identity."""
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")
    draft = SimpleNamespace(logical_post_id="")
    assert infographic._ensure_logical_post_id(draft) is True
    assert str(uuid.UUID(draft.logical_post_id)) == draft.logical_post_id


def test_infographic_fill_mints_for_none_and_holds_nonempty_invalid(monkeypatch):
    monkeypatch.setenv("ECHO_LOGICAL_POST_ID_ENABLED", "true")
    draft = SimpleNamespace(logical_post_id=None)
    assert infographic._ensure_logical_post_id(draft) is True
    assert str(uuid.UUID(draft.logical_post_id)) == draft.logical_post_id

    bad = SimpleNamespace(logical_post_id="not-a-uuid")
    assert infographic._ensure_logical_post_id(bad) is False
    assert bad.logical_post_id == "not-a-uuid"


def test_infographic_fill_flag_off_leaves_empty_identity_untouched(monkeypatch):
    monkeypatch.delenv("ECHO_LOGICAL_POST_ID_ENABLED", raising=False)
    assert config.logical_post_id_enabled() is False
    draft = SimpleNamespace(logical_post_id="")
    assert infographic._ensure_logical_post_id(draft) is True
    assert draft.logical_post_id == ""
