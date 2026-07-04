"""
Episode learnings memory tests (pipeline Part E). Offline. Asserts: every
learning's takeaway AND quote string match the stored transcript verbatim (a
paraphrased quote is refused, adversarial); files land under
knowledge/podcast/ with citation, title, date, and pillar taxonomy tags; the
index round trips; learnings are additive only (a re-run never edits a prior
file); existing knowledge files are byte untouched; written copy is dash free;
podcast-cards triggers the write; flag OFF = zero behavior change.
"""

import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, knowledge, podcast_feed, podcast_learn  # noqa: E402
from agent import podcast_transcripts  # noqa: E402

from test_podcast_feed import FEED  # noqa: E402
from test_podcast_transcripts import TRANSCRIPT  # noqa: E402

REAL_KNOWLEDGE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "brand_voice", "knowledge")


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PODCAST_ENABLED", "true")
    kdir = tmp_path / "knowledge"
    kdir.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", str(kdir))
    return kdir


def _store_ep7(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    podcast_feed.poll(fetch=lambda: FEED,
                      transcript_fetch=lambda u: TRANSCRIPT)
    podcast_transcripts.ingest(7, TRANSCRIPT, "test")


# ---- extraction: verbatim, 3 to 7, tagged from the pillar taxonomy -------------------
def test_learnings_verbatim_with_pillar_tags(monkeypatch, tmp_path):
    _store_ep7(monkeypatch, tmp_path)
    learnings = podcast_learn.extract_learnings(7)
    assert 3 <= len(learnings) <= 7
    for learning in learnings:
        assert podcast_transcripts.contains_verbatim(7, learning["takeaway"])
        assert podcast_transcripts.contains_verbatim(7, learning["quote"])
        assert learning["takeaway"] in learning["quote"]
        assert learning["tags"], learning
    taxonomy = {name for name, _ in podcast_learn._PILLAR_TAGS} | {"general"}
    for learning in learnings:
        assert set(learning["tags"]) <= taxonomy
    with pytest.raises(ValueError, match="no transcript stored"):
        podcast_learn.extract_learnings(99)


def test_paraphrased_quote_is_rejected(monkeypatch, tmp_path):
    _store_ep7(monkeypatch, tmp_path)
    paraphrase = {"takeaway": "Most gyms do not have a lead problem.",
                  "quote": "Gyms mostly have follow up problems, not lead "
                           "problems.",                       # close, but NOT said
                  "tags": ["general"]}
    with pytest.raises(ValueError, match="not verbatim"):
        podcast_learn.verify_learning(7, paraphrase)
    # adversarial: wire the paraphrase into the write path; NOTHING lands
    monkeypatch.setattr(podcast_learn, "extract_learnings",
                        lambda *a, **k: [paraphrase])
    with pytest.raises(ValueError, match="not verbatim"):
        podcast_learn.write_learnings(7)
    assert not os.path.exists(podcast_learn._learnings_path(7))
    assert podcast_learn.read_index() == []


# ---- writing: file shape, citation, dash free, additive only --------------------------
def test_write_learnings_file_and_index_round_trip(monkeypatch, tmp_path):
    _store_ep7(monkeypatch, tmp_path)
    out = podcast_learn.write_learnings(7)
    assert out["existed"] is False and out["learnings"] >= 3
    text = open(out["path"], encoding="utf-8").read()
    assert "PODCAST EPISODE 7 LEARNINGS" in text
    assert "podcast_ep7" in text                            # citation id
    assert "The follow up problem" in text                  # episode title
    assert "TAKEAWAY:" in text and "QUOTE:" in text and "TAGS:" in text
    # dash free content: beyond the markdown bullet marker, no line carries
    # any dash family character (takeaways, quotes, tags, title, dates)
    for raw in text.splitlines():
        body = raw.lstrip("- ").strip()
        assert not podcast_learn._DASH_RE.search(body), raw
    # the index round trips
    entries = podcast_learn.read_index()
    assert len(entries) == 1
    e = entries[0]
    assert e["episode"] == 7 and e["count"] == out["learnings"]
    assert "follow up problem" in e["title"].lower()


def test_additive_only_rerun_never_edits(monkeypatch, tmp_path):
    _store_ep7(monkeypatch, tmp_path)
    out = podcast_learn.write_learnings(7)
    before = open(out["path"], encoding="utf-8").read()
    again = podcast_learn.write_learnings(7)                # re-run refuses
    assert again["existed"] is True and again["learnings"] == 0
    assert open(out["path"], encoding="utf-8").read() == before
    assert len(podcast_learn.read_index()) == 1             # no duplicate line


def test_existing_knowledge_files_byte_untouched(monkeypatch, tmp_path):
    kdir = _arm(monkeypatch, tmp_path)
    # seed the tmp knowledge dir with REAL knowledge files, then run the write
    seeded = {}
    for name in ("00_README.md", "02_verified_stats.md", "06_content_pillars.md"):
        shutil.copy(os.path.join(REAL_KNOWLEDGE, name), kdir / name)
        seeded[name] = (kdir / name).read_bytes()
    podcast_feed.poll(fetch=lambda: FEED, transcript_fetch=lambda u: TRANSCRIPT)
    podcast_transcripts.ingest(7, TRANSCRIPT, "test")
    podcast_learn.write_learnings(7)
    for name, blob in seeded.items():
        assert (kdir / name).read_bytes() == blob, name
    # the global gate never reads the podcast subfolder: usable stats unchanged
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")
    stats_files = {s for s in knowledge.load_corpus()}
    assert all("ep7" not in f for f in stats_files)


def test_cards_run_triggers_learnings(monkeypatch, tmp_path, capsys):
    from agent import podcast_cards
    _store_ep7(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_NANO_ENABLED", "true")
    podcast_cards.cards_cli(7, 2)
    assert os.path.exists(podcast_learn._learnings_path(7))
    assert "learning(s) written" in capsys.readouterr().out


# ---- flag off = zero behavior change ---------------------------------------------------
def test_flag_off_zero_behavior(monkeypatch, tmp_path, capsys):
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", str(kdir))
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    assert podcast_learn.write_learnings(7) is None
    podcast_learn.learn_cli(7)
    assert "OFF" in capsys.readouterr().out
    assert os.listdir(kdir) == []                           # nothing written
