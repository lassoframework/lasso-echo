"""
Ops alerts tests. Fully OFFLINE: recording posters only, no Slack, no network.
Asserts: the flag defaults OFF and OFF is a true no-op (the poster is never even
built); ON, each of the five failure call sites (hosting failed, creative empty,
plan blocked, publish failed, store write failed) emits exactly ONE "ECHO ALERT:"
line; scrub() redacts secret-looking env values; and a failed alert post only
logs, never raises.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import (approvals, config, creative_studio, daily_studio,  # noqa: E402
                   media_host, ops_alerts)
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import Draft, DraftStatus  # noqa: E402
from agent.store import PendingStore  # noqa: E402


class RecordingPoster:
    def __init__(self):
        self.notices = []
        self.chat_posts = []  # (text, channel) — the ops-fix cross-post path

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}

    def _chat_post(self, text, blocks=None, channel=None, thread_ts=None):
        self.chat_posts.append((text, channel))
        return {"ok": True}


class ExplodingPoster:
    def post_notice(self, text):
        raise RuntimeError("slack is down")


def _wire(monkeypatch):
    """Arm the flag and intercept the default poster so no call site can ever
    reach a real SlackPoster in tests."""
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    rec = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)
    return rec


def _alerts(rec):
    return [n for n in rec.notices if n.startswith("ECHO ALERT: ")]


# ---- 1. flag defaults OFF and OFF is a true no-op ------------------------------
def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_OPS_ALERTS_ENABLED", raising=False)
    assert config.ops_alerts_enabled() is False


def test_alert_is_noop_when_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_OPS_ALERTS_ENABLED", raising=False)
    # the default poster must never even be constructed while dormant
    monkeypatch.setattr(ops_alerts, "_default_poster",
                        lambda: (_ for _ in ()).throw(AssertionError("poster built")))
    assert ops_alerts.alert("something failed") is None


def test_force_bypasses_flag_for_self_gated_callers(monkeypatch):
    monkeypatch.delenv("AGENT_OPS_ALERTS_ENABLED", raising=False)
    rec = RecordingPoster()
    assert ops_alerts.alert("token expiring", poster=rec, force=True) is not None
    assert rec.notices == ["ECHO ALERT: token expiring"]


def test_alert_posts_one_prefixed_line_when_on(monkeypatch):
    rec = _wire(monkeypatch)
    ops_alerts.alert("hosting failed for x.png")
    assert rec.notices == ["ECHO ALERT: hosting failed for x.png"]


# ---- 2. scrub: secret env values never leave the module ------------------------
def test_scrub_redacts_secret_env_values(monkeypatch):
    monkeypatch.setenv("SOME_FAKE_TOKEN", "tok_abcdef123456")
    monkeypatch.setenv("SOME_FAKE_SECRET", "hush_hush_value")
    monkeypatch.setenv("SOME_FAKE_KEY", "key_9876543210")
    monkeypatch.setenv("SOME_FAKE_PASSWORD", "p4ssw0rd_long")
    out = ops_alerts.scrub("boom: tok_abcdef123456 hush_hush_value "
                           "key_9876543210 p4ssw0rd_long")
    for secret in ("tok_abcdef123456", "hush_hush_value",
                   "key_9876543210", "p4ssw0rd_long"):
        assert secret not in out
    assert out.count("[REDACTED]") == 4


def test_scrub_leaves_short_flaglike_values_alone(monkeypatch):
    monkeypatch.setenv("SOME_FLAG_KEY", "true")
    assert ops_alerts.scrub("a true story") == "a true story"


def test_alert_text_is_scrubbed(monkeypatch):
    rec = _wire(monkeypatch)
    monkeypatch.setenv("LEAKY_FAKE_TOKEN", "tok_leaked_value_1")
    ops_alerts.alert("upload failed: tok_leaked_value_1 rejected")
    assert "tok_leaked_value_1" not in rec.notices[0]
    assert "[REDACTED]" in rec.notices[0]


# ---- 3. a failed alert post only logs, never raises ----------------------------
def test_failed_alert_post_only_logs(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    assert ops_alerts.alert("boom", poster=ExplodingPoster()) is None  # no raise
    assert "[ops-alerts]" in capsys.readouterr().out


# ---- 3b. ops-fix cross-post to #echosupport (2026-09-02) -----------------------
# Blake: "it should go to echo support that is already wired." A NEEDS_TRIAGE alert
# additionally cross-posts into #echosupport as "OPS-FIX REQUEST: ...", where
# scout-listener's ops-fix-triage.js relays it. #echoclaude keeps getting every
# alert unchanged either way (asserted below) — this is purely additive.

def _wire_ops_fix(monkeypatch, support_channel="C_ECHOSUPPORT"):
    rec = _wire(monkeypatch)
    monkeypatch.setenv("AGENT_OPS_FIX_TRIAGE_ENABLED", "true")
    monkeypatch.setattr(config, "support_channel_id", lambda: support_channel)
    return rec


def test_ops_fix_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_OPS_FIX_TRIAGE_ENABLED", raising=False)
    assert config.ops_fix_triage_enabled() is False


def test_needs_triage_alert_cross_posts_when_armed(monkeypatch):
    rec = _wire_ops_fix(monkeypatch)
    # a real needs_triage shape from tonight (see tests/test_ops_triage.py)
    ops_alerts.alert("intake ingest ABORTED for piercefitness: InternalError")
    assert rec.notices == ["ECHO ALERT: intake ingest ABORTED for piercefitness: "
                           "InternalError"], "the ORIGINAL #echoclaude post is unchanged"
    assert len(rec.chat_posts) == 1
    text, channel = rec.chat_posts[0]
    assert channel == "C_ECHOSUPPORT"
    assert text.startswith("OPS-FIX REQUEST: ECHO ALERT: intake ingest ABORTED")


def test_noise_alert_never_cross_posts(monkeypatch):
    rec = _wire_ops_fix(monkeypatch)
    ops_alerts.alert("gritx: re-dated 4 expired row(s) into open day(s) "
                     "2026-10-01..2026-10-02 (approvals preserved). No action needed.")
    assert len(rec.notices) == 1, "#echoclaude still gets it"
    assert rec.chat_posts == [], "but noise never reaches #echosupport"


def test_cross_post_off_when_flag_unset(monkeypatch):
    rec = _wire(monkeypatch)  # AGENT_OPS_FIX_TRIAGE_ENABLED left unset
    monkeypatch.setattr(config, "support_channel_id", lambda: "C_ECHOSUPPORT")
    ops_alerts.alert("intake ingest ABORTED for piercefitness")
    assert rec.chat_posts == []


def test_cross_post_off_when_no_support_channel_configured(monkeypatch):
    rec = _wire(monkeypatch)
    monkeypatch.setenv("AGENT_OPS_FIX_TRIAGE_ENABLED", "true")
    monkeypatch.setattr(config, "support_channel_id", lambda: "")
    ops_alerts.alert("intake ingest ABORTED for piercefitness")
    assert rec.chat_posts == []


def test_cross_post_failure_never_affects_the_primary_alert(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    monkeypatch.setenv("AGENT_OPS_FIX_TRIAGE_ENABLED", "true")
    monkeypatch.setattr(config, "support_channel_id", lambda: "C_ECHOSUPPORT")

    class FlakyOnChatPost:
        def __init__(self):
            self.notices = []

        def post_notice(self, text):
            self.notices.append(text)
            return {"ok": True}

        def _chat_post(self, **kwargs):
            raise RuntimeError("echosupport post failed")

    poster = FlakyOnChatPost()
    result = ops_alerts.alert("intake ingest ABORTED for piercefitness", poster=poster)
    assert result is not None, "the primary #echoclaude post still succeeded"
    assert poster.notices == ["ECHO ALERT: intake ingest ABORTED for piercefitness"]
    assert "[ops-alerts]" in capsys.readouterr().out


def test_force_bypassed_alert_still_cross_posts(monkeypatch):
    # force=True (self-gated callers) still reaches the primary post; the cross-post
    # rides that same success path, independent of AGENT_OPS_ALERTS_ENABLED itself.
    monkeypatch.delenv("AGENT_OPS_ALERTS_ENABLED", raising=False)
    monkeypatch.setenv("AGENT_OPS_FIX_TRIAGE_ENABLED", "true")
    monkeypatch.setattr(config, "support_channel_id", lambda: "C_ECHOSUPPORT")
    rec = RecordingPoster()
    ops_alerts.alert("intake ingest ABORTED for piercefitness", poster=rec, force=True)
    assert len(rec.chat_posts) == 1


# ---- 4. call site: media hosting failure ---------------------------------------
def _host_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    monkeypatch.setattr(config, "S3_MAX_RETRIES", 1)  # no retry sleeps in tests
    art = tmp_path / "creative.png"
    art.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    return str(art)


class ExplodingS3:
    def exists(self, key):
        return False

    def put(self, key, local_path):
        raise RuntimeError("R2 exploded")


def test_hosting_failure_alerts_once_and_still_falls_back(monkeypatch, tmp_path):
    rec = _wire(monkeypatch)
    path = _host_setup(monkeypatch, tmp_path)
    assert media_host.host_media(path, "gym_ig", client=ExplodingS3()) is None
    alerts = _alerts(rec)
    assert len(alerts) == 1
    assert "media hosting failed" in alerts[0]
    assert "RuntimeError" in alerts[0]            # exception class surfaces


def test_hosting_failure_silent_fallback_unchanged_when_off(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_OPS_ALERTS_ENABLED", raising=False)
    monkeypatch.setattr(ops_alerts, "_default_poster",
                        lambda: (_ for _ in ()).throw(AssertionError("poster built")))
    path = _host_setup(monkeypatch, tmp_path)
    assert media_host.host_media(path, "gym_ig", client=ExplodingS3()) is None


# ---- 5. call site: creative generation returned empty --------------------------
SOURCE_DOC = """# LASSO Now

