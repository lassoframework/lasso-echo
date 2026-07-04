"""
Podcast transcript ingest tests (pipeline Part C). Offline. Asserts: ingest
round trips (text, file, url, vtt cleanup); the feed poll auto-ingests a
podcast:transcript url on a NEW episode; a transcript-backed claim clears the
fabrication gate ONLY through the episode-scoped wrapper (the global gate never
borrows it); a claim NOT in the transcript is blocked either way (adversarial);
transcript text never lands in logs beyond SNIPPET_LEN; flag OFF = the CLI
refuses, reads are empty, and the gate is exactly today's.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import podcast_feed, podcast_transcripts, rotation  # noqa: E402

from test_podcast_feed import FEED  # noqa: E402

TRANSCRIPT = (
    "Welcome back to LASSO Now. Most gyms do not have a lead problem. "
    "Our blended cost per lead across the portfolio is $16 right now. "
    "One audit cycle this spring flagged over $17,000 in wasted spend. "
    "Follow up wins the month. That is the whole show."
)

VTT = ("WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\n"
       "<v Blake>Our blended cost per lead across the portfolio is $16 right now.\n"
       "2\n00:00:04.000 --> 00:00:08.000\nFollow up wins the month.\n")


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_PODCAST_ENABLED", "true")


# ---- round trips ----------------------------------------------------------------------
def test_ingest_round_trips(monkeypatch, tmp_path):
    _arm(monkeypatch)
    out = podcast_transcripts.ingest(7, TRANSCRIPT, "test")
    assert out["citation"] == "podcast_ep7"
    assert podcast_transcripts.transcript_text(7) == TRANSCRIPT
    # file round trip (replaces: newest transcript wins)
    p = tmp_path / "ep7.txt"
    p.write_text(TRANSCRIPT + " Bonus closing line.", encoding="utf-8")
    podcast_transcripts.ingest_file(7, str(p))
    assert podcast_transcripts.transcript_text(7).endswith("Bonus closing line.")
    # url round trip with an injected fetch
    podcast_transcripts.ingest_url(9, "https://cdn.example.com/ep9.txt",
                                   fetch=lambda u: "Short show. One idea.")
    assert podcast_transcripts.transcript_text(9) == "Short show. One idea."
    # vtt cleanup: cue metadata gone, the spoken words intact
    podcast_transcripts.ingest(11, VTT, "test")
    text = podcast_transcripts.transcript_text(11)
    assert "-->" not in text and "WEBVTT" not in text
    assert "cost per lead across the portfolio is $16" in text


def test_empty_transcript_fails_loud(monkeypatch):
    _arm(monkeypatch)
    with pytest.raises(ValueError, match="empty"):
        podcast_transcripts.ingest(7, "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n", "t")


def test_citation_id_parses():
    assert podcast_transcripts.citation_id(7) == "podcast_ep7"
    assert podcast_transcripts.parse_citation("podcast_ep12") == 12
    assert podcast_transcripts.parse_citation("podcast_ep") is None
    assert podcast_transcripts.parse_citation("book_ch3") is None


# ---- auto ingest from the feed ----------------------------------------------------------
def test_feed_poll_auto_ingests_transcript(monkeypatch):
    _arm(monkeypatch)
    fetched = []

    def tfetch(url):
        fetched.append(url)
        return TRANSCRIPT

    podcast_feed.poll(fetch=lambda: FEED, transcript_fetch=tfetch)
    assert fetched == ["https://cdn.example.com/ep7.txt"]      # ep 6 has no transcript
    assert podcast_transcripts.transcript_text(7) == TRANSCRIPT
    # a re-poll detects nothing and fetches nothing again
    podcast_feed.poll(fetch=lambda: FEED, transcript_fetch=tfetch)
    assert len(fetched) == 1


def test_transcript_failure_never_undetects_episode(monkeypatch):
    _arm(monkeypatch)

    def boom(url):
        raise OSError("cdn down")

    new = podcast_feed.poll(fetch=lambda: FEED, transcript_fetch=boom)
    assert len(new) == 2                                       # detection stands
    assert podcast_transcripts.transcript_text(7) == ""        # nothing half-stored


# ---- the episode-scoped gate -------------------------------------------------------------
def test_scoped_citation_clears_gate(monkeypatch):
    _arm(monkeypatch)
    podcast_transcripts.ingest(7, TRANSCRIPT, "test")
    said = "Our blended cost per lead across the portfolio is $16 right now."
    # episode tagged: the transcript backs the claim
    assert podcast_transcripts.gate_clean_for_episode(said, 7)
    # inside a longer episode draft too
    assert podcast_transcripts.gate_clean_for_episode(
        "From this week's show. " + said, 7)
    # NOT episode tagged: the global gate never borrows a podcast stat
    assert not rotation.is_gate_clean(said, rotation._approved_claims())
    # and another episode's tag does not reach this transcript
    assert not podcast_transcripts.gate_clean_for_episode(said, 8)


def test_claim_not_in_transcript_blocked(monkeypatch):
    _arm(monkeypatch)
    podcast_transcripts.ingest(7, TRANSCRIPT, "test")
    for bogus in ("We recovered $99,000 in one week.",
                  "Our cost per lead is $4.",
                  "Members grew 80 percent in a month."):
        assert not podcast_transcripts.gate_clean_for_episode(bogus, 7), bogus


# ---- no log leakage ------------------------------------------------------------------------
def test_transcript_never_leaks_into_logs(monkeypatch, capsys):
    _arm(monkeypatch)
    long_text = " ".join(f"Sentence number {i} of the show." for i in range(200))
    podcast_transcripts.ingest(7, long_text, "test")
    podcast_transcripts.ingest_cli(7, "", "")  # bad args path prints usage only
    out = capsys.readouterr().out
    limit = podcast_transcripts.SNIPPET_LEN
    # no run of transcript longer than the snippet law appears anywhere
    assert long_text[: limit + 20] not in out
    assert "Sentence number 50" not in out                     # deep content absent


def test_cli_preview_capped_at_snippet(monkeypatch, tmp_path, capsys):
    _arm(monkeypatch)
    long_text = " ".join(f"Sentence number {i} of the show." for i in range(200))
    p = tmp_path / "t.txt"
    p.write_text(long_text, encoding="utf-8")
    podcast_transcripts.ingest_cli(7, str(p), "")
    out = capsys.readouterr().out
    limit = podcast_transcripts.SNIPPET_LEN
    assert long_text[:limit] in out                            # the allowed preview
    assert long_text[: limit + 40] not in out                  # and not a char more


# ---- flag off = zero change ------------------------------------------------------------------
def test_flag_off_everything_dark(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    assert podcast_transcripts.ingest(7, TRANSCRIPT, "test") is None
    assert podcast_transcripts.transcript_text(7) == ""
    assert podcast_transcripts.transcript_sentences(7) == []
    # the episode-scoped gate degrades to exactly the global gate (conservative)
    said = "Our blended cost per lead across the portfolio is $16 right now."
    assert not podcast_transcripts.gate_clean_for_episode(said, 7)
    podcast_transcripts.ingest_cli(7, "x.txt", "")
    assert "OFF" in capsys.readouterr().out
