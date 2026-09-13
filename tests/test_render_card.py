"""
The render-card CLI: one Astra card on demand through the real engine chain.

Every test is OFFLINE. The engine is never reached: the block-on-missing-input
guards return before any network call, and the one render test injects a fake
generate_image through the module seam.

The contract under test:
  - BLOCKS on a missing headline or missing facts (no fabrication)
  - BLOCKS on a banned headline word and an unknown style token
  - publishes nothing and queues nothing, ever
  - --brief-only renders nothing and costs nothing
  - a render writes the PNG plus the audit sidecar
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import render_card  # noqa: E402

HEAD = "Paid ads are not magic. They are math."
FACT = "The Three Levers of Growth: churn, sales, leads"


def test_blocks_without_a_headline(capsys):
    assert render_card.run(["--fact", FACT]) == 2
    assert "BLOCKED" in capsys.readouterr().out


def test_blocks_without_any_approved_fact(capsys):
    """No fabrication: a card with nothing approved behind it never renders."""
    assert render_card.run(["--headline", HEAD]) == 2
    out = capsys.readouterr().out
    assert "BLOCKED" in out and "--fact" in out


def test_blocks_a_banned_headline_word(capsys):
    rc = render_card.run(["--headline", "Pick the right vendor", "--fact", FACT])
    assert rc == 2
    assert "vendor" in capsys.readouterr().out


def test_rejects_an_unknown_style_token(capsys):
    rc = render_card.run(["--headline", HEAD, "--fact", FACT,
                          "--canvas", "chartreuse"])
    assert rc == 2
    assert "unknown canvas" in capsys.readouterr().out


def test_unknown_argument_is_loud():
    with pytest.raises(SystemExit):
        render_card.run(["--headline", HEAD, "--fact", FACT, "--nope"])


def test_brief_only_renders_nothing(capsys, monkeypatch):
    """--brief-only must never reach the engine, so it can never spend."""
    from agent import image_engine

    def _boom(*a, **k):  # noqa: ANN001
        raise AssertionError("brief-only reached the image engine")
    monkeypatch.setattr(image_engine, "generate_image", _boom)

    assert render_card.run(["--headline", HEAD, "--fact", FACT,
                            "--brief-only"]) == 0
    out = capsys.readouterr().out
    assert "grade gate : PASS" in out
    assert "HOOK" in out          # the brief itself was printed


def test_pinned_style_shows_up_in_the_brief(capsys):
    render_card.run(["--headline", HEAD, "--fact", FACT, "--canvas", "ink",
                     "--composition", "type_poster", "--brief-only"])
    out = capsys.readouterr().out
    assert "ink/type_poster/" in out
    assert "CANVAS MODE INK" in out
    assert "COMPOSITION TYPE POSTER" in out


def test_locked_mode_renders_the_old_brief(capsys, monkeypatch):
    monkeypatch.delenv("AGENT_ASTRA_STYLE_FREEDOM", raising=False)
    render_card.run(["--headline", HEAD, "--fact", FACT, "--locked",
                     "--brief-only"])
    out = capsys.readouterr().out
    assert "style      : locked" in out
    assert "freedom    : OFF" in out
    assert "CANVAS MODE" not in out
    assert "THE canvas" in out      # the original cream-locked palette


def test_a_render_writes_the_png_and_the_audit_sidecar(tmp_path, monkeypatch, capsys):
    from agent import image_engine

    class _Fake:
        image_bytes = b"\x89PNG\r\n\x1a\nFAKE"
        image_url = ""
        model = "gpt-image-2.5-sunburst"
        engine = "astra"
        cost_estimate = 0.04
        revised_prompt = "Astra's own art direction for this card"
        latency_ms = 1234
        prompt_used = ""

    seen = {}

    def _fake_generate(prompt, opts=None, **kw):  # noqa: ANN001
        seen["prompt"] = prompt
        seen["opts"] = opts
        return _Fake()
    monkeypatch.setattr(image_engine, "generate_image", _fake_generate)

    rc = render_card.run(["--headline", HEAD, "--fact", FACT,
                          "--cta", "Save this for later.",
                          "--out", str(tmp_path)])
    assert rc == 0
    pngs = list(tmp_path.glob("*.png"))
    txts = list(tmp_path.glob("*.txt"))
    assert len(pngs) == 1 and len(txts) == 1

    sidecar = txts[0].read_text(encoding="utf-8")
    assert "ASTRA REVISED PROMPT" in sidecar     # the brief-reading layer is visible
    assert "Astra's own art direction" in sidecar
    assert "BRIEF AS SENT" in sidecar
    assert HEAD in sidecar

    # the card really is routed as a text-overlay infographic (pins Sunburst)
    assert seen["opts"]["has_text_overlay"] is True
    assert seen["opts"]["kind"] == "infographic"


def test_a_dead_chain_is_reported_not_swallowed(tmp_path, monkeypatch, capsys):
    """generate_image returns None only after needs_human was marked. The CLI
    must surface that as a non-zero exit, never a silent success."""
    from agent import image_engine
    monkeypatch.setattr(image_engine, "generate_image",
                        lambda *a, **k: None)
    rc = render_card.run(["--headline", HEAD, "--fact", FACT,
                          "--out", str(tmp_path)])
    assert rc == 1
    assert "needs_human" in capsys.readouterr().out
    assert not list(tmp_path.glob("*.png"))


# ---- --account (Blake, 2026-09-13: "only for LASSO right now") -----------

def test_account_flag_mirrors_production_scoping(capsys, monkeypatch):
    """--account lasso is freed, --account eng is locked, when the master flag
    is ON and the scope is still the LASSO-only default."""
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM", "true")
    monkeypatch.delenv("AGENT_ASTRA_STYLE_FREEDOM_ACCOUNTS", raising=False)

    render_card.run(["--headline", HEAD, "--fact", FACT, "--account", "lasso",
                     "--brief-only"])
    lasso_out = capsys.readouterr().out
    assert "style      : locked" not in lasso_out
    assert "freedom    : ON" in lasso_out

    render_card.run(["--headline", HEAD, "--fact", FACT, "--account", "eng",
                     "--brief-only"])
    eng_out = capsys.readouterr().out
    assert "style      : locked" in eng_out
    assert "freedom    : OFF" in eng_out
    assert "account    : 'eng'" in eng_out


def test_locked_flag_wins_over_account(capsys, monkeypatch):
    """--locked is the hard override; it beats --account even for lasso."""
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM", "true")
    render_card.run(["--headline", HEAD, "--fact", FACT, "--account", "lasso",
                     "--locked", "--brief-only"])
    out = capsys.readouterr().out
    assert "style      : locked" in out
    assert "freedom    : OFF" in out


def test_no_account_flag_is_the_unscoped_manual_preview(capsys, monkeypatch):
    """Omitting --account keeps the pre-scoping preview behavior: freedom ON
    whenever the master flag is ON, regardless of any account scope."""
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM", "true")
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM_ACCOUNTS", "lasso")
    render_card.run(["--headline", HEAD, "--fact", FACT, "--brief-only"])
    out = capsys.readouterr().out
    assert "style      : locked" not in out
    assert "freedom    : ON" in out
    assert "account    :" not in out
