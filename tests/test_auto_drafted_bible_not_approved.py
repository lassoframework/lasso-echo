"""
AN AUTO-DRAFTED BIBLE IS NOT AN APPROVED SOURCE (finding 2026-09-06).

The anti-fabrication gate drafter._output_claims_cleared clears a caption's
figures against "an approved input": the client note, or the voice doc. That was
safe only while every voice doc was human-owned. website_intake auto-drafts a
brand bible off a gym's public website into the SAME file slot, so a scraped,
unapproved figure ("847 members since 2015") landed in voice.raw and then cleared
the very gate that exists to block unapproved figures. It did not merely bypass
approval, it WIDENED the fabrication rail: any caption reusing that figure passed.

Two properties are asserted here, both by behavior:
  1. an auto-drafted bible loads as auto_drafted=True and a human one does not;
  2. _output_claims_cleared cannot be satisfied by a figure that only ever came
     from a scrape, while the same figure in a HUMAN bible still clears.

Nothing here is faked: the real _bible_text renders the doc, the real load_voice
reads it, and the real gate judges it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import drafter, voice as vmod, website_intake as wi  # noqa: E402

_DOM = "gymx.com"
_HOME = "https://gymx.com/"

# A scraped bundle in the shape client_sources / _bible_text speak, carrying a
# figure nobody approved.
_SCRAPED = {
    "about": [("Gym X has served 847 members in Carmel since 2015 and counting.",
               _HOME)],
    "service": [("Gym X runs a six week starter program for every new member.",
                 _HOME)],
}


def _write(tmp_path, body):
    path = tmp_path / "lasso_voice.md"
    path.write_text(body, encoding="utf-8")
    return str(path)


# ---- 1. the doc says what it is ------------------------------------------------

def test_an_auto_drafted_bible_loads_as_auto_drafted(tmp_path):
    path = _write(tmp_path, wi._bible_text("Gym X", _DOM, _SCRAPED))
    doc = vmod.load_voice(path)
    assert doc is not None
    assert doc.auto_drafted is True


def test_a_human_bible_loads_as_approved(tmp_path):
    path = _write(tmp_path, "# Gym X Brand Bible\nWe have 847 members since 2015.")
    doc = vmod.load_voice(path)
    assert doc is not None
    assert doc.auto_drafted is False


# ---- 2. the gate ----------------------------------------------------------------

def test_a_scraped_figure_cannot_clear_the_fabrication_gate(tmp_path):
    """THE FINDING. The figure is right there in the auto-drafted bible's text,
    and the gate must still refuse it."""
    path = _write(tmp_path, wi._bible_text("Gym X", _DOM, _SCRAPED))
    doc = vmod.load_voice(path)
    assert "847" in doc.raw          # the figure really is in the prompt material
    caption = "Join the 847 members already training at Gym X."
    assert drafter._output_claims_cleared(caption, doc, "") is False


def test_the_same_figure_in_a_human_bible_still_clears(tmp_path):
    """The control, so the fix is a rule about PROVENANCE and not a blanket ban on
    numbers: identical text, no auto-drafted stamp, and the caption passes."""
    body = wi._bible_text("Gym X", _DOM, _SCRAPED).replace(
        vmod.AUTO_DRAFTED_MARKER, "")
    doc = vmod.load_voice(_write(tmp_path, body))
    assert doc.auto_drafted is False
    caption = "Join the 847 members already training at Gym X."
    assert drafter._output_claims_cleared(caption, doc, "") is True


def test_an_approved_client_note_still_clears_against_an_auto_drafted_bible(tmp_path):
    """Excluding the bible must not break the note: a figure a HUMAN put in the
    client note clears even when the gym's bible is auto-drafted."""
    doc = vmod.load_voice(_write(tmp_path, wi._bible_text("Gym X", _DOM, _SCRAPED)))
    note = "Our six week starter program is $199 for new members."
    assert drafter._output_claims_cleared("Start for $199 this month.", doc,
                                          note) is True
    # and a figure in NEITHER is still refused
    assert drafter._output_claims_cleared("Start for $299 this month.", doc,
                                          note) is False


def test_a_figure_free_caption_is_unaffected(tmp_path):
    doc = vmod.load_voice(_write(tmp_path, wi._bible_text("Gym X", _DOM, _SCRAPED)))
    assert drafter._output_claims_cleared("Come train with us this week.", doc,
                                          "") is True


# ---- 3. citations survive the render -------------------------------------------

def test_the_auto_drafted_bible_keeps_every_fact_citation():
    """_bible_text used to render `[f for f, _ in bundle...]`, dropping the source
    URL the moment a fact left client_sources. A reader could not tell an
    unapproved scraped line from an approved one."""
    body = wi._bible_text("Gym X", _DOM, _SCRAPED)
    for facts in _SCRAPED.values():
        for fact, url in facts:
            assert fact in body
            assert f"(source: {url})" in body


def test_a_bundle_without_citations_still_renders_and_names_the_domain():
    """Robustness: an old-shape bundle (bare strings, no citation) must not blow
    up the writer, and still names where the material came from."""
    body = wi._bible_text("Gym X", _DOM, {"about": [("Gym X is in Carmel.", "")]})
    assert "Gym X is in Carmel." in body
    assert f"(source: {_DOM})" in body
