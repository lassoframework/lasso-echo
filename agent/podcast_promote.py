"""
Standing claim promotion, PROPOSE ONLY (Part F of the podcast pipeline).

Rides AGENT_PODCAST_ENABLED, no new flag (OFF = zero behavior change: nothing
proposes, nothing routes, no file can move). Armed, an extracted learning
(Part E) that carries a QUANTITATIVE CLAIM or a NAMED FRAMEWORK becomes a
PROMOTION PROPOSAL carded to the approval channel as PROPOSED STANDING CLAIM,
showing the verbatim quote, the podcast_ep<N> citation, and the EXACT line it
would add to the standing claims source (02_verified_stats.md).

THE HARD RULES, stated plainly:
  - NOTHING is written to lasso_now.md, 02_verified_stats.md, or any standing
    knowledge file without the approver's explicit tap. The ONLY write path is
    handle_promotion_action("approve", ...) behind the same approver id gate
    the post flow uses (approvals.py itself is untouched).
  - On approval the line lands WITH its podcast citation attached, under its
    own USE section, so the fabrication gate reads it like any receipt.
  - THE BOOK STAYS ON TOP of the citation hierarchy: a proposal that conflicts
    with a book claim (book_campaign.conflict_warnings) is flagged CONFLICT
    with the conflict named, blocked from promotion, audited, and never carded
    as approvable. The conflict re-checks AT TAP TIME too, belt and suspenders.
"""

import os
import re
from datetime import datetime, timezone

from . import config, db, podcast_transcripts
from .approvals import ActionResult, _is_approver
from .drafter import Draft, DraftStatus, _make_id

CLAIMS_FILE = "02_verified_stats.md"
# matches the file's existing section conventions (see the B2B swipe section)
SECTION = "## USE — Podcast promotions (approved by tap, episode cited)"

_QUANT_RE = re.compile(r"%|\bpercent\b|\$\s?\d|\b\d")
_FRAMEWORK_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z]+\s+){1,3}(?:Framework|Method|Effect|System)\b"
    r"|\bLASSO acronym\b")


def claims_path():
    return os.path.join(config.KNOWLEDGE_DIR, CLAIMS_FILE)


def promotion_reason(text):
    """Why a learning is promotable: 'quantitative claim', 'named framework',
    or '' (not promotable; it stays a learning and nothing cards)."""
    if _QUANT_RE.search(text or ""):
        return "quantitative claim"
    if _FRAMEWORK_RE.search(text or ""):
        return "named framework"
    return ""


def proposed_line(episode, quote):
    """The EXACT line an approval would add: a USE receipt with the podcast
    citation attached, in the claims file's own format."""
    return (f'- USE: "{quote}" (citation {podcast_transcripts.citation_id(episode)}, '
            "promoted from the episode transcript by approval tap)")


def _book_conflicts(text):
    from . import book_campaign
    return book_campaign.conflict_warnings(text)


# ---- proposing (never writing) ---------------------------------------------------------
def propose(episode, learning, poster=None, store=None, day_key=""):
    """
    Card ONE promotion proposal, or block it on conflict. Returns the PENDING
    Draft (draft_type claim_promotion, held for the tap), a conflict dict, or
    None (flag off / not promotable). NEVER writes any knowledge file.
    """
    if not config.podcast_enabled():
        return None
    episode = int(episode)
    reason = (promotion_reason(learning.get("quote", ""))
              or promotion_reason(learning.get("takeaway", "")))
    if not reason:
        return None
    quote = learning["quote"]
    if not podcast_transcripts.contains_verbatim(episode, quote):
        raise ValueError("promotion refused: the quote is not verbatim in the "
                         f"{podcast_transcripts.citation_id(episode)} transcript")
    cite = podcast_transcripts.citation_id(episode)
    line = proposed_line(episode, quote)
    conflicts = _book_conflicts(quote)
    if conflicts:
        db.audit("claim_promotion", cite, f"CONFLICT, blocked: {conflicts[0][:300]}")
        print(f"[podcast] promotion blocked, CONFLICT with the book: {conflicts[0]}")
        if poster is not None:
            try:
                poster.post_notice(
                    f"PROPOSED STANDING CLAIM blocked as CONFLICT ({cite}): "
                    f"{conflicts[0]} The book stays the top of the citation "
                    "hierarchy; nothing was carded.")
            except Exception as e:
                print(f"[podcast] conflict notice failed: {type(e).__name__}: {e}")
        return {"status": "conflict", "conflicts": conflicts}
    day = day_key or datetime.now(timezone.utc).date().isoformat()
    draft = Draft(
        draft_id=_make_id("standing_claims", f"promo_ep{episode}", day),
        account_key="standing_claims", platform="internal",
        caption=(f"PROPOSED STANDING CLAIM ({cite}, {reason})\n\n"
                 f"Quote: \"{quote}\"\n\n"
                 f"Approve adds EXACTLY this line to {CLAIMS_FILE}:\n{line}"),
        hashtags=[], creative_path="", creative_public_url="",
        scheduled_for="", status=DraftStatus.PENDING,
        source_fragments=[f"cite:{cite}", quote],
        day_key=day, draft_type="claim_promotion",
    )
    if store is not None:
        store.put(draft)
    if poster is not None:
        try:
            poster.post_approval_card(draft)
        except Exception as e:
            print(f"[podcast] proposal card post failed (the proposal is "
                  f"stored): {type(e).__name__}: {e}")
    db.audit("claim_promotion", cite,
             f"proposal carded ({reason}), held for the tap: {quote[:120]}")
    return draft