## Pillars
- Speed To Lead

## Pillar copy bank

### Pillar: Speed To Lead
Hook: Leads go cold in minutes.
Body: Answer inside five minutes and you book three times more.
"""


def _acct(key="lasso_ig"):
    return Account(key=key, display_name=key, platform=Platform.INSTAGRAM,
                   token_env="OPS_TEST_TOKEN", target_id_env="OPS_TEST_TARGET")


def test_empty_generation_alerts_once(monkeypatch, tmp_path):
    rec = _wire(monkeypatch)
    monkeypatch.setenv("AGENT_CONTENT_BRAIN_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    monkeypatch.setattr(creative_studio, "generate", lambda *a, **k: None)
    doc = tmp_path / "lasso_now.md"
    doc.write_text(SOURCE_DOC, encoding="utf-8")
    out = daily_studio.build_daily_infographic_draft(
        _acct(), "2026-07-01", s3_client=None, source_path=str(doc))
    assert out is None                             # library fallback unchanged
    alerts = _alerts(rec)
    assert len(alerts) == 1
    assert "creative generation returned empty" in alerts[0]


# ---- 6. call site: content plan blocked ----------------------------------------
def test_blocked_plan_alerts_once(monkeypatch, tmp_path):
    rec = _wire(monkeypatch)
    monkeypatch.setenv("AGENT_CONTENT_BRAIN_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    out = daily_studio.build_daily_infographic_draft(
        _acct(), "2026-07-01", source_path=str(tmp_path / "missing.md"))
    assert out.status == DraftStatus.BLOCKED       # still surfaces as a card
    alerts = _alerts(rec)
    assert len(alerts) == 1
    assert "content plan blocked" in alerts[0]


# ---- 7. call site: publish attempt failed ---------------------------------------
class ExplodingPublisher:
    def publish(self, draft, account):
        raise RuntimeError("Graph API said no")


def test_publish_failure_alerts_once_and_still_raises(monkeypatch):
    rec = _wire(monkeypatch)
    draft = Draft(draft_id="pub1", account_key="gym_ig", platform="instagram",
                  caption="x", hashtags=[], creative_path="a.png",
                  creative_public_url="", scheduled_for="2026-07-01T18:30:00+00:00")
    with pytest.raises(RuntimeError):              # behavior unchanged: still raises
        approvals.handle_action("approve", draft, config.APPROVER_SLACK_ID,
                                publisher=ExplodingPublisher(), account=_acct("gym_ig"))
    alerts = _alerts(rec)
    assert len(alerts) == 1
    assert "publish attempt failed" in alerts[0]
    assert "pub1" in alerts[0]


# ---- 8. call site: store write failed --------------------------------------------
def test_store_write_failure_alerts_once_and_still_raises(monkeypatch, tmp_path):
    rec = _wire(monkeypatch)
    store = PendingStore(path=str(tmp_path / "no_such_dir" / "pending.json"))
    draft = Draft(draft_id="st1", account_key="gym_ig", platform="instagram",
                  caption="x", hashtags=[], creative_path="a.png",
                  creative_public_url="", scheduled_for="2026-07-01T18:30:00+00:00")
    with pytest.raises(Exception):                 # behavior unchanged: still raises
        store.put(draft)
    alerts = _alerts(rec)
    assert len(alerts) == 1
    assert "store write failed" in alerts[0]
