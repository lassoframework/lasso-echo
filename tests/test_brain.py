"""
Nightly brain tests. Offline, adversarial. Asserts: the note cites ONLY approved
sources (a LOCKED knowledge stat never appears even when it is the juiciest
line); one note per night max with a persisted mark; the thin-data question
appears when data is thin; fully inert while the flag is OFF.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import brain, config, db  # noqa: E402


class RecordingPoster:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


ADVERSARIAL_KNOWLEDGE = """# stats
## USE approved
- USE: "Trusted by many gym owners." (approved wording)
## LOCKED, do not post
- LOCKED: we grew clients 900 percent overnight (juicy, unverified, banned).
"""


def _fixture_sources(monkeypatch, tmp_path, with_doc=True):
    src = tmp_path / "lasso_now.md"
    src.write_text("""# LASSO Now
## Pillars
- Speed To Lead
## Pillar copy bank
### Pillar: Speed To Lead
Hook: Leads go cold in minutes.
Body: Answer fast.
## CTAs
- Save this post.
## Hashtags
#LASSOFramework
""" if with_doc else "", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "02_stats.md").write_text(ADVERSARIAL_KNOWLEDGE, encoding="utf-8")
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", str(kdir))


def _seed_scored_post():
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, media_id, "
            "mode, published_at, creative_key, archetype, set_name, likes) "
            "VALUES ('d','lasso_ig','instagram','c','M','published',"
            "'2026-07-05T10:00:00','lasso_p1_a.jpg','flow','brand',12)")
        conn.commit()


def test_note_cites_only_approved_sources(monkeypatch, tmp_path):
    _fixture_sources(monkeypatch, tmp_path)
    _seed_scored_post()
    note = brain.build_note(now=datetime(2026, 7, 6, tzinfo=timezone.utc))
    assert "Leads go cold in minutes." in note            # the approved hook, cited
    assert "lasso_now.md" in note                          # with its citation
    assert "900 percent" not in note                       # the LOCKED stat NEVER
    assert "Winning this window" in note
    assert "pillar p1" in note


def test_locked_never_appears_even_without_source_doc(monkeypatch, tmp_path):
    _fixture_sources(monkeypatch, tmp_path, with_doc=False)
    note = brain.build_note(now=datetime(2026, 7, 6, tzinfo=timezone.utc))
    assert "900 percent" not in note                       # adversarial: still banned
    assert "Trusted by many gym owners." in note           # the USE stat instead


def test_thin_data_question(monkeypatch, tmp_path):
    _fixture_sources(monkeypatch, tmp_path)
    note = brain.build_note(now=datetime(2026, 7, 6, tzinfo=timezone.utc))
    assert "Data is thin" in note and "Question for Blake" in note


def test_one_note_per_night_max(monkeypatch, tmp_path):
    _fixture_sources(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_BRAIN_PROPOSALS_ENABLED", "true")
    monkeypatch.setenv("AGENT_BRAIN_HOUR_UTC", "0")
    poster = RecordingPoster()
    at_hour = datetime(2026, 7, 7, 0, 10, tzinfo=timezone.utc)
    assert brain.maybe_send(poster, now=at_hour) is not None
    assert brain.maybe_send(poster, now=at_hour) is None   # same night: once
    assert len(poster.notices) == 1
    assert db.kv_get("brain_sent_date") == "2026-07-07"    # persisted mark
    next_night = datetime(2026, 7, 8, 0, 10, tzinfo=timezone.utc)
    assert brain.maybe_send(poster, now=next_night) is not None


def test_inert_when_off(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_BRAIN_PROPOSALS_ENABLED", raising=False)
    poster = RecordingPoster()
    at_hour = datetime(2026, 7, 7, 0, 10, tzinfo=timezone.utc)
    assert brain.maybe_send(poster, now=at_hour) is None
    assert poster.notices == []
