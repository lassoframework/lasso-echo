"""
Document intake (the seed of Stage 2 client intake).

A client sends a PDF (message threads plus an email). Echo extracts the text, splits
it into distinct candidate post ideas with SIMPLE DETERMINISTIC boundaries (no LLM for
the split), and turns each idea into a draft post, ALL held for human approval.

NO FABRICATION. Each draft's caption is built ONLY from that idea's extracted CLIENT
text plus the approved brand voice (a CTA and hashtags from the voice doc). Echo never
invents claims, prices, or stats. If an idea is too thin to caption honestly, that one
comes back as a BLOCKED draft with a clear reason, never a filled in guess. The PDF is
raw material, never treated as approved fact.

Dormant by default behind AGENT_DOC_INTAKE_ENABLED. The infographic itself reuses
creative_studio + media_host exactly as daily_studio does: same flags, same visible
fallback when generation or hosting is unavailable, and the same source_fragments audit
trail (every fragment is verbatim client text). Nothing here publishes.
"""

import re

from . import config, creative_studio, media_host, ops_alerts
from .accounts import active_accounts
from .drafter import Draft, DraftStatus, TemplateGenerator, _make_id
from .library import Creative
from .voice import load_voice

# A line that is only divider characters separates one idea from the next.
_DIVIDER = re.compile(r"^\s*[-=_*]{3,}\s*$")
# Below this many characters an idea is too thin to caption honestly.
MIN_IDEA_CHARS = 20


def _extract_text(pdf_path):
    """Extract all text from a PDF. pypdf is imported LAZILY (like google-genai) so a
    cold start or a flag-off run never needs the dependency loaded."""
    from pypdf import PdfReader

    reader = PdfReader(pdf_path)
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def split_ideas(text):
    """
    Deterministic split into candidate ideas: on divider lines when present, else on
    blank line separated blocks. Each message thread, and the email, becomes one idea.
    """
    lines = (text or "").splitlines()
    if any(_DIVIDER.match(ln) for ln in lines):
        chunks, current = [], []
        for ln in lines:
            if _DIVIDER.match(ln):
                chunks.append("\n".join(current))
                current = []
            else:
                current.append(ln)
        chunks.append("\n".join(current))
    else:
        chunks = re.split(r"\n\s*\n", text or "")
    return [c.strip() for c in chunks if c.strip()]


def _idea_lines(idea):
    return [ln.strip() for ln in idea.splitlines() if ln.strip()]


def process_document(pdf_path=None, max_posts=7, *, account=None, voice=None,
                     voice_path=None, nano_client=None, s3_client=None, text=None):
    """
    Turn a client PDF into up to `max_posts` draft posts (PENDING, or BLOCKED for a thin
    idea). Returns None when AGENT_DOC_INTAKE_ENABLED is OFF (dormant no-op).

    `text` lets a caller or test supply the raw text directly and skip PDF extraction.
    """
    if not config.doc_intake_enabled():
        return None

    account = account or (active_accounts() or [None])[0]
    if voice is None:
        voice = load_voice(voice_path or config.VOICE_DOC_PATH)

    raw = text if text is not None else _extract_text(pdf_path)
    ideas = split_ideas(raw)[: max(0, int(max_posts))]

    return [_draft_for_idea(i, idea, account, voice, nano_client, s3_client)
            for i, idea in enumerate(ideas)]


def _draft_for_idea(index, idea, account, voice, nano_client, s3_client):
    acct_key = getattr(account, "key", "client")
    platform = getattr(account, "platform", "instagram")
    draft_id = _make_id(acct_key, f"doc_intake_{index}", idea[:40])

    def _blocked(reason):
        return Draft(draft_id=draft_id, account_key=acct_key, platform=platform,
                     caption="", hashtags=[], creative_path="", creative_public_url="",
                     scheduled_for="", status=DraftStatus.BLOCKED,
                     blocked_reason="Doc intake: " + reason)

    lines = _idea_lines(idea)
    if not lines or len(idea.strip()) < MIN_IDEA_CHARS:
        return _blocked(f"idea {index + 1} is too thin to caption honestly; needs more client text")

    # CAPTION: the client's own words plus the approved brand voice (CTA + hashtags).
    # No fabrication. The idea text is the creative's client note, exactly as a coach
    # would drop a note on a library creative.
    creative = Creative(path=f"intake_idea_{index}", media_type="infographic",
                        client_note=idea.strip())
    if voice is not None:
        caption, hashtags, _frags = TemplateGenerator().build(voice, creative)
    else:
        caption, hashtags = idea.strip(), []

    # INFOGRAPHIC: headline is the idea's first line (verbatim); the rest are the body
    # facts. Reuse creative_studio + media_host exactly as daily_studio does, including
    # the same visible fallback when a flag is off or a step returns nothing.
    headline, facts = lines[0], lines[1:]
    creative_path, creative_public_url = "", ""
    if facts:
        art = creative_studio.generate(headline, facts, client=nano_client,
                                       account_key=acct_key)
        if not art:
            print(f"[doc intake] {acct_key}: idea {index + 1} image generation produced "
                  "nothing; shipping a text only draft for approval.")
            ops_alerts.alert(
                f"doc intake: studio returned nothing for {acct_key} idea {index + 1} "
                "(studio dark or Gemini unavailable); shipping text-only draft."
            )
        else:
            hosted = media_host.host_media(art["path"], acct_key, client=s3_client)
            if not hosted:
                print(f"[doc intake] {acct_key}: idea {index + 1} hosting failed; "
                      "shipping a text only draft for approval.")
            else:
                creative_path, creative_public_url = art["path"], hosted

    return Draft(
        draft_id=draft_id, account_key=acct_key, platform=platform,
        caption=caption, hashtags=list(hashtags),
        creative_path=creative_path, creative_public_url=creative_public_url,
        scheduled_for="", status=DraftStatus.PENDING,
        source_fragments=list(lines),  # audit: every fragment is verbatim client text
    )
