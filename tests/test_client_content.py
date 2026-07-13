"""
Client content drafting (Part 3). With AGENT_CLIENT_SOURCES on, a client account
stocked with approved sources + a small image library drafts a full, varied month:
every day carries a caption sourced from that account's approved material, spread
across its categories, and NO caption asserts a fact absent from those sources. A
pending (unapproved) source is never used. Book/summit never appear for a client.
Fully OFFLINE (tmp sqlite + tmp library).
"""

import calendar
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import client_content, client_sources as cs, config, rotation  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "true")
    monkeypatch.delenv("AGENT_HOSTING_ENABLED", raising=False)
    yield


def _voice():
    return VoiceDoc(raw="We help members win.\n#GetFit",
                    hashtags=["#GetFit", "#StrongTogether"],
                    ctas=["Save this post.", "Send this to a friend."])


def _client(tmp_path):
    return Account(key="gym_alpha_ig", display_name="Gym Alpha",
                   platform=Platform.INSTAGRAM,
                   token_env="T", target_id_env="TID",
                   slack_channel="C_ALPHA")


def _lib(tmp_path, n=5):
    lib = tmp_path / "alpha_lib"
    lib.mkdir(exist_ok=True)
    for i in range(n):
        (lib / f"photo_{i:02d}.jpg").write_bytes(b"\xff\xd8\xffFAKEJPEG")
    return str(lib)


def _stock_sources(account_key):
    cs.add_source(account_key, "offer", "6 week challenge for $199", "website /pricing")
    cs.add_source(account_key, "offer", "Free intro session for new members", "website /start")
    cs.add_source(account_key, "service", "Small group personal training", "website /services")
    cs.add_source(account_key, "testimonial", "Sarah lost 30 pounds in 3 months", "member Sarah M")
    cs.add_source(account_key, "faq", "Do you offer childcare? Yes, all morning classes.", "website /faq")
    cs.add_source(account_key, "about", "Family owned and coaching since 2015", "website /about")


def _july_days():
    return [f"2026-07-{d:02d}" for d in range(1, 31)]


# ---- 1. a stocked client drafts a full varied month --------------------------
def test_stocked_client_drafts_full_month(tmp_path):
    acct = _client(tmp_path)
    _stock_sources(acct.key)
    lib = _lib(tmp_path, n=5)
    voice = _voice()

    drafted, cats = [], []
    for day in _july_days():
        d = client_content.build_client_draft(acct, day, voice, lib)
        assert d is not None, f"{day} produced no draft"
        assert d.status == DraftStatus.PENDING
        assert d.caption.strip()
        assert d.creative_path                 # an image was paired
        drafted.append(d)
        cats.append(d.category)

    assert len(drafted) == 30
    # varied: more than one category actually used across the month
    assert len(set(cats)) >= 3, f"month not varied: {set(cats)}"


# ---- 2. no caption asserts a fact absent from the account's sources -----------
def test_no_caption_fabricates(tmp_path):
    acct = _client(tmp_path)
    _stock_sources(acct.key)
    lib = _lib(tmp_path, n=5)
    voice = _voice()
    claims = cs.approved_claims(acct.key)
    for day in _july_days():
        d = client_content.build_client_draft(acct, day, voice, lib)
        # every claim-bearing sentence in the caption traces to an approved source
        assert rotation.is_gate_clean(d.caption, approved_claims=claims), \
            f"{day}: caption asserts an unsourced fact: {d.caption!r}"
        # no dash characters in client copy (standing law)
        assert not any(dash in d.caption for dash in ("—", "–", "-"))
        # never the word vendor
        assert "vendor" not in d.caption.lower()


# ---- 3. a pending (unapproved) source is never drafted from -------------------
def test_pending_source_never_used(tmp_path):
    acct = _client(tmp_path)
    lib = _lib(tmp_path, n=5)
    voice = _voice()
    # ONLY pending sources exist: nothing approved
    cs.submit_intake(acct.key, {"offer": ["Secret unapproved $1 deal"]})
    d = client_content.build_client_draft(acct, "2026-07-01", voice, lib)
    assert d is None                            # nothing approved -> no client draft
    # and the pending fact never leaks into any claim set
    assert cs.approved_claims(acct.key) == []


# ---- 4. flag OFF -> dormant (client behaves as today) ------------------------
def test_flag_off_is_dormant(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CLIENT_SOURCES", "false")
    acct = _client(tmp_path)
    _stock_sources(acct.key)
    lib = _lib(tmp_path, n=5)
    assert client_content.build_client_draft(acct, "2026-07-01", _voice(), lib) is None


# ---- 5. citation is carried for the cited-source audit trail ------------------
def test_citation_carried_in_fragments(tmp_path):
    acct = _client(tmp_path)
    _stock_sources(acct.key)
    lib = _lib(tmp_path, n=5)
    d = client_content.build_client_draft(acct, "2026-07-01", _voice(), lib)
    cites = [f for f in d.source_fragments if f.startswith("cite:")]
    assert len(cites) == 1 and cites[0] != "cite:"


# ---- 6. book and summit never appear for a client account --------------------
def test_client_draft_is_never_book_or_summit(tmp_path):
    acct = _client(tmp_path)
    _stock_sources(acct.key)
    lib = _lib(tmp_path, n=5)
    voice = _voice()
    for day in _july_days():
        d = client_content.build_client_draft(acct, day, voice, lib)
        assert d.category in cs.CLIENT_CATEGORIES
        assert d.category not in ("book", "summit")


# ---- 7. end to end through run_daily: the client cards a source-based draft ----
def test_run_daily_drafts_client_from_sources(tmp_path, monkeypatch):
    from agent import config as _config
    from agent.runner import run_daily
    from agent.store import PendingStore

    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setattr(_config, "SLACK_CHANNEL_ID", "C_LASSO_INTERNAL")
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)

    voice = tmp_path / "alpha_voice.md"
    voice.write_text("We help members win.\n#GetFit\nCTA: Save this post.\n",
                     encoding="utf-8")
    lib = _lib(tmp_path, n=5)
    _stock_sources("gym_alpha_ig")

    acct = Account(key="gym_alpha_ig", display_name="Gym Alpha",
                   platform=Platform.INSTAGRAM, token_env="T", target_id_env="TID",
                   slack_channel="C_ALPHA", voice_doc=str(voice), library_prefix=lib)

    posted = []

    class _Poster:
        def post_approval_card(self, draft):
            posted.append(draft)
            return {"channel": "C_ALPHA", "ts": "t"}

        def post_notice(self, text):
            return {"ok": True}

        def mark_superseded(self, draft):
            pass

        def mark_expired(self, draft):
            pass

    out = run_daily(poster=_Poster(), voice_path=str(voice), library_path=lib,
                    scheduled_for="2026-07-08T14:00:00+00:00",
                    accounts=[acct], store=PendingStore(path=db_path))

    assert out["status"] == "drafted"
    assert len(posted) == 1
    d = posted[0]
    assert d.account_key == "gym_alpha_ig"
    assert d.status == DraftStatus.PENDING
    assert d.category in cs.CLIENT_CATEGORIES     # a client-source draft, not blocked
    assert d.caption.strip()
