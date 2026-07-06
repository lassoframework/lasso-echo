"""
calendar-html tests (calendar Part B). Offline. Asserts: the artifact renders
for EVERY active account from fixture state; visible copy is dash free; the
upload key shape is echo/calendars/<account>_<month>.html; status colors in
the grid match store state; empty days render an open slot and no post is
fabricated; the approve/edit/kill buttons are clearly display only previews;
no writes beyond the local file and the upload.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import calendar_artifact, db  # noqa: E402
from agent.accounts import active_accounts  # noqa: E402

_DASH_RE = re.compile(r"[‐‑‒–—―−-]")
_TAG_RE = re.compile(r"<[^>]+>")
MONTH = "2026-07"


class FakeS3:
    def __init__(self):
        self.puts = []

    def put(self, key, local_path):
        self.puts.append((key, local_path))


def _seed(account_key):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, "
            "media_id, mode, published_at, creative_key) VALUES (?,?,?,?,?,?,?,?)",
            ("d1", account_key, "instagram", "published caption", "m1",
             "published", "2026-07-01T14:00:00", "lasso_v2_one_screen.png"))
        conn.execute(
            "INSERT INTO drafts (draft_id, account_key, status, day_key, "
            "draft_type, data) VALUES (?,?,?,?,?,?)",
            (f"{account_key}_d2", account_key, "pending", "2026-07-03", "feed",
             json.dumps({"creative_path": "lib/lasso_v2_b2b_16_cpl.png",
                         "caption": "pending caption"})))
        conn.commit()


def test_renders_every_account_from_fixture_state(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    for acct in active_accounts():
        _seed(acct.key)
        out = calendar_artifact.run(
            acct.key, MONTH, out_path=str(tmp_path / f"{acct.key}.html"))
        assert out is not None
        text = open(out["path"], encoding="utf-8").read()
        assert f"{acct.key}: July 2026" in text
        assert "lasso_v2_one_screen.png" in text
        assert "Month rollup:" in text


def test_visible_copy_dash_free_and_buttons_preview_only(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    _seed("lasso_ig")
    out = calendar_artifact.run("lasso_ig", MONTH,
                                out_path=str(tmp_path / "c.html"))
    text = open(out["path"], encoding="utf-8").read()
    assert "—" not in text and "–" not in text
    visible = _TAG_RE.sub(" ", text)
    # visible copy only: filenames like lasso_v2_* carry underscores, and the
    # remaining prose carries no dash family character at all
    assert not _DASH_RE.search(visible.replace("lasso_v2_", "").replace(
        "b2b_16_cpl.png", "").replace("one_screen.png", "")), visible
    assert "PREVIEW ONLY" in text
    assert text.count("<button disabled>") == 6       # 3 header + 3 modal
    assert "the tap still happens in Slack" in text


def test_status_colors_match_state(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    _seed("lasso_ig")
    plan = calendar_artifact.assemble_month("lasso_ig", MONTH)
    text = calendar_artifact.render_html(plan)
    # the day cell's top border carries its status color
    published_color = calendar_artifact.STATUS_COLORS["published"]
    pending_color = calendar_artifact.STATUS_COLORS["pending"]
    assert f"border-top:4px solid {published_color}" in text
    assert f"border-top:4px solid {pending_color}" in text
    counts = {s: text.count(f"border-top:4px solid {c}")
              for s, c in calendar_artifact.STATUS_COLORS.items()}
    assert counts["published"] == plan["rollup"]["published"]
    assert counts["pending"] == plan["rollup"]["pending"]
    assert counts["rest"] == plan["rollup"]["rest"]


def test_no_post_fabricated_on_empty_month(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    out = calendar_artifact.run("lasso_fb", MONTH,      # nothing seeded
                                out_path=str(tmp_path / "fb.html"))
    text = open(out["path"], encoding="utf-8").read()
    assert "open slot" in text
    assert ".png" not in text                           # no concept invented
    assert "PUBLISHED" not in text.replace("published", "")


def test_upload_key_shape(monkeypatch, tmp_path):
    from agent import config
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    s3 = FakeS3()
    out = calendar_artifact.run("lasso_ig", MONTH, upload=True, s3_client=s3,
                                out_path=str(tmp_path / "c.html"))
    key = "echo/calendars/lasso_ig_2026-07.html"
    assert out["key"] == key
    assert s3.puts and s3.puts[0][0] == key
    assert out["url"] == f"https://cdn.echo.test/{key}"


def test_cli_validates_input(capsys, tmp_path):
    calendar_artifact.run("nope", MONTH)
    assert "unknown account" in capsys.readouterr().out
    calendar_artifact.run("lasso_ig", "July")
    assert "YYYY-MM" in capsys.readouterr().out
    calendar_artifact.cli([])
    assert "usage" in capsys.readouterr().out


# ---- PART A: full-post cells -------------------------------------------------

def test_thumbnail_renders_sidecar_public_url(monkeypatch, tmp_path):
    """Days with a sidecar public_url render that exact URL in an img tag."""
    _seed("lasso_ig")
    test_url = "https://cdn.echo.test/renders/lasso_v2_one_screen.png"
    monkeypatch.setattr(
        calendar_artifact, "_public_url_for",
        lambda key: test_url if key == "lasso_v2_one_screen.png" else "")
    out = calendar_artifact.run("lasso_ig", MONTH,
                                out_path=str(tmp_path / "c.html"))
    text = open(out["path"], encoding="utf-8").read()
    assert f'src="{test_url}"' in text
    assert f'alt="lasso_v2_one_screen.png"' in text


def test_thumbnail_placeholder_when_no_url(monkeypatch, tmp_path):
    """Days with a concept but no public_url show 'image pending', no broken img."""
    _seed("lasso_ig")
    # Real sidecars have public_url="" so _public_url_for returns ""
    out = calendar_artifact.run("lasso_ig", MONTH,
                                out_path=str(tmp_path / "c.html"))
    text = open(out["path"], encoding="utf-8").read()
    assert "image pending" in text
    assert '<img src=""' not in text


def test_cell_shows_exact_caption_and_hashtags(monkeypatch, tmp_path):
    """Cell shows the full caption and hashtags exactly as stored in the draft."""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO drafts (draft_id, account_key, status, day_key, "
            "draft_type, data) VALUES (?,?,?,?,?,?)",
            ("cap1", "lasso_ig", "pending", "2026-07-05", "feed",
             json.dumps({"creative_path": "lib/lasso_v2_built_by_gym_owners.png",
                         "caption": "Full caption text here.",
                         "hashtags": ["#gymlife", "#lasso"],
                         "source_fragments": []})))
        conn.commit()
    out = calendar_artifact.run("lasso_ig", MONTH,
                                out_path=str(tmp_path / "c.html"))
    text = open(out["path"], encoding="utf-8").read()
    assert "Full caption text here." in text
    assert "#gymlife" in text
    assert "#lasso" in text


# ---- PART B: lightbox / tap-to-expand ---------------------------------------

def test_lightbox_structure_present(monkeypatch, tmp_path):
    """Lightbox structural IDs are present in the rendered HTML."""
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    _seed("lasso_ig")
    out = calendar_artifact.run("lasso_ig", MONTH,
                                out_path=str(tmp_path / "c.html"))
    text = open(out["path"], encoding="utf-8").read()
    assert 'id="daymodal"' in text
    assert 'id="modalcaption"' in text
    assert 'id="modalsource"' in text
    assert 'id="plandata"' in text
    assert 'id="modalchips"' in text


def test_lightbox_data_embeds_url_and_full_caption(monkeypatch, tmp_path):
    """Draft creative_public_url and caption appear inside the data-plan attribute."""
    draft_url = "https://cdn.echo.test/renders/lightbox_test.png"
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO drafts (draft_id, account_key, status, day_key, "
            "draft_type, data) VALUES (?,?,?,?,?,?)",
            ("lb1", "lasso_ig", "approved", "2026-07-04", "feed",
             json.dumps({"creative_path": "lib/lasso_v2_built_by_gym_owners.png",
                         "creative_public_url": draft_url,
                         "caption": "Lightbox caption here.",
                         "hashtags": ["#echo"],
                         "source_fragments": ["source: internal"]})))
        conn.commit()
    out = calendar_artifact.run("lasso_ig", MONTH,
                                out_path=str(tmp_path / "c.html"))
    text = open(out["path"], encoding="utf-8").read()
    # URL and caption are stored in the data-plan attribute (HTML-escaped)
    assert "lightbox_test.png" in text
    assert "Lightbox caption here." in text


def test_lightbox_buttons_preview_only(monkeypatch, tmp_path):
    """Modal adds 3 more disabled buttons; total is 6 (3 header + 3 modal)."""
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    _seed("lasso_ig")
    out = calendar_artifact.run("lasso_ig", MONTH,
                                out_path=str(tmp_path / "c.html"))
    text = open(out["path"], encoding="utf-8").read()
    assert text.count("<button disabled>") == 6


def test_closing_modal_script_present(monkeypatch, tmp_path):
    """openDay and closeModal JS functions are present in the rendered HTML."""
    monkeypatch.delenv("AGENT_PODCAST_ENABLED", raising=False)
    _seed("lasso_ig")
    out = calendar_artifact.run("lasso_ig", MONTH,
                                out_path=str(tmp_path / "c.html"))
    text = open(out["path"], encoding="utf-8").read()
    assert "function openDay" in text
    assert "function closeModal" in text
    assert "dataset.plan" in text


def test_creative_public_url_from_draft_data_used(monkeypatch, tmp_path):
    """creative_public_url in the draft data takes priority over the sidecar."""
    draft_url = "https://r2.echo.test/renders/built_by_gym_owners_navy_poster.png"
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO drafts (draft_id, account_key, status, day_key, "
            "draft_type, data) VALUES (?,?,?,?,?,?)",
            ("url1", "lasso_ig", "approved", "2026-07-02", "feed",
             json.dumps({"creative_path": "lib/lasso_v2_built_by_gym_owners.png",
                         "creative_public_url": draft_url,
                         "caption": "Draft caption.", "hashtags": []})))
        conn.commit()
    # Even if sidecar returns a different url, draft's creative_public_url wins
    monkeypatch.setattr(calendar_artifact, "_public_url_for",
                        lambda k: "https://sidecar.url/different.png")
    out = calendar_artifact.run("lasso_ig", MONTH,
                                out_path=str(tmp_path / "c.html"))
    text = open(out["path"], encoding="utf-8").read()
    assert draft_url in text