def propose_from_learnings(episode, learnings, poster=None, store=None):
    """Part E's ride along hook: every promotable learning cards one proposal;
    duplicates (same quote already proposed or already promoted) are skipped."""
    out = []
    seen = set()
    already = ""
    try:
        with open(claims_path(), encoding="utf-8") as fh:
            already = fh.read()
    except OSError:
        pass
    for learning in learnings or []:
        quote = learning.get("quote", "")
        if quote in seen or (quote and quote in already):
            continue
        seen.add(quote)
        res = propose(episode, learning, poster=poster, store=store)
        if res is not None:
            out.append(res)
    return out


# ---- the tap: the ONLY write path -------------------------------------------------------
def _line_from(draft):
    lines = [l for l in (draft.caption or "").splitlines()
             if l.startswith("- USE:")]
    return lines[-1] if lines else ""


def _append_claim(line):
    path = claims_path()
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if line in text:
        return  # idempotent: a double tap never duplicates the claim
    lines = text.splitlines()
    if SECTION in lines:
        lines.insert(lines.index(SECTION) + 1, line)
        text = "\n".join(lines) + "\n"
    else:
        text = text.rstrip("\n") + f"\n\n{SECTION}\n{line}\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def handle_promotion_action(action, draft, actor_slack_id):
    """
    The promotion tap, same approver gate as every post (approvals._is_approver,
    reused not modified). Approve = the ONE write; skip = dropped, no write;
    anything else is refused. A conflict discovered between carding and tapping
    still blocks: the book wins even at the last second.
    """
    if not _is_approver(actor_slack_id):
        return ActionResult(ok=False, action=action,
                            draft_id=getattr(draft, "draft_id", ""),
                            detail=f"Denied: {actor_slack_id} is not the approver.")
    action = (action or "").lower()
    if action == "skip":
        draft.status = DraftStatus.SKIPPED
        db.audit("claim_promotion", draft.draft_id, "proposal skipped; no write")
        return ActionResult(ok=True, action="skip", draft_id=draft.draft_id,
                            detail="Dropped. Nothing was written.")
    if action == "approve":
        line = _line_from(draft)
        if not line:
            return ActionResult(ok=False, action="approve", draft_id=draft.draft_id,
                                detail="No proposed line on this card; nothing written.")
        conflicts = _book_conflicts(line)
        if conflicts:
            db.audit("claim_promotion", draft.draft_id,
                     f"tap time CONFLICT, blocked: {conflicts[0][:300]}")
            return ActionResult(ok=False, action="approve", draft_id=draft.draft_id,
                                detail=f"CONFLICT with the book, not promoted: "
                                       f"{conflicts[0]}")
        _append_claim(line)
        draft.status = DraftStatus.APPROVED
        db.audit("claim_promotion", draft.draft_id,
                 f"standing claim promoted with citation: {line[:200]}")
        return ActionResult(ok=True, action="approve", draft_id=draft.draft_id,
                            detail=f"Standing claim added to {CLAIMS_FILE} with "
                                   "its podcast citation.")
    return ActionResult(ok=False, action=action, draft_id=draft.draft_id,
                        detail=f"Unknown action '{action}'.")
