"""
Standing claim promotion tests (pipeline Part F). Offline. Asserts, hard:
NO TAP NO WRITE (a carded proposal changes nothing; a non approver's approve
changes nothing, adversarial); a proposal conflicting with a book claim blocks
with the conflict NAMED and is never carded approvable; an approved promotion
lands in the claims source with its podcast citation intact and then clears
the fabrication gate; skip drops without writing; only quantitative or named
framework learnings propose; flag OFF = zero behavior change.
"""

import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, knowledge, podcast_promote, podcast_transcripts, rotation  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402
from agent.store import PendingStore  # noqa: E402

REAL_KNOWLEDGE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "brand_voice", "knowledge")

QUANT_QUOTE = ("Our blended cost per lead across the portfolio is $16 right "
               "now. One audit cycle this spring flagged over $17,000 in wasted spend.")
PLAIN_QUOTE = "Most gyms do not have a lead problem."
CONFLICT_QUOTE = "Over 1,000 gym owners have run this exact play with us."
TRANSCRIPT = (f"Welcome back to LASSO Now. {PLAIN_QUOTE} {QUANT_QUOTE} "
              f"{CONFLICT_QUOTE} That is the whole show.")


class RecordingPoster:
    def __init__(self):
        self.cards, self.notices = [], []

    def post_approval_card(self, draft):
        self.cards.append(draft)
        return {"ok": True}

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PODCAST_ENABLED", "true")
    kdir = tmp_path / "knowledge"
    kdir.mkdir(exist_ok=True)
    shutil.copy(os.path.join(REAL_KNOWLEDGE, "02_verified_stats.md"),
                kdir / "02_verified_stats.md")
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", str(kdir))
    podcast_transcripts.ingest(7, TRANSCRIPT, "test")
    return kdir


def _learning(quote):
    return {"takeaway": quote.split(". ")[0] + ".", "quote": quote,
            "tags": ["know your numbers"]}


def _claims(kdir):
    return (kdir / "02_verified_stats.md").read_text(encoding="utf-8")


# ---- proposing: carded, never written; only promotable learnings ----------------------
def test_proposal_cards_but_never_writes(monkeypatch, tmp_path):
    kdir = _arm(monkeypatch, tmp_path)
    before = _claims(kdir)
    poster, store = RecordingPoster(), PendingStore()
    d = podcast_promote.propose(7, _learning(QUANT_QUOTE), poster, store)
    assert d is not None and d.status == DraftStatus.PENDING
    assert d.draft_type == "claim_promotion"
    assert "PROPOSED STANDING CLAIM" in d.caption
    assert "podcast_ep7" in d.caption                       # citation on the card
    assert podcast_promote.proposed_line(7, QUANT_QUOTE) in d.caption  # the exact line
    assert poster.cards and store.get(d.draft_id) is not None
    assert _claims(kdir) == before                          # NO TAP NO WRITE


def test_only_quantitative_or_framework_proposes(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    assert podcast_promote.propose(7, _learning(PLAIN_QUOTE),
                                   RecordingPoster(), PendingStore()) is None
    assert podcast_promote.promotion_reason("$16 per lead") == "quantitative claim"
    assert podcast_promote.promotion_reason(
        "The Halo Effect lifts everything.") == "named framework"
    assert podcast_promote.promotion_reason("Follow up wins the month.") == ""


def test_paraphrase_cannot_propose(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="not verbatim"):
        podcast_promote.propose(7, _learning("Our blended CPL is about $16."),
                                RecordingPoster(), PendingStore())


# ---- the tap: the only write path; adversarial actors and conflicts --------------------
def test_no_approver_no_write_adversarial(monkeypatch, tmp_path):
    kdir = _arm(monkeypatch, tmp_path)
    d = podcast_promote.propose(7, _learning(QUANT_QUOTE),
                                RecordingPoster(), PendingStore())
    before = _claims(kdir)
    res = podcast_promote.handle_promotion_action("approve", d, "U_INTRUDER")
    assert res.ok is False and "not the approver" in res.detail
    assert _claims(kdir) == before                          # byte untouched


def test_skip_drops_without_writing(monkeypatch, tmp_path):
    kdir = _arm(monkeypatch, tmp_path)
    d = podcast_promote.propose(7, _learning(QUANT_QUOTE),
                                RecordingPoster(), PendingStore())
    before = _claims(kdir)
    res = podcast_promote.handle_promotion_action("skip", d,
                                                  config.APPROVER_SLACK_ID)
    assert res.ok and d.status == DraftStatus.SKIPPED
    assert _claims(kdir) == before


def test_approval_writes_line_with_citation_intact(monkeypatch, tmp_path):
    kdir = _arm(monkeypatch, tmp_path)
    d = podcast_promote.propose(7, _learning(QUANT_QUOTE),
                                RecordingPoster(), PendingStore())
    res = podcast_promote.handle_promotion_action("approve", d,
                                                  config.APPROVER_SLACK_ID)
    assert res.ok and d.status == DraftStatus.APPROVED
    text = _claims(kdir)
    line = podcast_promote.proposed_line(7, QUANT_QUOTE)
    assert line in text                                     # the EXACT line landed
    assert "podcast_ep7" in line                            # citation intact
    # a double tap never duplicates the claim
    podcast_promote.handle_promotion_action("approve", d, config.APPROVER_SLACK_ID)
    assert _claims(kdir).count(line) == 1
    # the promoted claim now clears the fabrication gate as a USE receipt
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")
    assert any(QUANT_QUOTE in s for s in knowledge.usable_stats())
    assert rotation.is_gate_clean(QUANT_QUOTE, rotation._approved_claims())


def test_book_conflict_blocks_and_names_it(monkeypatch, tmp_path):
    kdir = _arm(monkeypatch, tmp_path)
    before = _claims(kdir)
    poster, store = RecordingPoster(), PendingStore()
    out = podcast_promote.propose(7, _learning(CONFLICT_QUOTE), poster, store)
    assert out == {"status": "conflict", "conflicts": out["conflicts"]}
    assert "BOOK CONFLICT" in out["conflicts"][0]           # the conflict is NAMED
    assert "author bio" in out["conflicts"][0]
    assert poster.cards == []                               # never carded approvable
    assert any("CONFLICT" in n for n in poster.notices)     # flagged loud
    assert _claims(kdir) == before                          # blocked from promotion


def test_flag_off_zero_behavior(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    assert podcast_promote.propose(7, _learning(QUANT_QUOTE),
                                   RecordingPoster(), PendingStore()) is None
