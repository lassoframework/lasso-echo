"""
Stage 1 gate tests. These ARE the spec. If one fails, a gate is broken.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, approvals, drafter, runner, meta_publisher, postlog  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus, draft_post, TemplateGenerator  # noqa: E402
from agent.library import Creative  # noqa: E402
from agent.trust import TrustLevel, default_trust_for_new_account, requires_approval  # noqa: E402
from agent.voice import VoiceDoc, load_voice  # noqa: E402


# ---- helpers ----------------------------------------------------------------
def _acct(platform=Platform.FACEBOOK_PAGE, key="t_fb"):
    return Account(key=key, display_name="T", platform=platform,
                   token_env="T_TOKEN", target_id_env="T_ID")


def _voice():
    return VoiceDoc(raw="We help gym owners grow without burning out.\n#LASSO #GymGrowth",
                    hashtags=["#LASSO", "#GymGrowth"])


def _creative(note="Founders class, Saturday 9am."):
    return Creative(path="/lib/a.jpg", media_type="image", client_note=note,
                    public_url="https://cdn.example.com/a.jpg")


class FakePoster:
    def __init__(self):
        self.cards = []
        self.notices = []
    def post_approval_card(self, draft):
        self.cards.append(draft); return {"ok": True}
    def post_notice(self, text):
        self.notices.append(text); return {"ok": True}


class FakePublisher:
    def __init__(self):
        self.calls = 0
    def publish(self, draft, account):
        self.calls += 1
        return meta_publisher.PublishResult(ok=True, mode="published", media_id="REAL123")


class SpyLogger:
    def __init__(self):
        self.records = []
    def log_post(self, **kw):
        self.records.append(kw); return kw


# ---- GATE: master flag off does nothing -------------------------------------
def test_master_flag_off_does_nothing(monkeypatch):
    monkeypatch.delenv("AGENT_ENABLED", raising=False)
    poster = FakePoster()
    out = runner.run_daily(poster=poster)
    assert out["status"] == "disabled"
    assert poster.cards == [] and poster.notices == []


# ---- GATE: one draft per account --------------------------------------------
def test_daily_drafts_one_per_account(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    vp = tmp_path / "voice.md"
    vp.write_text("We help gyms grow.\n#LASSO", encoding="utf-8")
    lib = tmp_path / "lib"; lib.mkdir()
    (lib / "a.jpg").write_bytes(b"img")
    poster = FakePoster()
    accts = [_acct(key="a"), _acct(key="b"), _acct(key="c")]
    from agent.store import PendingStore
    out = runner.run_daily(poster=poster, voice_path=str(vp),
                           library_path=str(lib), accounts=accts,
                           scheduled_for="2026-07-01T12:00:00Z",  # Wednesday, a posting day
                           store=PendingStore(path=str(tmp_path / "pend.json")))
    assert out["status"] == "drafted"
    assert len(poster.cards) == 3  # exactly one per account


# ---- GATE: missing voice doc blocks drafting --------------------------------
def test_missing_voice_blocks_drafting(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    poster = FakePoster()
    out = runner.run_daily(poster=poster, voice_path=str(tmp_path / "nope.md"),
                           library_path=str(tmp_path))
    assert out["status"] == "no_voice"
    assert poster.cards == []
    assert len(poster.notices) == 1  # surfaced, drafted nothing


def test_drafter_blocks_when_voice_none():
    d = draft_post(_acct(), _creative(), "2026-07-01T12:00:00Z", voice=None,
                   voice_path="/does/not/exist.md")
    assert d.status == DraftStatus.BLOCKED
    assert "voice" in d.blocked_reason.lower()
    assert d.caption == ""


# ---- GATE: approval required; non-approver denied ---------------------------
def test_non_approver_denied():
    d = draft_post(_acct(), _creative(), "t", voice=_voice())
    res = approvals.handle_action("approve", d, actor_slack_id="U_RANDOM",
                                  publisher=FakePublisher(), logger=SpyLogger())
    assert res.ok is False
    assert "not the approver" in res.detail.lower()


def test_approver_can_approve(monkeypatch):
    monkeypatch.setattr(config, "APPROVER_SLACK_ID", "U06EPUUCL13")
    d = draft_post(_acct(), _creative(), "t", voice=_voice())
    pub, log = FakePublisher(), SpyLogger()
    res = approvals.handle_action("approve", d, actor_slack_id="U06EPUUCL13",
                                  publisher=pub, logger=log, account=_acct())
    assert res.ok is True
    assert pub.calls == 1
    assert len(log.records) == 1


# ---- GATE: draft-only mode never writes to Meta -----------------------------
def test_draft_only_makes_no_network_call(monkeypatch):
    monkeypatch.delenv("AGENT_PUBLISH_ENABLED", raising=False)  # OFF

    class ExplodingHTTP:
        def post(self, *a, **k):
            raise AssertionError("Network call attempted in draft-only mode!")

    d = draft_post(_acct(), _creative(), "t", voice=_voice())
    result = meta_publisher.publish(d, _acct(), http=ExplodingHTTP())
    assert result.mode == "would_publish"
    assert result.ok is True


def test_publish_armed_routes_to_network(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("T_TOKEN", "secrettoken")
    monkeypatch.setenv("T_ID", "PAGE123")

    class CaptureHTTP:
        def __init__(self): self.posted = []
        def post(self, url, data=None, timeout=None, **k):
            self.posted.append((url, data))
            class R:
                status_code = 200
                def json(self): return {"id": "FB_OK", "post_id": "FB_OK"}
            return R()

    http = CaptureHTTP()
    acct = _acct(platform=Platform.FACEBOOK_PAGE)
    d = draft_post(acct, _creative(), "t", voice=_voice())
    result = meta_publisher.publish(d, acct, http=http)
    assert result.mode == "published"
    assert len(http.posted) == 1
    # token must be passed to Meta but NEVER appear in our logs (see log test)


# ---- GATE: personal FB profile is not publishable ---------------------------
def test_personal_profile_not_supported(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("T_TOKEN", "x")
    d = draft_post(_acct(platform=Platform.PERSONAL), _creative(), "t", voice=_voice())
    with pytest.raises(meta_publisher.NotSupported):
        meta_publisher.publish(d, _acct(platform=Platform.PERSONAL))


# ---- GATE: trust ladder — new account starts at full approval ---------------
def test_new_account_starts_full_approval():
    assert default_trust_for_new_account() == TrustLevel.FULL_APPROVAL
    a = _acct()
    assert a.trust == TrustLevel.FULL_APPROVAL


def test_requires_approval_always_true_in_stage1():
    a = _acct()
    a.trust = TrustLevel.TRUSTED  # even if someone bumps it
    d = draft_post(a, _creative(), "t", voice=_voice())
    assert requires_approval(a, d) is True


# ---- GATE: no fabrication — caption only from approved sources --------------
def test_caption_composed_only_from_approved_sources():
    voice = _voice()
    creative = _creative(note="Founders class, Saturday 9am.")
    caption, hashtags, fragments = TemplateGenerator().build(voice, creative)
    # every fragment must come from the client note or a line in the voice doc
    voice_lines = [l.strip() for l in voice.text.splitlines()]
    for frag in fragments:
        assert frag == creative.client_note or frag in voice_lines
    # hashtags must be a subset of the doc's hashtags (none invented)
    assert set(hashtags).issubset(set(voice.hashtags))


# ---- GATE: token never lands in the post log --------------------------------
def test_post_log_never_contains_token(tmp_path, monkeypatch):
    p = tmp_path / "log.jsonl"
    rec = postlog.log_post(account_key="t_fb", platform="facebook_page",
                           caption="hi", media_id="M1", mode="would_publish",
                           draft_id="d1", path=str(p))
    assert "token" not in rec
    body = p.read_text(encoding="utf-8")
    assert "token" not in body.lower()


def test_account_repr_has_no_secret(monkeypatch):
    monkeypatch.setenv("T_TOKEN", "SUPERSECRET")
    a = _acct()
    assert "SUPERSECRET" not in repr(a)


# ---- Edit path re-drafts and re-holds ---------------------------------------
def test_edit_redrafts_and_holds(monkeypatch):
    monkeypatch.setattr(config, "APPROVER_SLACK_ID", "U06EPUUCL13")
    d = draft_post(_acct(), _creative(), "t", voice=_voice())

    def redraft_fn(old, note):
        new = draft_post(_acct(), _creative(note=note), "t", voice=_voice())
        return new

    res = approvals.handle_action("edit", d, actor_slack_id="U06EPUUCL13",
                                  note="make it punchier", redraft_fn=redraft_fn)
    assert res.ok is True
    assert res.redraft is not None
    assert res.redraft.status == DraftStatus.PENDING


# ---- Skip drops the draft ---------------------------------------------------
def test_skip_drops_draft(monkeypatch):
    monkeypatch.setattr(config, "APPROVER_SLACK_ID", "U06EPUUCL13")
    d = draft_post(_acct(), _creative(), "t", voice=_voice())
    res = approvals.handle_action("skip", d, actor_slack_id="U06EPUUCL13")
    assert res.ok is True
    assert d.status == DraftStatus.SKIPPED


# ---- Store: round-trips a draft, never holds a token -------------------------
def test_store_roundtrip_and_no_token(tmp_path):
    from agent.store import PendingStore
    s = PendingStore(path=str(tmp_path / "p.json"))
    d = draft_post(_acct(), _creative(), "2026-07-01T10:00:00Z", voice=_voice())
    s.put(d)
    got = s.get(d.draft_id)
    assert got is not None and got.caption == d.caption
    assert len(s.list_pending()) == 1
    body = (tmp_path / "p.json").read_text(encoding="utf-8")
    assert "token" not in body.lower()
    assert s.remove(d.draft_id) is True
    assert s.get(d.draft_id) is None
