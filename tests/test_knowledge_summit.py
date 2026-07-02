"""
Knowledge brain + summit campaign tests. Fully OFFLINE (tmp knowledge folder, fake
nano/S3 clients). Adversarial: LOCKED / PENDING / NOT FOUND content and *_pending.md
files can never reach a draft; only USE-marked stats survive, wording exact. Summit:
weekly cap by day, 3-week angle rotation, the fixed CTA + link, the 2026-11-08
auto-stop, and both flags OFF = fully inert.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, knowledge, summit  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


ADVERSARIAL_DOC = """# 01 core facts

## Story (approved)
We started LASSO inside a real gym.

## Pricing history LOCKED
The old price was 199 and must never appear.

Secret line inside locked section.

## Stats
- USE: Gyms that answer in five minutes book three times more consults.
- 92 percent of gyms fail within a year (unverified, do not use).
- PENDING legal review: we guarantee results.
This claim is NOT FOUND in any source.
- USE: LASSO gyms average a 70 percent close rate on booked consults.
"""

SUMMIT_DOC = """# 04 summit campaign

## VERIFIED FACTS
- The LASSO Summit is November 7 to 8, 2026 in Boise.
- Every session is run by a working gym owner.

## APPROVED ANGLES
- Meet the owners actually doing the numbers.
- Two days that pay for themselves.
- Stop guessing. Copy what works.
- The room where gym math gets fixed.

## Draft ideas PENDING
- An angle that is not approved yet.
"""


class FakeNano:
    def generate_image(self, prompt, model):
        return b"\x89PNG\r\n\x1a\nFAKE"


class FakeS3:
    def exists(self, key):
        return False

    def put(self, key, local_path):
        pass


def _acct():
    return Account(key="lasso_ig", display_name="LASSO IG", platform=Platform.INSTAGRAM,
                   token_env="X", target_id_env="Y")


def _voice():
    return VoiceDoc(raw="x", hashtags=["#LASSOFramework"])


def _kdir(tmp_path):
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "01_core.md").write_text(ADVERSARIAL_DOC, encoding="utf-8")
    (kdir / "04_summit_campaign.md").write_text(SUMMIT_DOC, encoding="utf-8")
    (kdir / "03_social_proof_pending.md").write_text(
        "Quote: never use me directly\nPermission: yes", encoding="utf-8")
    (kdir / "05_ideas_pending.md").write_text("A whole pending file.", encoding="utf-8")
    return str(kdir)


def _arm_knowledge(monkeypatch, kdir):
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", kdir)


def _arm_summit(monkeypatch, tmp_path, kdir):
    monkeypatch.setenv("AGENT_SUMMIT_CAMPAIGN_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", kdir)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")


TUESDAY = "2026-07-07"       # config.SUMMIT_DAY default


def _summit(tmp_path, kdir, day=TUESDAY):
    return summit.build_summit_draft(_acct(), day, voice=_voice(),
                                     nano_client=FakeNano(), s3_client=FakeS3(),
                                     knowledge_dir=kdir)


# ---- knowledge gates (adversarial) ---------------------------------------------
def test_locked_pending_notfound_never_in_corpus(monkeypatch, tmp_path):
    kdir = _kdir(tmp_path)
    _arm_knowledge(monkeypatch, kdir)
    corpus = "\n".join("\n".join(v) for v in knowledge.load_corpus().values())
    assert "We started LASSO inside a real gym." in corpus       # approved survives
    assert "old price was 199" not in corpus                      # LOCKED section
    assert "Secret line inside locked section." not in corpus     # inside LOCKED
    assert "we guarantee results" not in corpus                   # PENDING line
    assert "NOT FOUND" not in corpus                              # NOT FOUND line
    assert "never use me directly" not in corpus                  # 03_social_proof_pending
    assert "A whole pending file." not in corpus                  # *_pending.md


def test_only_use_marked_stats_and_wording_exact(monkeypatch, tmp_path):
    _arm_knowledge(monkeypatch, _kdir(tmp_path))
    stats = knowledge.usable_stats()
    assert stats == [
        "Gyms that answer in five minutes book three times more consults.",
        "LASSO gyms average a 70 percent close rate on booked consults.",
    ]  # exact wording, exact order, and the unverified 92 percent stat is absent


def test_knowledge_inert_when_flag_off(monkeypatch, tmp_path):
    kdir = _kdir(tmp_path)
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", kdir)
    assert knowledge.load_corpus() == {}
    assert knowledge.usable_stats() == []


# ---- summit campaign --------------------------------------------------------------
def test_summit_draft_from_verified_facts_and_angles_only(monkeypatch, tmp_path):
    kdir = _kdir(tmp_path)
    _arm_summit(monkeypatch, tmp_path, kdir)
    d = _summit(tmp_path, kdir)
    assert d is not None and d.status == DraftStatus.PENDING
    assert "Claim your seat: https://lassoframework.com/summit" in d.caption
    # caption lines are verbatim from the approved blocks
    for frag in d.source_fragments[:2]:
        assert frag in SUMMIT_DOC
    assert "not approved yet" not in d.caption        # the PENDING angle never drafts


def test_summit_weekly_cap_only_fires_on_summit_day(monkeypatch, tmp_path):
    kdir = _kdir(tmp_path)
    _arm_summit(monkeypatch, tmp_path, kdir)
    assert _summit(tmp_path, kdir, day="2026-07-07") is not None   # Tuesday
    for day in ("2026-07-06", "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-12"):
        assert _summit(tmp_path, kdir, day=day) is None            # rest of the week


def test_summit_angle_rotation_no_repeat_within_3_weeks(monkeypatch, tmp_path):
    kdir = _kdir(tmp_path)
    _arm_knowledge(monkeypatch, kdir)  # not needed for pick_angle but harmless
    _, angles = summit.load_campaign(kdir)
    assert len(angles) == 4
    picks = [summit.pick_angle(angles, d) for d in
             ("2026-07-07", "2026-07-14", "2026-07-21")]           # 3 consecutive weeks
    assert len(set(picks)) == 3                                     # all distinct


def test_summit_auto_stop_after_nov_8(monkeypatch, tmp_path):
    kdir = _kdir(tmp_path)
    _arm_summit(monkeypatch, tmp_path, kdir)
    assert _summit(tmp_path, kdir, day="2026-11-03") is not None    # Tue, still on
    assert _summit(tmp_path, kdir, day="2026-11-10") is None        # Tue, after end
    assert summit.campaign_active("2026-11-08") is True             # inclusive
    assert summit.campaign_active("2026-11-09") is False


def test_summit_inert_when_flag_off(monkeypatch, tmp_path):
    kdir = _kdir(tmp_path)
    monkeypatch.delenv("AGENT_SUMMIT_CAMPAIGN_ENABLED", raising=False)
    assert _summit(tmp_path, kdir) is None


def test_summit_silently_absent_when_file_missing(monkeypatch, tmp_path):
    empty = tmp_path / "empty_knowledge"
    empty.mkdir()
    _arm_summit(monkeypatch, tmp_path, str(empty))
    assert _summit(tmp_path, str(empty)) is None
