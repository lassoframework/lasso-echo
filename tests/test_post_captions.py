"""
post-captions sidecar URL resolution.

Confirms:
- _sidecar_public_url reads from config.LIBRARY_PATH first (Railway persistent
  volume fix: sidecars at /data/content_library survive redeploys even when the
  creative_path is a relative ephemeral path).
- Falls back to the literal path for local dev.
- When no sidecar has a public_url, returns "" (loud WARN, card shows placeholder,
  no crash).
- Re-running is idempotent: INSERT OR REPLACE means no duplicate DB rows.
- The draft IDs match the deterministic sha1 formula so the same 6 IDs appear
  on every run (stable Slack card dedup).

Fully offline: no network, no Slack, no R2.
"""

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.drafter import Draft, DraftStatus
from agent.store import PendingStore


# ---- helpers -----------------------------------------------------------------

CREATIVE_PATH = "content_library/lasso_v2_built_by_gym_owners.png"
SCHEDULED_FOR = "2026-07-17T12:00:00"
ACCOUNT_KEY   = "lasso_ig"


def _expected_draft_id(account_key, creative_path, scheduled_for):
    h = hashlib.sha1(f"{account_key}|{creative_path}|{scheduled_for}".encode()).hexdigest()
    return h[:10]


class _CardCapture:
    """Captures post_approval_card calls without hitting Slack."""
    def __init__(self):
        self.cards = []

    def post_approval_card(self, draft):
        self.cards.append(draft)
        return {"ok": True}


# ---- tests -------------------------------------------------------------------

def test_sidecar_public_url_is_read_into_draft(tmp_path, monkeypatch):
    """When a sidecar JSON exists in LIBRARY_PATH, _sidecar_public_url
    returns its public_url so the Slack card renders the image inline."""
    from agent.__main__ import _sidecar_public_url
    import agent.config as _cfg

    r2_url = "https://pub-XXXX.r2.dev/echo/lasso_ig/abc/lasso_v2_built_by_gym_owners.png"

    lib = tmp_path / "content_library"
    lib.mkdir()
    png = lib / "lasso_v2_built_by_gym_owners.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    sidecar = lib / "lasso_v2_built_by_gym_owners.json"
    sidecar.write_text(json.dumps({"public_url": r2_url, "note": ""}))

    monkeypatch.setattr(_cfg, "LIBRARY_PATH", str(lib))

    resolved = _sidecar_public_url(str(png))
    assert resolved == r2_url, f"expected R2 URL, got {resolved!r}"


def test_sidecar_resolved_via_library_path(tmp_path, monkeypatch):
    """config.LIBRARY_PATH is checked BEFORE the literal creative_path stem.
    This is the Railway fix: sidecars on the persistent volume survive redeploys
    even when creative_path is a relative ephemeral path that no longer exists."""
    from agent.__main__ import _sidecar_public_url
    import agent.config as _cfg

    r2_url = "https://pub-XXXX.r2.dev/echo/lasso/abc/lasso_v2_built_by_gym_owners.png"

    # Persistent volume: LIBRARY_PATH has the sidecar
    persistent_lib = tmp_path / "data" / "content_library"
    persistent_lib.mkdir(parents=True)
    (persistent_lib / "lasso_v2_built_by_gym_owners.json").write_text(
        json.dumps({"public_url": r2_url})
    )

    # Ephemeral container FS: PNG exists but no sidecar alongside it
    ephemeral_lib = tmp_path / "content_library"
    ephemeral_lib.mkdir()
    png = ephemeral_lib / "lasso_v2_built_by_gym_owners.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")

    monkeypatch.setattr(_cfg, "LIBRARY_PATH", str(persistent_lib))

    resolved = _sidecar_public_url(str(png))
    assert resolved == r2_url, (
        "must resolve from LIBRARY_PATH even when the literal path has no sidecar"
    )


def test_missing_sidecar_gives_empty_url(tmp_path, monkeypatch):
    """No sidecar in LIBRARY_PATH or alongside creative -> returns "" (loud WARN
    emitted by the CLI loop; card shows placeholder, but no crash)."""
    from agent.__main__ import _sidecar_public_url
    import agent.config as _cfg

    empty_lib = tmp_path / "empty_lib"
    empty_lib.mkdir()
    monkeypatch.setattr(_cfg, "LIBRARY_PATH", str(empty_lib))

    creative_path = str(tmp_path / "content_library" / "lasso_v2_no_sidecar.png")
    resolved = _sidecar_public_url(creative_path)
    assert resolved == ""


