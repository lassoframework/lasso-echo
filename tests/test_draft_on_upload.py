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
    p.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
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
