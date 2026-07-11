"""
Launch simulation: one daily run across 12 client accounts where several are
broken, proving fleet isolation end to end.

The fleet: 7 healthy gyms, 3 gyms with corrupt legacy rows in the store
(NULL blob, malformed JSON, unknown status), 1 gym with no token set
(expired/unset), 1 gym with an empty library.

Must hold, in one run:
  - the run COMPLETES (no corrupt row, missing token, or empty library
    raises out of run_daily),
  - every account with content drafts a PENDING card — including the three
    with corrupt rows (quarantined, not fatal) and the token-less one
    (drafting never needs a token; only publishing does, and it's off),
  - the empty-library gym cards a BLOCKED draft naming the reason (block,
    never fabricate) instead of vanishing,
  - NOTHING publishes: publish flag off, posts table stays empty,
  - one bad account's failure alerts and skips without touching the rest
    (forced by making one account's library path a file, not a dir).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db as _db
from agent.accounts import Account, Platform
from agent.drafter import DraftStatus
from agent.runner import run_daily
from agent.store import PendingStore

DAY = "2026-07-08"  # a Wednesday: not a skip day

_VOICE = """# Voice
We help gym owners grow.
## CTAs
- Save this post.
## Hashtags
#LASSOFramework
"""


class _RecordingPoster:
    def __init__(self):
        self.cards = []
        self.notices = []

    def post_approval_card(self, draft):
        self.cards.append(draft)
        return {"channel": f"C_{draft.account_key}", "ts": "ts1"}

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}

    def mark_superseded(self, draft):
        pass

    def mark_expired(self, draft):
        pass


def _gym(key, lib_dir):
    return Account(key=key, display_name=key, platform=Platform.INSTAGRAM,
                   token_env=f"TOK_{key.upper()}",
                   target_id_env=f"TGT_{key.upper()}",
                   library_prefix=str(lib_dir))


def _make_library(tmp_path, key):
    lib = tmp_path / f"lib_{key}"
    lib.mkdir()
    (lib / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    (lib / "asset.txt").write_text("An approved note.", encoding="utf-8")
    return lib


def _seed_corrupt_row(db_path, key, kind):
    """Three corruption shapes, one per broken account."""
    data = {"null": None, "garbage": "{not json", "badstatus":
            json.dumps({"draft_id": f"old_{key}", "status": "retired_v0"})}[kind]
    with _db.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO drafts "
            "(draft_id, account_key, status, day_key, draft_type, data) "
            "VALUES (?,?,?,?,?,?)",
            (f"old_{key}", key, "pending", "2020-01-01", "feed", data))
        conn.commit()


def test_twelve_account_run_with_broken_accounts(monkeypatch, tmp_path):
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.delenv("AGENT_PUBLISH_ENABLED", raising=False)  # publish OFF

    voice = tmp_path / "voice.md"
    voice.write_text(_VOICE, encoding="utf-8")

    accounts = []
    # 7 healthy gyms with tokens and content
    for i in range(1, 8):
        key = f"gym_{i:02d}"
        accounts.append(_gym(key, _make_library(tmp_path, key)))
        monkeypatch.setenv(f"TOK_{key.upper()}", "tok")
    # 3 gyms with corrupt legacy rows (content otherwise fine)
    for i, kind in ((8, "null"), (9, "garbage"), (10, "badstatus")):
        key = f"gym_{i:02d}"
        accounts.append(_gym(key, _make_library(tmp_path, key)))
        monkeypatch.setenv(f"TOK_{key.upper()}", "tok")
        _seed_corrupt_row(db_path, key, kind)
    # 1 gym with NO token set (expired/unset) but content present
    accounts.append(_gym("gym_11", _make_library(tmp_path, "gym_11")))
    monkeypatch.delenv("TOK_GYM_11", raising=False)
    # 1 gym with an EMPTY library
    empty_lib = tmp_path / "lib_gym_12"
    empty_lib.mkdir()
    accounts.append(_gym("gym_12", empty_lib))
    monkeypatch.setenv("TOK_GYM_12", "tok")

    poster = _RecordingPoster()
    store = PendingStore(path=db_path)

    out = run_daily(poster=poster, voice_path=str(voice),
                    library_path=str(tmp_path / "unused_global_lib"),
                    scheduled_for=f"{DAY}T14:00:00+00:00",
                    accounts=accounts, store=store)

    # 1) the run completes
    assert out["status"] == "drafted"

    by_acct = {}
    for d in out["drafts"]:
        if not getattr(d, "is_story", False):
            by_acct.setdefault(d.account_key, []).append(d)

    # 2) all 11 accounts with content draft a PENDING card — the corrupt-row
    #    three and the token-less one included
    for i in range(1, 12):
        key = f"gym_{i:02d}"
        drafts = by_acct.get(key, [])
        assert drafts, f"{key} produced no draft at all"
        assert any(d.status == DraftStatus.PENDING for d in drafts), (
            f"{key} should card a pending draft; got "
            f"{[d.status.value for d in drafts]}")

    # 3) the empty-library gym cards a BLOCKED draft naming the reason
    gym12 = by_acct.get("gym_12", [])
    assert gym12, "gym_12 (empty library) must card a blocked draft, not vanish"
    assert all(d.status == DraftStatus.BLOCKED for d in gym12)
    assert any(d.blocked_reason for d in gym12)

    # 4) nothing publishes
    with _db.connect(db_path) as conn:
        n_posts = conn.execute("SELECT count(*) FROM posts").fetchone()[0]
    assert n_posts == 0, "publish flag is OFF; nothing may reach the posts table"


def test_one_crashing_account_skips_and_alerts_others_draft(monkeypatch, tmp_path):
    """Force one account to genuinely RAISE inside its draft cycle and prove
    the per-account try/except holds: that account alerts and skips, the
    others draft, the run returns normally.

    (A library path pointing at a file was tried first, but that degrades to
    a BLOCKED card before ever raising — contained even earlier. To exercise
    the exception path itself, the heartbeat write blows up for one key.)"""
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.delenv("AGENT_PUBLISH_ENABLED", raising=False)

    voice = tmp_path / "voice.md"
    voice.write_text(_VOICE, encoding="utf-8")

    healthy = _gym("gym_ok", _make_library(tmp_path, "gym_ok"))
    monkeypatch.setenv("TOK_GYM_OK", "tok")
    broken = _gym("gym_boom", _make_library(tmp_path, "gym_boom"))
    monkeypatch.setenv("TOK_GYM_BOOM", "tok")

    import agent.heartbeat as _hb
    real_hb = _hb.record_heartbeat

    def _selective_boom(account_key, day_key):
        if account_key == "gym_boom":
            raise RuntimeError("simulated per-account crash")
        return real_hb(account_key, day_key)

    monkeypatch.setattr(_hb, "record_heartbeat", _selective_boom)

    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda m, **kw: alerts.append(m))

    poster = _RecordingPoster()
    out = run_daily(poster=poster, voice_path=str(voice),
                    library_path=str(tmp_path / "unused"),
                    scheduled_for=f"{DAY}T14:00:00+00:00",
                    accounts=[broken, healthy],
                    store=PendingStore(path=db_path))

    assert out["status"] == "drafted"
    keys = {d.account_key for d in out["drafts"]}
    assert "gym_ok" in keys, "the healthy account must still draft"
    assert "gym_boom" not in keys, "the crashed account skips its day"
    assert any("gym_boom" in a for a in alerts), (
        "the crashed account must fire one ops alert naming it")
