"""
variant_regen_brief.py — "generate an image from what I typed", a human-initiated,
per-request companion to variant_regen.py's "Try a v2 image (Astra)" button.

Blake (2026-09-11 correction to the no-media auto-fallback build): "build a
manual, on-demand option: a button ... that lets a human type in what they want,
and the system generates ONE infographic pulling from that gym's real brand
brain (their scraped/on-file brand voice, positioning, visual identity) — not
the auto-triggered zero-content case, a deliberate one-off request a person
makes... Do NOT build this as something that runs automatically... The manual
button is opt-in, per-request, human-initiated every time."

HOW THIS DIFFERS FROM variant_regen.py: that module derives its brief ENTIRELY
from the row's own pillar + caption (no new input). This module takes a human's
free-text ask as the rendered HEADLINE, and grounds the card's supporting facts
in the gym's own real, already-on-file material — approved client_sources first
(the same set every caption draws from), falling back to the gym's deep_brain
scrape output when it has no approved sources yet — so a request can never
invent a claim about the gym that isn't already grounded in its own material.
Nothing is fetched or scraped here; this module only READS what already landed.

Reuses creative_studio.generate — the SAME Astra-first, grade-gated pipeline
every normal build and variant_regen.py use. The caller (portal_social's HTTP
handler) inserts the result as a linked 'candidate' via
SupabaseCalendarStore.create_variant_candidate, same as variant_regen — a human
still picks between the two later via swap_variant; nothing here publishes.

NEVER RUNS ON ITS OWN: there is no scheduler, cron, or scan-loop call anywhere
in this module. It is invoked exactly once per human click, always carrying an
explicit `brief` a person typed that turn.

Flag: config.variant_pairing_enabled() (ECHO_VARIANT_PAIRING) — the same gate
variant_regen.py uses, since this ships the same generated-candidate row shape
through the same review surface.
"""

from . import config

REASON_DISABLED = "disabled"
REASON_NO_BRIEF = "no_brief"
REASON_GENERATE_FAILED = "generate_failed"
REASON_HOSTING = "hosting_unavailable"

_CLIENT_MESSAGES = {
    REASON_NO_BRIEF: "Type what you want this card to say before generating.",
    REASON_GENERATE_FAILED: "Astra could not produce a clean image from that "
                            "brief. Nothing changed; try rephrasing it.",
    REASON_HOSTING: "The new image was generated but could not be hosted. "
                    "Nothing changed; try again shortly.",
}

# How many of the gym's own grounded lines ride along as supporting facts —
# enough to ground the visual, not so many the card is cluttered.
_MAX_FACTS = 6


def enabled():
    return config.variant_pairing_enabled()


def client_message(reason):
    return _CLIENT_MESSAGES.get(reason, "Could not generate that card right now.")


def _brand_brain_facts(account_key):
    """The gym's own already-on-file grounded material: approved client_sources
    first (the set every real caption already draws from); when a gym has none
    yet, fall back to its deep_brain scrape's landed facts (also real, cited
    material, just not yet human-approved for captions) so a brand-new gym's
    button still has something real to ground the card in. [] when neither
    exists — the caller still renders from the brief alone in that case, never
    inventing a fact to fill the gap."""
    try:
        from . import client_sources
        approved = client_sources.approved_sources(account_key)
        if approved:
            return [s.text for s in approved][:_MAX_FACTS]
    except Exception:  # noqa: BLE001 - a lookup failure is just "nothing found"
        pass
    try:
        from . import client_sources
        pending = client_sources.pending_sources(account_key)
        if pending:
            return [s.text for s in pending][:_MAX_FACTS]
    except Exception:  # noqa: BLE001
        pass
    return []


def generate_variant_from_brief(row, account_key, brief, client=None,
                                generate_fn=None, host_fn=None):
    """Generate ONE new image for the logical post `row` represents, from a
    human-typed `brief`, WITHOUT touching `row` itself. Returns
    {"ok": True, "image_url": ..., "prompt": ...} or {"ok": False, "reason": REASON_*}.
    """
    if not enabled():
        return {"ok": False, "reason": REASON_DISABLED}
    brief = str(brief or "").strip()
    if not brief:
        return {"ok": False, "reason": REASON_NO_BRIEF}

    facts = _brand_brain_facts(account_key) or [brief]
    is_story = "story" in str(row.get("format") or "").lower()
    aspect = "9:16" if is_story else None
    pixels = "1080x1920" if is_story else None
    surface = "story" if is_story else "feed post"

    gen = generate_fn or _default_generate
    result = gen(brief, facts, client=client, aspect=aspect, pixels=pixels,
                surface=surface, account_key=account_key)
    if not result or not result.get("path"):
        return {"ok": False, "reason": REASON_GENERATE_FAILED}

    from . import media_host
    host = host_fn or media_host.host_media
    url = host(result["path"], account_key)
    if not url:
        return {"ok": False, "reason": REASON_HOSTING}

    return {"ok": True, "image_url": url, "prompt": result.get("prompt", ""),
           "model": result.get("model", ""), "route": result.get("route", "")}


def _default_generate(headline, facts, client=None, aspect=None, pixels=None,
                      surface=None, account_key=None):
    from . import creative_studio
    return creative_studio.generate(
        headline, facts, client=client, aspect=aspect, pixels=pixels,
        surface=surface, account_key=account_key)