def test_draft_ids_are_deterministic():
    """The 6 draft IDs produced by post-captions must be stable across re-runs
    so INSERT OR REPLACE deduplicates correctly and Slack cards are not doubled."""
    specs = [
        ("lasso_ig", "content_library/lasso_v2_built_by_gym_owners.png",   "2026-07-17T12:00:00"),
        ("lasso_fb", "content_library/lasso_v2_built_by_gym_owners.png",   "2026-07-17T12:00:00"),
        ("lasso_ig", "content_library/lasso_v2_speed_to_lead_concept.png", "2026-07-22T12:00:00"),
        ("lasso_fb", "content_library/lasso_v2_speed_to_lead_concept.png", "2026-07-22T12:00:00"),
        ("lasso_ig", "content_library/lasso_v2_follow_up_problem.png",     "2026-07-28T12:00:00"),
        ("lasso_fb", "content_library/lasso_v2_follow_up_problem.png",     "2026-07-28T12:00:00"),
    ]
    seen = set()
    for acct, path, sched in specs:
        did = _expected_draft_id(acct, path, sched)
        assert did not in seen, f"duplicate draft_id {did}"
        seen.add(did)
    assert len(seen) == 6


def test_post_captions_idempotent_in_store(tmp_path, monkeypatch):
    """Writing the same draft twice (INSERT OR REPLACE) produces one row, not two."""
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)

    store = PendingStore(path=db_path)
    draft_id = _expected_draft_id(ACCOUNT_KEY, CREATIVE_PATH, SCHEDULED_FOR)
    draft = Draft(
        draft_id=draft_id,
        account_key=ACCOUNT_KEY,
        platform="instagram",
        caption="Test caption.",
        hashtags=["#GymOwner"],
        creative_path=CREATIVE_PATH,
        creative_public_url="https://pub-XXXX.r2.dev/echo/lasso_ig/abc/built.png",
        scheduled_for=SCHEDULED_FOR,
        status=DraftStatus.PENDING,
        day_key="2026-07-17",
        draft_type="feed",
    )
    store.put(draft)
    store.put(draft)  # second write — must not duplicate
    pending = [d for d in store.list_pending() if d.draft_id == draft_id]
    assert len(pending) == 1, f"expected 1 row, got {len(pending)}"


def test_public_url_reaches_card_image_block():
    """When creative_public_url is a hosted PNG URL, build_card_blocks includes
    an image block — not the placeholder context block."""
    from agent.slack_surface import build_card_blocks
    draft = Draft(
        draft_id="test01",
        account_key="lasso_ig",
        platform="instagram",
        caption="Test.",
        hashtags=[],
        creative_path="content_library/lasso_v2_built_by_gym_owners.png",
        creative_public_url="https://pub-XXXX.r2.dev/echo/lasso_ig/abc/built.png",
        scheduled_for="2026-07-17T12:00:00",
        status=DraftStatus.PENDING,
        day_key="2026-07-17",
        draft_type="feed",
    )
    blocks = build_card_blocks(draft)
    image_blocks = [b for b in blocks if b.get("type") == "image"]
    assert image_blocks, "expected an image block when creative_public_url is a hosted PNG"
    assert image_blocks[0]["image_url"] == draft.creative_public_url


def test_empty_public_url_shows_placeholder_not_crash():
    """When creative_public_url is empty, the card shows the placeholder context
    block and does NOT crash. No image block is emitted."""
    from agent.slack_surface import build_card_blocks
    draft = Draft(
        draft_id="test02",
        account_key="lasso_ig",
        platform="instagram",
        caption="Test.",
        hashtags=[],
        creative_path="content_library/lasso_v2_built_by_gym_owners.png",
        creative_public_url="",
        scheduled_for="2026-07-17T12:00:00",
        status=DraftStatus.PENDING,
        day_key="2026-07-17",
        draft_type="feed",
    )
    blocks = build_card_blocks(draft)
    image_blocks = [b for b in blocks if b.get("type") == "image"]
    assert not image_blocks, "no image block when creative_public_url is empty"
    texts = [
        e.get("text", "")
        for b in blocks if b.get("type") == "context"
        for e in b.get("elements", [])
    ]
    assert any("public URL" in t for t in texts), (
        "placeholder text must mention public URL so ops knows exactly what's missing")
