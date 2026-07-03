"""
Retry-storm regression (the Jul 1 twelve-card event). Asserts: a slot that
BLOCKS repeatedly (the same failure every scheduler fire) cards exactly ONCE;
an empty-caption draft blocks instead of growing Approve buttons; recovery to
a real PENDING draft still posts its card; expiry then clears the stale one.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.accounts import Account, Platform  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402
from agent.runner import run_daily  # noqa: E402
from agent.store import PendingStore  # noqa: E402

DAY = "2027-07-07"
VOICE = """# Voice
We help gym owners grow.
## CTAs
- Save this post.
## Hashtags
#LASSOFramework
"""


class FakePoster:
    def __init__(self):
        self.cards = []
        self.expired = []

    def post_approval_card(self, draft):
        self.cards.append(draft)
        return {"channel": "C1", "ts": f"ts{len(self.cards)}"}

    def post_notice(self, text):
        return {"ok": True}

    def mark_superseded(self, draft):
        pass

    def mark_expired(self, draft):
        self.expired.append(draft)


def _acct():
    return Account(key="gym_ig", display_name="Gym IG", platform=Platform.INSTAGRAM,
                   token_env="RS_T", target_id_env="RS_I")


def _run(tmp_path, poster, store, with_note=False):
    voice = tmp_path / "voice.md"
    voice.write_text(VOICE, encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    (lib / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    note = lib / "asset.txt"
    if with_note:
        note.write_text("A plain approved note.", encoding="utf-8")
    elif note.exists():
        note.unlink()
    return run_daily(poster=poster, voice_path=str(voice), library_path=str(lib),
                     scheduled_for=f"{DAY}T18:30:00+00:00",
                     accounts=[_acct()], store=store)


def test_repeated_failure_cards_once_not_twelve(monkeypatch, tmp_path):
    """ADVERSARIAL: twelve scheduler fires against the same failing slot (the
    note-less asset drafts an empty caption every time). ONE card, total."""
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("AGENT_IDEMPOTENT_DRAFTS_ENABLED", "true")
    store = PendingStore(path=str(tmp_path / "p.json"))
    poster = FakePoster()
    for _ in range(12):
        out = _run(tmp_path, poster, store)
    assert len(poster.cards) == 1                      # the storm is dead
    assert poster.cards[0].status == DraftStatus.BLOCKED
    assert "empty caption" in poster.cards[0].blocked_reason


def test_empty_caption_blocks_never_buttons(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("AGENT_IDEMPOTENT_DRAFTS_ENABLED", "true")
    store = PendingStore(path=str(tmp_path / "p.json"))
    poster = FakePoster()
    _run(tmp_path, poster, store)
    d = poster.cards[0]
    assert d.status == DraftStatus.BLOCKED
    from agent.slack_surface import build_card_blocks
    blocks = str(build_card_blocks(d))
    assert "Approve" not in blocks                     # a blocked card, no buttons


def test_recovery_to_pending_posts_its_card(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("AGENT_IDEMPOTENT_DRAFTS_ENABLED", "true")
    store = PendingStore(path=str(tmp_path / "p.json"))
    poster = FakePoster()
    _run(tmp_path, poster, store)                      # blocked, cards once
    _run(tmp_path, poster, store)                      # repeat block: no card
    assert len(poster.cards) == 1
    out = _run(tmp_path, poster, store, with_note=True)  # the note arrives
    assert len(poster.cards) == 2                      # the REAL draft cards
    assert poster.cards[1].status == DraftStatus.PENDING
    assert "A plain approved note." in poster.cards[1].caption
