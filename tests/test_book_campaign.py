"""
Book campaign tests. Offline (fake nano/S3, tmp doc copies for tampering).
Adversarial: a countdown with no LAUNCH DATE never drafts; tampered case study
numbers block; numbers-pending studies unselectable; queue posts verbatim and
in order; first person voice enforced; the cover style is scoped to book cards
only; the knowledge brain registers the book sources but never the LOCKED
section; everything inert while AGENT_BOOK_CAMPAIGN_ENABLED is OFF.
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import book_campaign, config, creative_studio, knowledge  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402

REPO_BOOK_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "knowledge")


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
    return Account(key="lasso_ig", display_name="LASSO IG", platform=Platform.INSTAGRAM,
                   token_env="X", target_id_env="Y")


def _arm(monkeypatch, tmp_path, book_dir=None):
    monkeypatch.setenv("AGENT_BOOK_CAMPAIGN_ENABLED", "true")
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(lib))
    monkeypatch.setattr(config, "BOOK_DIR", book_dir or REPO_BOOK_DIR)


def _tmp_docs(tmp_path):
    d = tmp_path / "book_docs"
    d.mkdir()
    for f in list(config.BOOK_SOURCE_FILES) + [config.BOOK_QUEUE_FILE]:
        shutil.copy(os.path.join(REPO_BOOK_DIR, f), d / f)
    return str(d)


def _draft(tmp_path, day="2026-07-06"):
    return book_campaign.build_book_draft(_acct(), day, nano_client=FakeNano(),
                                          s3_client=FakeS3())


# ---- inert when OFF ----------------------------------------------------------------
def test_inert_when_flag_off(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_BOOK_CAMPAIGN_ENABLED", raising=False)
    monkeypatch.setattr(config, "BOOK_DIR", REPO_BOOK_DIR)
    assert book_campaign.build_book_draft(_acct(), "2026-07-06") is None


# ---- locked blanks: the countdown NEVER drafts -------------------------------------
def test_locked_blanks_parsed_from_real_book():
    blanks = book_campaign.locked_blanks.__wrapped__() if hasattr(
        book_campaign.locked_blanks, "__wrapped__") else None
    # direct call against the real repo docs
    import agent.config as cfg
    old = cfg.BOOK_DIR
    cfg.BOOK_DIR = REPO_BOOK_DIR
    try:
        blanks = book_campaign.locked_blanks()
    finally:
        cfg.BOOK_DIR = old
    assert blanks["LAUNCH DATE"] is False
    assert blanks["BUY OR PREORDER LINK"] is False
    assert blanks["PRICE"] is False
    assert blanks["Subtitle of record"] is False


def test_countdown_with_no_date_never_drafts(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    # angles 9 to 11 are never in the rotation set
    angles = book_campaign.load_angles()
    assert angles[9]["blocked"] and angles[10]["blocked"] and angles[11]["blocked"]
    # a DIRECT attempt at the countdown blocks with the blank named
    d = book_campaign.build_angle_draft(_acct(), "2026-07-20", 9)
    assert d.status == DraftStatus.BLOCKED
    assert "LAUNCH DATE" in d.blocked_reason
    assert "never guessed" in d.blocked_reason.lower()
    # and the daily selection across two weeks never emits a countdown
    for day in (f"2026-07-{dd:02d}" for dd in range(6, 20)):
        out = book_campaign.build_book_draft(_acct(), day, nano_client=FakeNano(),
                                             s3_client=FakeS3())
        if out is not None and out.status == DraftStatus.PENDING:
            assert "countdown" not in out.caption.lower()
            assert "launch date" not in out.caption.lower()


# ---- queue verbatim, in order --------------------------------------------------------
def test_queue_verbatim_and_ordered(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    d1 = _draft(tmp_path, "2026-07-06")
    assert d1.status == DraftStatus.PENDING
    assert d1.caption.startswith("We wrote a book.")            # day 1, verbatim
    assert "Our book is called The Full Gym." in d1.caption
    assert d1.hashtags[0] == "#gymowner" and "#thefullgym" in d1.hashtags
    # the same day returns the SAME item (both accounts share it)
    d1b = _draft(tmp_path, "2026-07-06")
    assert d1b.caption == d1.caption
    # the next day advances to day 2, in order
    d2 = _draft(tmp_path, "2026-07-07")
    assert "The Full Gym was written to get you unstuck." in d2.caption
    assert "You are stuck." in d2.caption                        # verbatim body line


def test_real_week1_cards_resolve_from_repo_folder():
    """ALL SEVEN shipped content_library/book_campaign/ files resolve through the
    drafter's day<N> lookup: the real cover art (day 1, jpg) plus the six cards.
    Nothing in week 1 generates; every card is the premade artwork."""
    found1 = book_campaign._existing_card(1)
    assert found1 is not None and found1.endswith("day1_cover.jpg")
    for n, stem in [(2, "who_its_for"), (3, "three_levers"), (4, "quote_math"),
                    (5, "halo_effect"), (6, "surgeon_story"), (7, "pat_case_study")]:
        found = book_campaign._existing_card(n)
        assert found is not None, f"day {n} card missing"
        assert found.endswith(f"day{n}_{stem}.png"), found


def test_existing_card_used_instead_of_generating(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    card_dir = tmp_path / "book_cards"
    card_dir.mkdir()
    (card_dir / "day1_cover.png").write_bytes(b"THE COVER")
    monkeypatch.setattr(book_campaign, "BOOK_CARD_DIR", str(card_dir))
    nano = FakeNano()
    d = book_campaign.build_book_draft(_acct(), "2026-07-06", nano_client=nano,
                                       s3_client=FakeS3())
    assert d.creative_path.endswith("day1_cover.png")            # the premade card
    assert nano.prompts == []                                     # nothing generated


# ---- number exactness gate -----------------------------------------------------------
def test_tampered_numbers_block(monkeypatch, tmp_path):
    docs = _tmp_docs(tmp_path)
    _arm(monkeypatch, tmp_path, book_dir=docs)
    # tamper the queue's Pat caption with a figure in NO source doc (63)
    qpath = os.path.join(docs, config.BOOK_QUEUE_FILE)
    text = open(qpath, encoding="utf-8").read()
    text = text.replace("Twenty percent to sixty.", "Conversion hit 63 percent.")
    open(qpath, "w", encoding="utf-8").write(text)
    # walk the queue to day 7
    for i, day in enumerate(["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09",
                             "2026-07-10", "2026-07-11", "2026-07-12"]):
        d = book_campaign.build_book_draft(_acct(), day, nano_client=FakeNano(),
                                           s3_client=FakeS3())
    assert d.status == DraftStatus.BLOCKED                        # day 7 tampered
    assert "63" in d.blocked_reason and "never guessed" in d.blocked_reason.lower()


def test_numbers_pending_studies_unselectable(monkeypatch):
    monkeypatch.setattr(config, "BOOK_DIR", REPO_BOOK_DIR)
    studies = book_campaign.case_studies()
    for pending in (15, 16, 18, 19):
        assert pending not in studies
    assert 5 in studies and "30 40 30" in studies[5]              # Shayla, with numbers


# ---- first person voice ---------------------------------------------------------------
def test_first_person_on_book_captions(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    d = _draft(tmp_path, "2026-07-06")
    assert book_campaign.first_person_ok(d.caption)
    cs = book_campaign.build_case_study_draft(_acct(), "2026-07-06",
                                              nano_client=FakeNano(),
                                              s3_client=FakeS3())
    assert "our book The Full Gym" in cs.caption                  # credited, first person
    assert book_campaign.first_person_ok(cs.caption)
    assert not book_campaign.first_person_ok("A book called The Full Gym exists.")


# ---- cover style scoped to book cards only ----------------------------------------------
def test_cover_style_scoped_to_book_cards(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    # exhaust the queue quickly by jumping past it: force the angle path
    nano = FakeNano()
    d = book_campaign.build_angle_draft(_acct(), "2026-07-20", 3,
                                        nano_client=nano, s3_client=FakeS3())
    assert d is not None and d.status == DraftStatus.PENDING
    assert any("BLACK canvas" in p for p in nano.prompts)         # cover style
    assert all("#FAF6F0: THE canvas" not in p for p in nano.prompts)
    # the house spec is untouched everywhere else
    house = creative_studio.build_prompt("H", ["x"])
    assert "BLACK canvas" not in house
    assert "Cream #FAF6F0: THE canvas" in house


# ---- knowledge registration ----------------------------------------------------------------
def test_book_sources_registered_locked_section_excluded(monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setattr(config, "BOOK_DIR", REPO_BOOK_DIR)
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", "brand_voice/knowledge")
    corpus = knowledge.load_corpus()
    assert "full_gym_book.md" in corpus
    assert "full_gym_case_studies.md" in corpus
    assert "full_gym_launch_campaign.md" in corpus
    assert config.BOOK_QUEUE_FILE not in corpus                   # ops file, not source
    book_text = "\n".join(corpus["full_gym_book.md"])
    assert "Paid ads aren't magic" in book_text                   # quotable line in
    assert "LAUNCH DATE" not in book_text                          # LOCKED section OUT
    assert "BUY OR PREORDER LINK" not in book_text


# ---- master source conflict flags ------------------------------------------------------------
def test_conflict_flagged_on_card(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    d = _draft(tmp_path, "2026-07-06")                            # day 1 uses cover subtitle
    assert any("subtitle of record" in w.lower() for w in d.warnings)
