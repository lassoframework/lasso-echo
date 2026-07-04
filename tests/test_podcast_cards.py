"""
Episode infographic tests (pipeline Part D). Offline (fake nano/S3). Asserts:
extracted concepts are VERBATIM transcript sentences and every generated card
resolves its podcast_ep<N> citation; an uncited card cannot enter the queue
(adversarial, loud); the queue spreads at most one card per day behind the
release card (which itself sits behind book priority in the runner slot, Part
B tests); copy rules hold (dash free, hook-first StoryBrand order, first
person we); the 18 existing house concepts are byte untouched; flag OFF =
inert everywhere.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, podcast_cards, podcast_feed, podcast_release  # noqa: E402
from agent import podcast_transcripts  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402
from agent.regen_library import CONCEPTS  # noqa: E402

from test_podcast_feed import FEED  # noqa: E402
from test_podcast_transcripts import TRANSCRIPT  # noqa: E402


class FakeNano:
    def __init__(self):
        self.prompts = []

    def generate_image(self, prompt, model):
        self.prompts.append(prompt)
        return b"\x89PNG\r\n\x1a\nFAKE"


class FakeS3:
    def exists(self, key):
        return False

    def put(self, key, local_path):
        pass


def _acct():
    return Account(key="lasso_ig", display_name="LASSO IG",
                   platform=Platform.INSTAGRAM, token_env="X", target_id_env="Y")


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PODCAST_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(lib))
    # cards_cli also writes episode learnings (Part E): keep them in tmp, the
    # real brand_voice/knowledge is never touched by a test
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", str(tmp_path / "knowledge"))


def _slot(day):
    return podcast_release.build_podcast_slot_draft(
        _acct(), day, nano_client=FakeNano(), s3_client=FakeS3())


# ---- extraction: verbatim, dash free, 2 or 3 --------------------------------------------
def test_extraction_verbatim_and_counted(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_transcripts.ingest(7, TRANSCRIPT, "test")
    for count in (2, 3):
        picks = podcast_cards.extract_concepts(7, count)
        assert len(picks) == count
        for hook, support in picks:
            assert podcast_transcripts.contains_verbatim(7, hook)
            assert podcast_transcripts.contains_verbatim(7, support)
            assert not podcast_cards._DASH_RE.search(hook + support)
    with pytest.raises(ValueError, match="count must be 2 or 3"):
        podcast_cards.extract_concepts(7, 4)
    with pytest.raises(ValueError, match="no transcript stored"):
        podcast_cards.extract_concepts(99, 2)


# ---- the adversarial gate: uncited cards cannot enter ------------------------------------
def test_uncited_card_cannot_enter_queue(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_transcripts.ingest(7, TRANSCRIPT, "test")
    before = len(podcast_cards.list_queue())
    with pytest.raises(ValueError, match="uncited card refused"):
        podcast_cards.enqueue(7, "We guarantee one hundred new members fast.",
                              "Follow up wins the month.")
    with pytest.raises(ValueError, match="uncited card refused"):
        podcast_cards.enqueue(7, "Follow up wins the month.",
                              "Invented support line that was never said aloud.")
    assert len(podcast_cards.list_queue()) == before               # nothing entered


def test_cli_queues_and_never_duplicates(monkeypatch, tmp_path, capsys):
    _arm(monkeypatch, tmp_path)
    podcast_transcripts.ingest(7, TRANSCRIPT, "test")
    podcast_cards.cards_cli(7, 2)
    assert len(podcast_cards.list_queue()) == 2
    podcast_cards.cards_cli(7, 2)                                  # idempotent re-run
    assert len(podcast_cards.list_queue()) == 2
    out = capsys.readouterr().out
    assert "podcast_ep7" in out and "held for approval" in out


# ---- serving: citations resolve, copy rules, 1/day spacing --------------------------------
def test_cards_resolve_citation_and_copy_rules(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_transcripts.ingest(7, TRANSCRIPT, "test")
    podcast_cards.cards_cli(7, 2)
    d = _slot("2026-07-06")
    assert d is not None and d.status == DraftStatus.PENDING
    assert d.draft_type == "podcast"
    assert podcast_cards.resolve_citation(d)                       # citation resolves
    assert f"cite:podcast_ep7" in d.source_fragments
    hook = d.caption.split("\n\n")[0]
    assert podcast_transcripts.contains_verbatim(7, hook)          # hook leads, verbatim
    assert "We break it down in episode 7 of our podcast." in d.caption  # first person we
    assert not podcast_cards._DASH_RE.search(d.caption)            # dash free


def test_spacing_max_one_card_per_day(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_transcripts.ingest(7, TRANSCRIPT, "test")
    podcast_cards.cards_cli(7, 2)
    d1 = _slot("2026-07-06")
    assert d1 is not None
    assert _slot("2026-07-06") is None                             # one per day, hard
    d2 = _slot("2026-07-07")
    assert d2 is not None and d2.caption != d1.caption             # next card, next day
    assert _slot("2026-07-08") is None                             # queue dry


def test_release_card_outranks_episode_cards(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_feed.poll(fetch=lambda: FEED, transcript_fetch=lambda u: TRANSCRIPT)
    podcast_cards.cards_cli(7, 2)
    d = _slot("2026-07-06")
    assert d.caption.startswith("EPISODE 7:")                      # release first
    d2 = _slot("2026-07-07")
    assert podcast_cards.resolve_citation(d2)                      # then the cards


def test_studio_unavailable_never_consumes_the_queue(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENT_NANO_ENABLED", raising=False)        # studio dark
    podcast_transcripts.ingest(7, TRANSCRIPT, "test")
    podcast_cards.cards_cli(7, 2)
    assert _slot("2026-07-06") is None
    assert all(c["status"] == "queued" for c in podcast_cards.list_queue())


# ---- the 18 existing concepts are byte untouched --------------------------------------------
def test_existing_18_concepts_byte_untouched(monkeypatch, tmp_path):
    snapshot = json.dumps(CONCEPTS, sort_keys=True)
    assert len(CONCEPTS) == 46         # 16 house + 10 b2b + 10 platform + 10 ads
    house = {k: v for k, v in CONCEPTS.items()
             if v.get("set") not in ("b2b", "platform", "platform_ads")}
    renders = len(house) + sum(1 for v in house.values() if v.get("story"))
    assert renders == 18                           # the original 18 renders
    assert all("podcast" not in k for k in CONCEPTS)               # nothing added there
    # run the whole Part D flow, then prove the concept library did not move
    _arm(monkeypatch, tmp_path)
    podcast_transcripts.ingest(7, TRANSCRIPT, "test")
    podcast_cards.cards_cli(7, 3)
    _slot("2026-07-06")
    assert json.dumps(CONCEPTS, sort_keys=True) == snapshot


# ---- flag off = inert -----------------------------------------------------------------------
def test_flag_off_everything_dark(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    assert podcast_cards.build_card_draft(_acct(), "2026-07-06") is None
    podcast_cards.cards_cli(7, 2)
    assert "OFF" in capsys.readouterr().out
    assert podcast_cards.list_queue() == []
