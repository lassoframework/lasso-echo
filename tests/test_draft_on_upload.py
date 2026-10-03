"""
Draft-on-upload (AGENT_DRAFT_ON_UPLOAD): the instant a gym's media is ingested,
draft ONE approval card per new asset instead of waiting for the daily draw.

Fully OFFLINE: fake poster, real PendingStore on tmp, injected account/voice, no
network. Asserts:
  - the flag defaults OFF and OFF is a no-op (today's behavior);
  - ON, each new asset produces one PENDING draft and one approval card, through
    the SAME _post_and_save path (gates intact);
  - a tenant with no registry account is SKIPPED with one ops alert (media safe);
  - a tenant whose voice doc is missing is SKIPPED with one ops alert (NO fabrication);
  - one bad asset never blocks the others and never crashes;
  - the ingest pass fires the trigger and reports drafted_on_upload in its stats.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, runner, intake_ingest, ops_alerts  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402
from agent.store import PendingStore  # noqa: E402


VOICE = ('We help busy people get fit again.\n\n'
         '### CTA rotation\n"Book your intro session."\n\n'
         '## Hashtags\n#GymLife')


class FakePoster:
    def __init__(self):
        self.cards = []
        self.notices = []
        self.expired = []

    def post_approval_card(self, draft):
        self.cards.append(draft)
        return {"ok": True, "channel": "C1", "ts": f"ts{len(self.cards)}"}

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}

    def mark_expired(self, draft):
        self.expired.append(draft)
        return {"ok": True}


def _acct(key="gymx_ig", voice_doc=""):
    return Account(key=key, display_name="Gym X", platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="G", voice_doc=voice_doc)


def _asset(tmp_path, name="photo1.jpg", note="Saturday open house was packed."):
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    p = lib / name
    p.write_bytes(b"\x89PNG\r\n\x1a\nFAKE" + name.encode())
    return str(p), note


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("AGENT_DRAFT_ON_UPLOAD", "true")
    for f in ("AGENT_PUBLISH_ENABLED", "AGENT_AUTO_APPROVE_ENABLED",
              "AGENT_PORTAL_SOCIAL_ENABLED", "AGENT_HOSTING_ENABLED",
              "AGENT_CONTENT_BRAIN_ENABLED"):
        monkeypatch.delenv(f, raising=False)


# ---- flag default + OFF = no-op -----------------------------------------------

def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_DRAFT_ON_UPLOAD", raising=False)
    assert config.draft_on_upload_enabled() is False


def test_flag_off_is_noop(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_DRAFT_ON_UPLOAD", raising=False)
    poster = FakePoster()
    out = runner.draft_for_new_upload("gymx", [_asset(tmp_path)], poster=poster,
                                      store=PendingStore(path=str(tmp_path / "s.json")))
    assert out == []
    assert poster.cards == []


# ---- ON: one PENDING card per new asset, via _post_and_save -------------------

def test_drafts_one_card_per_asset(monkeypatch, tmp_path):
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    voice_file = tmp_path / "voice.md"
    voice_file.write_text(VOICE, encoding="utf-8")
    monkeypatch.setattr(runner, "_generation_account_for",
                        lambda t: _acct(voice_doc=str(voice_file)))
    poster = FakePoster()
    store = PendingStore(path=str(tmp_path / "s.json"))
    assets = [_asset(tmp_path, "a.jpg", "First win of the week."),
              _asset(tmp_path, "b.jpg", "New members joining Monday.")]
    out = runner.draft_for_new_upload("gymx", assets, poster=poster, store=store)
    assert len(out) == 2
    assert all(d.status == DraftStatus.PENDING for d in out)
    assert len(poster.cards) == 2                 # one approval card per asset
    assert len(store.list_pending()) == 2


def test_upload_claim_consumes_local_media_across_lanes_and_aliases(monkeypatch, tmp_path):
    from agent import client_content, dam, rotation
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    voice_file = tmp_path / "voice.md"
    voice_file.write_text(VOICE, encoding="utf-8")
    monkeypatch.setattr(runner, "_generation_account_for",
                        lambda t: _acct(voice_doc=str(voice_file)))
    first = _asset(tmp_path, "first.jpg", "A class at the gym.")
    alias = _asset(tmp_path, "alias.jpg", "The same class.")
    with open(first[0], "rb") as source, open(alias[0], "wb") as target:
        target.write(source.read())
    poster = FakePoster()
    store = PendingStore(path=str(tmp_path / "s.json"))
    out = runner.draft_for_new_upload("gymx", [first, alias], poster=poster, store=store)
    assert len(out) == len(poster.cards) == 1
    assert rotation.local_photo_served(
        dam.rotation_key(alias[0]), "gymx_fb", "2026-10-04", path=alias[0])
    assert client_content.pick_image("gymx_fb", "2026-10-04",
                                     os.path.dirname(alias[0])) is None
    assert runner.draft_for_new_upload("gymx", [first], poster=poster, store=store) == []


def test_direct_upload_holds_when_global_ledger_already_consumed_bytes(monkeypatch, tmp_path):
    """The immediate upload route bypasses pick_image and needs its own guard."""
    from agent import gym_media_selector as selector, rotation
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv(selector.GLOBAL_LEDGER_FLAG_ENV, "true")
    voice_file = tmp_path / "voice.md"
    voice_file.write_text(VOICE, encoding="utf-8")
    monkeypatch.setattr(runner, "_generation_account_for",
                        lambda _t: _acct(voice_doc=str(voice_file)))
    asset = _asset(tmp_path, "used.jpg", "Packed class.")
    used = rotation.local_global_fingerprint(asset[0])
    monkeypatch.setattr(selector, "cross_client_used_fingerprints",
                        lambda *args, **kwargs: {used})
    poster = FakePoster()

    out = runner.draft_for_new_upload(
        "gymx", [asset], poster=poster, store=PendingStore(path=str(tmp_path / "s.json")))

    assert out == []
    assert poster.cards == []


@pytest.mark.parametrize("mode", ("ambiguous_flag", "unreadable_ledger"))
def test_direct_upload_holds_photo_when_global_history_uncertain(
        monkeypatch, tmp_path, mode):
    """Unknown global photo history must hold the photo upload (fail closed),
    alert once, and never draft or card it."""
    from agent import gym_media_selector as selector
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    if mode == "ambiguous_flag":
        monkeypatch.setenv(selector.GLOBAL_LEDGER_FLAG_ENV, "maybe")
    else:
        monkeypatch.setenv(selector.GLOBAL_LEDGER_FLAG_ENV, "true")
        monkeypatch.setattr(selector, "cross_client_used_fingerprints",
                            lambda *args, **kwargs: (_ for _ in ()).throw(
                                selector.GlobalLedgerUnavailable("unreadable")))
    voice_file = tmp_path / "voice.md"
    voice_file.write_text(VOICE, encoding="utf-8")
    monkeypatch.setattr(runner, "_generation_account_for",
                        lambda _t: _acct(voice_doc=str(voice_file)))
    alerts = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **k: alerts.append(msg))
    poster = FakePoster()
    store = PendingStore(path=str(tmp_path / "s.json"))
    held = _asset(tmp_path, "held.jpg", "Packed class.")

    out = runner.draft_for_new_upload("gymx", [held], poster=poster, store=store)

    # Uncertain global state is never proof the bytes are free: hold the asset.
    assert out == []
    assert poster.cards == []
    assert store.list_pending() == []
    assert len(alerts) == 1 and "held.jpg" in alerts[0]


def test_upload_releases_only_definitive_no_card_no_row(monkeypatch, tmp_path):
    from agent import dam, rotation
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    voice_file = tmp_path / "voice.md"
    voice_file.write_text(VOICE, encoding="utf-8")
    monkeypatch.setattr(runner, "_generation_account_for",
                        lambda t: _acct(voice_doc=str(voice_file)))
    asset = _asset(tmp_path, "retry.jpg", "Coached class.")
    store = PendingStore(path=str(tmp_path / "s.json"))

    def absent(_draft, _store, _poster, _idempotent):
        raise OSError("before card")

    monkeypatch.setattr(runner, "_post_and_save", absent)
    assert runner.draft_for_new_upload("gymx", [asset], store=store, poster=FakePoster()) == []
    assert not rotation.local_photo_served(
        dam.rotation_key(asset[0]), "gymx_ig", "2026-10-04", path=asset[0])

    def uncertain(draft, _store, _poster, _idempotent):
        draft._external_visibility_attempted = True
        raise TimeoutError("card outcome unknown")

    monkeypatch.setattr(runner, "_post_and_save", uncertain)
    assert runner.draft_for_new_upload("gymx", [asset], store=store, poster=FakePoster()) == []
    assert rotation.local_photo_served(
        dam.rotation_key(asset[0]), "gymx_ig", "2026-10-04", path=asset[0])


# ---- CRITICAL gate: a client upload must NEVER auto-publish -------------------

def test_client_upload_never_auto_publishes_when_autoapprove_armed(monkeypatch, tmp_path):
    """Regression for the audit's CRITICAL: with the portfolio-wide auto-approve
    armed, a CLIENT gym's upload draft must carry force_approval=True so it is
    NEVER caught by the auto-approve block in _post_and_save. It stays PENDING
    (cards for approval); it is never marked APPROVED / pushed to publish."""
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_AUTO_APPROVE_ENABLED", "true")   # the armed switch
    voice_file = tmp_path / "voice.md"
    voice_file.write_text(VOICE, encoding="utf-8")
    monkeypatch.setattr(runner, "_generation_account_for",
                        lambda t: _acct(key="gymx_ig", voice_doc=str(voice_file)))
    poster = FakePoster()
    store = PendingStore(path=str(tmp_path / "s.json"))
    out = runner.draft_for_new_upload(
        "gymx", [_asset(tmp_path, "a.jpg", "Packed 6am class today.")],
        poster=poster, store=store)
    assert len(out) == 1
    assert out[0].force_approval is True
    assert out[0].status == DraftStatus.PENDING               # NOT auto-published
    assert not any("Auto-published" in n for n in poster.notices)


def test_lasso_upload_keeps_default_force_approval(monkeypatch, tmp_path):
    """LASSO's own accounts keep force_approval=False, so their existing
    portfolio auto-approve behavior is unchanged by draft-on-upload."""
    _arm(monkeypatch)   # auto-approve NOT armed here
    voice_file = tmp_path / "voice.md"
    voice_file.write_text(VOICE, encoding="utf-8")
    monkeypatch.setattr(runner, "_generation_account_for",
                        lambda t: _acct(key="lasso_ig", voice_doc=str(voice_file)))
    out = runner.draft_for_new_upload(
        "lasso", [_asset(tmp_path, "a.jpg", "A LASSO win this week.")],
        poster=FakePoster(), store=PendingStore(path=str(tmp_path / "s.json")))
    assert len(out) == 1
    assert out[0].force_approval is False


def test_noteless_asset_is_skipped_not_cta_only_card(monkeypatch, tmp_path):
    """A note-less upload must not surface a CTA-only card; it is skipped."""
    _arm(monkeypatch)
    voice_file = tmp_path / "voice.md"
    voice_file.write_text(VOICE, encoding="utf-8")
    monkeypatch.setattr(runner, "_generation_account_for",
                        lambda t: _acct(voice_doc=str(voice_file)))
    poster = FakePoster()
    path, _note = _asset(tmp_path, "nocaption.jpg", "")
    out = runner.draft_for_new_upload("gymx", [(path, "")], poster=poster,
                                      store=PendingStore(path=str(tmp_path / "s.json")))
    assert out == []
    assert poster.cards == []


def test_video_upload_waits_while_an_unused_photo_is_available(monkeypatch, tmp_path):
    """Arming the instant-card lane cannot let a clip jump an available photo."""
    _arm(monkeypatch)
    voice_file = tmp_path / "voice.md"
    voice_file.write_text(VOICE, encoding="utf-8")
    monkeypatch.setattr(runner, "_generation_account_for",
                        lambda t: _acct(voice_doc=str(voice_file)))
    monkeypatch.setattr(runner, "_unused_client_photo_available",
                        lambda *a, **k: True)
    poster = FakePoster()
    out = runner.draft_for_new_upload(
        "gymx", [_asset(tmp_path, "class.mp4", "A coached class in progress.")],
        poster=poster, store=PendingStore(path=str(tmp_path / "s.json")))
    assert out == []
    assert poster.cards == []


def test_daily_drive_builder_accepts_one_explicit_media_tier(monkeypatch):
    """The daily entrypoint can constrain the builder to the photo tier."""
    from types import SimpleNamespace
    from agent import client_content, client_sources, gym_media_builder, rotation

    monkeypatch.setattr(config, "client_sources_enabled", lambda: True)
    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: True)
    monkeypatch.setattr(config, "gym_drive_connect_active_for", lambda _key: True)
    monkeypatch.setattr(client_sources, "categories_present", lambda _key: ["service"])
    monkeypatch.setattr(client_sources, "approved_claims", lambda _key: ["Fact"])
    monkeypatch.setattr(client_content, "category_for_day",
                        lambda *a: "service")
    monkeypatch.setattr(client_content, "_pillars_for",
                        lambda *a: ["service"])
    source = SimpleNamespace(text="Fact")
    monkeypatch.setattr(client_content, "_source_for_day", lambda *a: source)
    monkeypatch.setattr(rotation, "is_gate_clean", lambda *a, **k: True)
    seen = []
    expected = SimpleNamespace(scheduled_for="")

    def _build(*args, **kwargs):
        seen.append(kwargs["kind_prefs"])
        return expected

    monkeypatch.setattr(gym_media_builder, "build_gym_media_draft", _build)
    account = SimpleNamespace(key="gymx_ig")
    assert runner._client_drive_first_draft(
        account, "2026-10-03", object(), kind_prefs=("photo",)) is expected
    assert seen == [("photo",)]
    assert expected.scheduled_for
    assert expected.force_approval is True


def test_daily_transient_drive_photo_failure_holds_all_videos(monkeypatch):
    """A still-pickable Drive photo may not fall through to either video tier."""
    from types import SimpleNamespace
    from agent import client_content

    account = SimpleNamespace(key="gymx_ig")
    kinds = []
    monkeypatch.setattr(runner, "_client_drive_kind_available",
                        lambda _account, kind: True)
    monkeypatch.setattr(runner, "_client_drive_first_draft",
                        lambda *a, **k: kinds.append(k["kind_prefs"]) or None)
    monkeypatch.setattr(runner, "_client_local_photo_available",
                        lambda *a, **k: False)
    monkeypatch.setattr(client_content, "build_client_draft",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("local video must stay held")))
    assert runner._client_photo_first_draft(
        account, "2026-10-03", object(), "/lib") is None
    assert kinds == [("photo",)]


def test_daily_order_reaches_drive_video_only_after_both_photo_tiers_empty(monkeypatch):
    from types import SimpleNamespace
    account = SimpleNamespace(key="gymx_ig")
    checks = []
    expected = object()

    def _available(_account, kind):
        checks.append(kind)
        return kind == "video"

    monkeypatch.setattr(runner, "_client_drive_kind_available", _available)
    monkeypatch.setattr(runner, "_client_local_photo_available",
                        lambda *a, **k: False)
    monkeypatch.setattr(runner, "_client_drive_first_draft",
                        lambda *a, **k: expected)
    assert runner._client_photo_first_draft(
        account, "2026-10-03", object(), "/lib") is expected
    assert checks == ["photo", "video"]


def test_daily_photo_inventory_claim_failure_holds_video(monkeypatch):
    from types import SimpleNamespace
    from agent import config, db, gym_media_index
    from tests.gym_media_fakes import FakeMediaStore, make_asset

    monkeypatch.setattr(config, "gym_drive_stage_enabled", lambda: True)
    monkeypatch.setattr(config, "gym_drive_connect_active_for", lambda _key: True)
    monkeypatch.setattr(gym_media_index, "default_store", lambda: FakeMediaStore(
        assets=[make_asset("photo", gym_id="gymx")]))
    monkeypatch.setattr(db, "drive_asset_claimed_ids",
                        lambda _base: (_ for _ in ()).throw(OSError("claim read")))
    assert runner._client_drive_kind_available(
        SimpleNamespace(key="gymx_ig"), "photo") is None


def test_upload_video_holds_when_local_inventory_is_uncertain(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from agent import rotation

    monkeypatch.setattr(rotation, "load_served_strict",
                        lambda: (_ for _ in ()).throw(OSError("ledger unavailable")))
    assert runner._unused_client_photo_available(
        SimpleNamespace(key="gymx_ig"), str(tmp_path / "clip.mp4"),
        "2026-10-03") is True


def test_unlanded_daily_drive_draft_restores_its_pre_stage_stamp(monkeypatch):
    from types import SimpleNamespace
    from agent import db, gym_media_index, gym_media_selector
    calls = []
    releases = []
    media_store = object()
    monkeypatch.setattr(gym_media_index, "default_store", lambda: media_store)
    monkeypatch.setattr(db, "socialapi_claim_release",
                        lambda *args: releases.append(args))
    monkeypatch.setattr(
        gym_media_selector, "rollback_use",
        lambda gym, day, **kwargs: calls.append((gym, day, kwargs)) or True)
    draft = SimpleNamespace(account_key="gymx_ig",
                            source_media_asset_id="drive-photo-1",
                            _drive_claim_id="gbp_media:gymx:hash:" + "a" * 64)
    assert runner._rollback_unlanded_drive_draft(draft, "2026-10-03") is True
    assert calls == [("gymx_ig", "2026-10-03",
                      {"store": media_store, "asset_id": "drive-photo-1",
                       "restore_unstaged": True})]
    assert releases == [("gbp_media:gymx:hash:" + "a" * 64, "gymx_gbp")]


def test_daily_failed_restore_retains_exact_claim(monkeypatch):
    from types import SimpleNamespace
    from agent import db, gym_media_selector
    releases = []
    monkeypatch.setattr(gym_media_selector, "rollback_use",
                        lambda *a, **k: False)
    monkeypatch.setattr(db, "socialapi_claim_release",
                        lambda *a: releases.append(a))
    draft = SimpleNamespace(account_key="gymx_ig",
                            source_media_asset_id="drive-photo-1")
    assert runner._rollback_unlanded_drive_draft(draft, "2026-10-03") is False
    assert releases == []


def test_visible_card_or_partial_put_readback_never_restores_drive_media(monkeypatch):
    from types import SimpleNamespace
    draft = SimpleNamespace(draft_id="d1")

    class EmptyStore:
        def get(self, _draft_id):
            return None

    assert runner._drive_draft_landed_or_uncertain(draft, EmptyStore()) is False
    draft._approval_visible = True
    assert runner._drive_draft_landed_or_uncertain(draft, EmptyStore()) is True

    persisted = SimpleNamespace(draft_id="d1")
    class PartialPutStore:
        def get(self, _draft_id):
            return persisted

    assert runner._drive_draft_landed_or_uncertain(
        SimpleNamespace(draft_id="d1"), PartialPutStore()) is True

    class UnknownStore:
        def get(self, _draft_id):
            raise OSError("readback unavailable")

    assert runner._drive_draft_landed_or_uncertain(
        SimpleNamespace(draft_id="d1"), UnknownStore()) is True

    attempted = SimpleNamespace(
        draft_id="d1", _external_visibility_attempted=True)
    assert runner._drive_draft_landed_or_uncertain(attempted, EmptyStore()) is True
    explicit_failure = SimpleNamespace(
        draft_id="d1", _external_visibility_attempted=True,
        _external_visibility_known_absent=True)
    assert runner._drive_draft_landed_or_uncertain(
        explicit_failure, EmptyStore()) is False


def test_post_and_save_marks_visible_before_partial_store_failure(monkeypatch):
    from types import SimpleNamespace
    draft = SimpleNamespace(
        draft_id="d1", account_key="gymx_ig", status=DraftStatus.PENDING,
        caption="A real caption", hashtags=[], force_approval=True)

    class Poster:
        def post_approval_card(self, _draft):
            return {"ok": True, "channel": "C1", "ts": "1.2"}

    class PartialStore:
        def put(self, _draft):
            raise OSError("persist failed after card")

    monkeypatch.setattr("agent.gym_calendar_queue.approval_surface_for",
                        lambda _acct: "slack")
    monkeypatch.setattr("agent.accounts.get_account", lambda _key: None)
    with pytest.raises(OSError, match="persist failed"):
        runner._post_and_save(draft, PartialStore(), Poster(), True)
    assert draft._approval_visible is True


def test_post_and_save_retains_drive_stamp_when_slack_outcome_is_uncertain(monkeypatch):
    from types import SimpleNamespace
    draft = SimpleNamespace(
        draft_id="d1", account_key="gymx_ig", status=DraftStatus.PENDING,
        caption="A real caption", hashtags=[], force_approval=True)

    class Poster:
        def post_approval_card(self, _draft):
            raise TimeoutError("response lost")

    monkeypatch.setattr("agent.gym_calendar_queue.approval_surface_for",
                        lambda _acct: "slack")
    monkeypatch.setattr("agent.accounts.get_account", lambda _key: None)
    with pytest.raises(TimeoutError, match="response lost"):
        runner._post_and_save(draft, object(), Poster(), True)
    assert runner._drive_draft_landed_or_uncertain(
        draft, type("Store", (), {"get": lambda self, _id: None})()) is True


# ---- no account -> skip with one alert, media untouched -----------------------

def test_no_account_skips_with_alert(monkeypatch, tmp_path):
    _arm(monkeypatch)
    monkeypatch.setattr(runner, "_generation_account_for", lambda t: None)
    alerts = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **k: alerts.append(msg))
    poster = FakePoster()
    out = runner.draft_for_new_upload("mystery_gym", [_asset(tmp_path)], poster=poster,
                                      store=PendingStore(path=str(tmp_path / "s.json")))
    assert out == []
    assert poster.cards == []
    assert len(alerts) == 1
    assert "mystery_gym" in alerts[0] and "no registry account" in alerts[0]


# ---- no voice -> skip with one alert, NO fabrication --------------------------

def test_missing_voice_skips_with_alert(monkeypatch, tmp_path):
    _arm(monkeypatch)
    # account exists but points at a voice doc that does not exist
    monkeypatch.setattr(runner, "_generation_account_for",
                        lambda t: _acct(voice_doc=str(tmp_path / "nope.md")))
    alerts = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **k: alerts.append(msg))
    poster = FakePoster()
    out = runner.draft_for_new_upload("gymx", [_asset(tmp_path)], poster=poster,
                                      store=PendingStore(path=str(tmp_path / "s.json")))
    assert out == []
    assert poster.cards == []
    assert len(alerts) == 1 and "voice doc" in alerts[0]


# ---- one bad asset never blocks the rest -------------------------------------

def test_one_bad_asset_does_not_block_others(monkeypatch, tmp_path):
    _arm(monkeypatch)
    voice_file = tmp_path / "voice.md"
    voice_file.write_text(VOICE, encoding="utf-8")
    monkeypatch.setattr(runner, "_generation_account_for",
                        lambda t: _acct(voice_doc=str(voice_file)))
    poster = FakePoster()
    store = PendingStore(path=str(tmp_path / "s.json"))
    good = _asset(tmp_path, "good.jpg", "A real caption here.")
    bad = (None, "note")     # a None path explodes inside the per-asset try
    out = runner.draft_for_new_upload("gymx", [bad, good], poster=poster, store=store)
    # the good one still drafted; the bad one was contained
    assert len(out) == 1
    assert len(poster.cards) == 1


# ---- integration: ingest pass fires the trigger ------------------------------

class _R2:
    def __init__(self):
        self.objects = {}

    def list_keys(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def get_bytes(self, key):
        return self.objects[key]

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = data

    def delete(self, key):
        self.objects.pop(key, None)


def test_ingest_pass_triggers_draft_on_upload(monkeypatch, tmp_path):
    import json
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path / "library"))
    voice_file = tmp_path / "voice.md"
    voice_file.write_text(VOICE, encoding="utf-8")
    monkeypatch.setattr(runner, "_generation_account_for",
                        lambda t: _acct(voice_doc=str(voice_file)))
    # a real PendingStore for the trigger's default path
    monkeypatch.setattr("agent.store.PendingStore",
                        lambda *a, **k: PendingStore(path=str(tmp_path / "s.json")))

    r2 = _R2()
    name = "20260812T100000Z_photo.jpg"
    r2.put_bytes(f"intake/gymx/incoming/{name}", b"IMGBYTES")
    stamp = name.split("_", 1)[0]
    r2.put_bytes(f"intake/gymx/incoming/{stamp}_upload.json",
                 json.dumps({"note": "Packed class this morning.",
                             "client": "gymx", "timestamp": stamp,
                             "filenames": [name]}).encode())

    poster = FakePoster()
    stats = intake_ingest.process_all(
        r2=r2, poster=poster,
        converter=lambda d, n: (d, n),
        phash=lambda d, n: "ph:" + d[:4].hex(),
        moderator=lambda d, n: (True, ""))
    assert stats["gymx"]["accepted"] == 1
    assert stats["gymx"]["drafted_on_upload"] == 1
    assert len(poster.cards) == 1                 # the card is in the queue now


def test_voice_resolves_durable_first_like_the_month_builder(monkeypatch, tmp_path):
    """Pierce 2026-08-25: this path read ONLY the account's repo-relative voice_doc
    (wiped on deploy for onboarded gyms) and alerted 'voice doc missing' while the real
    bible sat on the persistent volume. It must resolve DURABLE-FIRST
    (<DATA>/brand_voice/<base>/lasso_voice.md), exactly like the month builder."""
    _arm(monkeypatch)
    from agent import config as _config
    # durable bible EXISTS; the account's repo voice_doc points at a WIPED path
    durable_dir = tmp_path / "brand_voice" / "gymx"
    durable_dir.mkdir(parents=True)
    (durable_dir / "lasso_voice.md").write_text(VOICE, encoding="utf-8")
    monkeypatch.setattr(_config, "client_voice_dir",
                        lambda: str(tmp_path / "brand_voice"))
    monkeypatch.setattr(runner, "_generation_account_for",
                        lambda t: _acct(voice_doc="/app/brand_voice/gymx.md"))  # gone
    poster = FakePoster()
    store = PendingStore(path=str(tmp_path / "s.json"))
    out = runner.draft_for_new_upload(
        "gymx", [_asset(tmp_path, "a.jpg", "Members crushing the 6am class.")],
        poster=poster, store=store)
    assert len(out) == 1, "the durable bible must be found; no false voice-missing skip"
    assert len(poster.cards) == 1
