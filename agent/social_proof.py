"""
Social proof cards from a VERIFIED source doc.

The source is brand_voice/social_proof.md (per-account convention:
brand_voice/social_proof.<account_key>.md wins when present, so a future client
account carries its own proof file beside its voice doc). Every entry needs an
explicit `Permission: yes` AND a `Verified: YYYY-MM-DD` date; anything missing
either is SKIPPED with one Slack notice line and never rendered. A missing or
empty file means the feature is silently absent; normal drafting is untouched.

Rotation: proof converts but repels when spammed, so at most ONE social proof
post per account per week enters the plan (structurally: it only fires on the
configured proof weekday, default Wednesday), rotating deterministically through
the approved entries by ISO week.

NO FABRICATION: the card and the caption carry only the entry's approved lines
(quote/stat, support, attribution) plus the approved brand voice CTA + hashtags.
Dormant behind AGENT_SOCIAL_PROOF_ENABLED (default OFF). Nothing here publishes.
"""

import os
import re
from dataclasses import dataclass
from datetime import date

from . import config, creative_studio, media_host, schedule
from .drafter import Draft, DraftStatus, TemplateGenerator, _make_id
from .library import Creative
from .voice import load_voice

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class ProofEntry:
    kind: str            # "quote" or "stat"
    main: str            # the quote text, or the stat line
    support: str = ""    # stat cards only: one short support line
    attribution: str = ""
    permission: str = ""
    verified: str = ""

    def approved(self):
        return self.permission.strip().lower() == "yes" and bool(_DATE_RE.match(self.verified.strip()))

    def skip_reason(self):
        if self.permission.strip().lower() != "yes":
            return "no permission on record"
        return "no verified date"

    def approved_lines(self):
        return [ln for ln in (self.main, self.support, self.attribution) if ln]


def source_path(account_key):
    """Per-account file beside the voice doc when present, else the shared default."""
    base = config.SOCIAL_PROOF_PATH
    root, ext = os.path.splitext(base)
    per_account = f"{root}.{account_key}{ext}"
    return per_account if os.path.isfile(per_account) else base


def load_entries(path):
    """
    Parse the source doc. Returns (approved, skipped) where skipped is a list of
    (entry, reason). A missing or empty file returns ([], []) - silently absent.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except (FileNotFoundError, IsADirectoryError, OSError):
        return [], []
    if not raw.strip():
        return [], []

    approved, skipped = [], []
    blocks = re.split(r"^##\s+", raw, flags=re.MULTILINE)[1:]  # each "## Entry" block
    for block in blocks:
        fields = {}
        for line in block.splitlines():
            m = re.match(r"^\s*(Quote|Stat|Support|Attribution|Permission|Verified)\s*:\s*(.*?)\s*$",
                         line, re.IGNORECASE)
            if m:
                fields[m.group(1).lower()] = m.group(2).strip()
        main = fields.get("quote") or fields.get("stat") or ""
        if not main:
            continue  # not an entry (a heading or commentary block)
        entry = ProofEntry(
            kind="stat" if fields.get("stat") else "quote",
            main=main,
            support=fields.get("support", ""),
            attribution=fields.get("attribution", ""),
            permission=fields.get("permission", ""),
            verified=fields.get("verified", ""),
        )
        if entry.approved():
            approved.append(entry)
        else:
            skipped.append((entry, entry.skip_reason()))
    return approved, skipped


def is_proof_day(day_key):
    """True only on the configured proof weekday: at most one proof post per week."""
    return schedule.weekday_abbr(day_key) == config.SOCIAL_PROOF_DAY


def pick_entry(approved, day_key):
    """Deterministic weekly rotation: the ISO week number indexes the approved list."""
    if not approved:
        return None
    week = date.fromisoformat(day_key).isocalendar()[1]
    return approved[week % len(approved)]


def build_social_proof_draft(account, day_key, *, voice=None, voice_path=None,
                             nano_client=None, s3_client=None, path=None, poster=None):
    """
    The week's social proof draft for this account, or None whenever the feature
    must stay out of the way (flag off, not the proof day, no file, no approved
    entries, generation/hosting unavailable). Never blocks normal drafting.
    """
    if not config.social_proof_enabled():
        return None
    if not is_proof_day(day_key):
        return None

    src = path or source_path(account.key)
    approved, skipped = load_entries(src)
    if skipped and poster is not None:
        for entry, reason in skipped:
            poster.post_notice(
                f"Social proof entry skipped ({reason}), never rendered: "
                f"\"{entry.main[:80]}\"")
    if not approved:
        return None  # silently absent; the normal draft path takes the day

    entry = pick_entry(approved, day_key)

    # Card image: the verified entry, feed 4:5 (the Story path re-renders 9:16
    # from source_fragments exactly as it does for daily studio cards).
    art = creative_studio.generate_social_proof(
        entry.kind, entry.main, entry.support, entry.attribution, client=nano_client)
    if not art:
        print(f"[social-proof] {account.key}: card generation unavailable; "
              "normal draft path takes the day.")
        return None
    hosted = media_host.host_media(art["path"], account.key, client=s3_client)
    if not hosted:
        print(f"[social-proof] {account.key}: hosting failed; "
              "normal draft path takes the day.")
        return None

    # Caption: the entry's approved lines + brand voice CTA/hashtags (no new text).
    if voice is None:
        voice = load_voice(voice_path or config.VOICE_DOC_PATH)
    note = "\n\n".join(entry.approved_lines())
    creative = Creative(path=art["path"], media_type="infographic", client_note=note)
    if voice is not None:
        caption, hashtags, _ = TemplateGenerator().build(voice, creative)
    else:
        caption, hashtags = note, []

    return Draft(
        draft_id=_make_id(account.key, f"social_proof_{entry.kind}", day_key),
        account_key=account.key, platform=account.platform,
        caption=caption, hashtags=list(hashtags),
        creative_path=art["path"], creative_public_url=hosted,
        scheduled_for=schedule.scheduled_for(day_key), status=DraftStatus.PENDING,
        source_fragments=entry.approved_lines(),  # audit: verified entry text only
    )
