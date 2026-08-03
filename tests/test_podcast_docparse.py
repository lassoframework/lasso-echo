"""Tests for agent.podcast_docparse - GMMS show-notes doc parsing (offline)."""

from agent import podcast_docparse as dp


DOC = """\
# Episode 140 Show Notes

## Episode Chapters
- 00:00 Intro and welcome
- 12:34 The funnel diagnostic order
- [01:02:03] Retention and the reclaim member
* 45:10 - Speed to lead beats everything

## Memorable Quotes
- "SPEED TO LEAD is the whole game, most gyms wait a full day."
- **Your close rate is the first leg to fix, not the last.** 12:34
- 45:10 A busy professional does not want a workout, they want a result.
"""


# ---- parse_timestamp -----------------------------------------------------------------
def test_parse_timestamp_mm_ss():
    assert dp.parse_timestamp("12:34") == 12 * 60 + 34


def test_parse_timestamp_h_mm_ss():
    assert dp.parse_timestamp("1:02:03") == 3600 + 2 * 60 + 3


def test_parse_timestamp_bracketed():
    assert dp.parse_timestamp("[12:34]") == 754


def test_parse_timestamp_whitespace():
    assert dp.parse_timestamp("  [ 45:10 ] ") == 45 * 60 + 10


def test_parse_timestamp_none_on_junk():
    assert dp.parse_timestamp("no timestamp here") is None
    assert dp.parse_timestamp("") is None
    assert dp.parse_timestamp(None) is None


def test_parse_timestamp_rejects_out_of_range():
    assert dp.parse_timestamp("12:99") is None


# ---- parse_chapters ------------------------------------------------------------------
def test_parse_chapters_count_and_order():
    chapters = dp.parse_chapters(DOC)
    assert len(chapters) == 4
    assert chapters[0]["ts_seconds"] == 0
    assert chapters[1]["ts_seconds"] == 754
    assert chapters[2]["ts_seconds"] == 3723


def test_parse_chapters_titles_strip_separator():
    chapters = dp.parse_chapters(DOC)
    assert chapters[1]["title"] == "The funnel diagnostic order"
    # "45:10 - Speed to lead beats everything" strips the leading dash separator
    assert chapters[3]["title"] == "Speed to lead beats everything"


def test_parse_chapters_keeps_raw():
    chapters = dp.parse_chapters(DOC)
    assert all(c["raw"] for c in chapters)


# ---- parse_quotes --------------------------------------------------------------------
def test_parse_quotes_count():
    quotes = dp.parse_quotes(DOC)
    assert len(quotes) == 3


def test_parse_quotes_no_timestamp():
    q = dp.parse_quotes(DOC)[0]
    assert q["ts_seconds"] is None
    assert q["text"].startswith("SPEED TO LEAD is the whole game")


def test_parse_quotes_trailing_timestamp_stripped():
    q = dp.parse_quotes(DOC)[1]
    assert q["ts_seconds"] == 754
    assert q["text"] == "Your close rate is the first leg to fix, not the last."


def test_parse_quotes_leading_timestamp_stripped():
    q = dp.parse_quotes(DOC)[2]
    assert q["ts_seconds"] == 2710
    assert q["text"].startswith("A busy professional")


def test_parse_quotes_keeps_raw():
    assert all(q["raw"] for q in dp.parse_quotes(DOC))


# ---- clip_candidates -----------------------------------------------------------------
def test_clip_candidates_quotes_primary_then_chapters():
    cands = dp.clip_candidates(DOC)
    # three quotes first (primary), then four chapters (secondary)
    assert len(cands) == 7
    assert [c["source"] for c in cands[:3]] == ["quote", "quote", "quote"]
    assert [c["source"] for c in cands[3:]] == ["chapter"] * 4


def test_clip_candidates_quote_shape():
    first = dp.clip_candidates(DOC)[0]
    assert first["source"] == "quote"
    assert first["title"] is None
    assert first["ts_seconds"] is None


def test_clip_candidates_chapter_shape():
    chap = dp.clip_candidates(DOC)[3]
    assert chap["source"] == "chapter"
    assert chap["title"] == chap["text"]
    assert isinstance(chap["ts_seconds"], int)


# ---- has_usable_doc ------------------------------------------------------------------
def test_has_usable_doc_true_for_full_doc():
    assert dp.has_usable_doc(DOC) is True


def test_has_usable_doc_true_with_only_quotes():
    doc = "Memorable Quotes\nThis is a hand picked money line from the show."
    assert dp.has_usable_doc(doc) is True


def test_has_usable_doc_true_with_only_chapters():
    doc = "Chapters\n05:00 A structural marker in the episode"
    assert dp.has_usable_doc(doc) is True


def test_has_usable_doc_false_when_blank():
    assert dp.has_usable_doc("") is False
    assert dp.has_usable_doc(None) is False


def test_has_usable_doc_false_when_no_section():
    assert dp.has_usable_doc("Just some random prose with no sections at all.") is False


def test_header_present_but_no_parseable_lines_is_empty_not_guessed():
    # A doc with headers but zero parseable lines under them: loud by emptiness,
    # so the caller falls back to transcript scoring rather than a guess.
    doc = "## Memorable Quotes\n\n## Episode Chapters\n"
    assert dp.has_usable_doc(doc) is False
    assert dp.clip_candidates(doc) == []


def test_chapters_section_with_untimestamped_lines_only_is_empty():
    doc = "Episode Chapters\nA line with no timestamp at all"
    assert dp.parse_chapters(doc) == []


# ---- messy input robustness ----------------------------------------------------------
def test_header_variations_and_bullets():
    doc = ("NOTABLE QUOTES\n"
           "  •  A quote behind a bullet glyph and messy   whitespace.\n"
           "+ Another quote behind a plus bullet.\n")
    quotes = dp.parse_quotes(doc)
    assert len(quotes) == 2
    assert quotes[0]["text"] == "A quote behind a bullet glyph and messy whitespace."
