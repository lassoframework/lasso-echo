"""
Social proof card tests. Fully OFFLINE: fake nano + S3 clients, tmp source files,
recording poster. Asserts: unverified/unpermissioned entries are SKIPPED with a
notice and never rendered; a missing or empty file is a silent no-op; the weekly
cap holds (proof day only); both templates render in both aspects with the V3
palette; the happy path yields a hosted PENDING draft whose source_fragments are
the verified entry's own lines; the flag defaults OFF.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, creative_studio, social_proof  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


GOOD_DOC = """# Social proof

## Entry
Quote: Your coaches actually check in on me.
Attribution: Sarah M., member since 2024
Permission: yes
Verified: 2026-06-28

## Entry
Stat: 18 pounds down in 12 weeks
Support: First strength block
Attribution: Mike R.
Permission: yes
Verified: 2026-06-30

## Entry
Quote: No verified date on this one.
Attribution: Pat Q.
Permission: yes

## Entry
Quote: No permission on this one.
Attribution: Lee W.
Permission: no
Verified: 2026-06-30
"""

WEDNESDAY = "2026-07-01"   # the default proof day
THURSDAY = "2026-07-02"


class FakeNano:
    def generate_image(self, prompt, model):
        return b"\x89PNG\r\n\x1a\nFAKE"


class FakeS3:
    def exists(self, key):
        return False

    def put(self, key, local_path):
        pass


class RecordingPoster:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


def _acct():
    return Account(key="lasso_ig", display_name="LASSO IG", platform=Platform.INSTAGRAM,
                   token_env="X", target_id_env="Y")


def _voice():
    return VoiceDoc(raw="We help gym owners grow.\n#LASSOFramework",
                    hashtags=["#LASSOFramework"], ctas=["Save this post."])


def _doc(tmp_path, content=GOOD_DOC):
    p = tmp_path / "social_proof.md"
    p.write_text(content, encoding="utf-8")
    return str(p)


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SOCIAL_PROOF_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path))
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")


def _build(tmp_path, day=WEDNESDAY, path=None, poster=None):
    return social_proof.build_social_proof_draft(
        _acct(), day, voice=_voice(), nano_client=FakeNano(), s3_client=FakeS3(),
        path=path if path is not None else _doc(tmp_path), poster=poster)


# ---- 1. unverified / unpermissioned entries skipped with a notice --------------
def test_unverified_entries_skipped_never_rendered(tmp_path):
    approved, skipped = social_proof.load_entries(_doc(tmp_path))
    assert len(approved) == 2                      # the two clean entries
    assert len(skipped) == 2
    reasons = {r for _, r in skipped}
    assert reasons == {"no verified date", "no permission on record"}


def test_skipped_entries_post_a_notice(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    poster = RecordingPoster()
    d = _build(tmp_path, poster=poster)
    assert d is not None                            # approved entries still draft
    assert len(poster.notices) == 2                 # one line per skipped entry
    assert all("skipped" in n.lower() for n in poster.notices)
    # and the skipped text never reaches the draft
    assert all("No verified date" not in f and "No permission" not in f
               for f in d.source_fragments)


# ---- 2. missing / empty file = silent no-op ------------------------------------
def test_missing_file_is_silent_noop(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    poster = RecordingPoster()
    d = _build(tmp_path, path=str(tmp_path / "nope.md"), poster=poster)
    assert d is None
    assert poster.notices == []                     # silent: no notice, no block


def test_empty_file_is_silent_noop(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    d = _build(tmp_path, path=_doc(tmp_path, content="  \n"))
    assert d is None


# ---- 3. weekly cap: proof day only ---------------------------------------------
def test_weekly_cap_only_fires_on_proof_day(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    assert _build(tmp_path, day=WEDNESDAY) is not None   # the one proof day
    for day in ("2026-06-29", "2026-06-30", THURSDAY, "2026-07-03", "2026-07-05"):
        assert _build(tmp_path, day=day) is None          # every other day: dormant


def test_entry_rotation_is_deterministic_by_week(tmp_path):
    approved, _ = social_proof.load_entries(_doc(tmp_path))
    a = social_proof.pick_entry(approved, "2026-07-01")
    b = social_proof.pick_entry(approved, "2026-07-01")
    assert a is b                                       # same week -> same entry
    c = social_proof.pick_entry(approved, "2026-07-08")
    assert c is not a                                   # next week rotates (2 entries)


# ---- 4. templates render (both kinds, both aspects, V3 palette) ---------------
def test_quote_card_template_renders_feed_and_story():
    feed = creative_studio.build_social_proof_prompt(
        "quote", "Your coaches actually check in on me.", attribution="Sarah M.")
    story = creative_studio.build_social_proof_prompt(
        "quote", "Your coaches actually check in on me.", attribution="Sarah M.",
        aspect="9:16", pixels="1080x1920", surface="story")
    for p in (feed, story):
        assert "QUOTE CARD" in p
        assert "Your coaches actually check in on me." in p
        assert "Sarah M." in p
        assert "no body sentences" in p.lower()
        for hexcode in ("#121E3C", "#FF0000", "#5EB9E6", "#FAF6F0"):
            assert hexcode in p
    assert "4:5" in feed and "1080x1350" in feed
    assert "9:16" in story and "1080x1920" in story


def test_number_card_template_renders_stat_support_attribution():
    p = creative_studio.build_social_proof_prompt(
        "stat", "18 pounds down in 12 weeks", support_line="First strength block",
        attribution="Mike R.")
    assert "NUMBER CARD" in p
    assert "18 pounds down in 12 weeks" in p
    assert "First strength block" in p
    assert "Mike R." in p
    assert "HUGE" in p


# ---- 5. happy path: hosted PENDING draft, fragments = verified entry text ------
def test_happy_path_pending_draft_from_verified_entry(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    d = _build(tmp_path)
    assert d.status == DraftStatus.PENDING
    assert d.creative_public_url.startswith("https://cdn.echo.test/echo/lasso_ig/")
    assert d.caption.strip() != ""
    assert d.source_fragments                       # every fragment is entry text
    for frag in d.source_fragments:
        assert frag in GOOD_DOC


# ---- 6. flag defaults OFF -------------------------------------------------------
def test_flag_defaults_off(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_SOCIAL_PROOF_ENABLED", raising=False)
    assert config.social_proof_enabled() is False
    assert _build(tmp_path) is None
