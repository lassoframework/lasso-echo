"""
Summit campaign: one summit post per week inside the existing daily cadence (it
takes the day's feed slot, never adds to the cadence).

Dormant behind AGENT_SUMMIT_CAMPAIGN_ENABLED (default OFF). Drafted ONLY from the
VERIFIED FACTS and APPROVED ANGLES blocks of brand_voice/knowledge/
04_summit_campaign.md, read through the knowledge gate (so LOCKED / PENDING /
NOT FOUND content can never leak into a summit post). Angles rotate by ISO week so
no angle repeats within 3 weeks (guaranteed when the file carries 3+ angles). The
CTA is always "Claim your seat" with https://lassoframework.com/summit. The
campaign auto-stops after 2026-11-08: from November 9 the builder returns None
forever, no flag flip needed.
"""

import os
import re
from datetime import date

from . import config, creative_studio, knowledge, media_host, ops_alerts, schedule
from .drafter import Draft, DraftStatus, _make_id

SUMMIT_FILE = "04_summit_campaign.md"


def load_campaign(knowledge_dir=None):
    """
    (facts, angles) from the summit file's VERIFIED FACTS and APPROVED ANGLES
    blocks, via the knowledge gate. ([], []) when the file or blocks are absent.
    """
    knowledge_dir = knowledge_dir or config.KNOWLEDGE_DIR
    path = os.path.join(knowledge_dir, SUMMIT_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = knowledge._usable_lines(fh.read())
    except OSError:
        return [], []

    # Split into blocks by heading, then join wrapped list items inside each block
    # so a multi-line fact or angle is ONE string, wording intact across the wrap.
    facts, angles, current, buf = [], [], None, []

    def _flush():
        if current is not None:
            for item in knowledge.join_items(buf):
                cleaned = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", item).strip()
                if cleaned:
                    current.append(cleaned)

    for line in lines:
        if re.match(r"^#{1,6}\s", line):
            _flush()
            buf = []
            upper = line.upper()
            current = (facts if "VERIFIED FACTS" in upper
                       else angles if "APPROVED ANGLES" in upper else None)
            continue
        buf.append(line)
    _flush()
    return facts, angles


def campaign_active(day_key):
    """True through SUMMIT_END_DATE inclusive; False after (auto-stop)."""
    return date.fromisoformat(day_key) <= date.fromisoformat(config.SUMMIT_END_DATE)


def is_summit_day(day_key):
    """The one weekly summit slot (inside the daily cadence, never additional)."""
    return schedule.weekday_abbr(day_key) == config.SUMMIT_DAY


def pick_angle(angles, day_key):
    """ISO-week rotation: consecutive weeks take consecutive angles, so with 3+
    approved angles no angle repeats within any 3-week span."""
    if not angles:
        return None
    week = date.fromisoformat(day_key).isocalendar()[1]
    return angles[week % len(angles)]


def build_summit_draft(account, day_key, *, voice=None, voice_path=None,
                       nano_client=None, s3_client=None, knowledge_dir=None):
    """
    The week's summit draft, or None whenever the campaign must stay out of the way
    (flag off, past the end date, not the summit day, file/blocks missing, image
    generation or hosting unavailable). Never blocks normal drafting.
    """
    if not config.summit_campaign_enabled():
        return None
    if not campaign_active(day_key):
        return None  # auto-stopped: the summit is over
    if not is_summit_day(day_key):
        return None

    facts, angles = load_campaign(knowledge_dir)
    if not facts or not angles:
        return None  # nothing verified/approved to draft from; silently absent

    angle = pick_angle(angles, day_key)
    week = date.fromisoformat(day_key).isocalendar()[1]
    fact = facts[week % len(facts)]  # facts rotate on the same clock

    art = creative_studio.generate(angle, facts, client=nano_client)
    if not art:
        print(f"[summit] {account.key}: card generation unavailable; "
              "normal draft path takes the day.")
        ops_alerts.alert(
            f"summit: studio returned nothing for {account.key} "
            "(studio dark or Gemini unavailable); normal draft path takes the day."
        )
        return None
    hosted = media_host.host_media(art["path"], account.key, client=s3_client)
    if not hosted:
        print(f"[summit] {account.key}: hosting failed; normal draft path takes the day.")
        return None

    # Caption: the approved angle + one verified fact, verbatim, then THE fixed CTA.
    cta_line = f"{config.SUMMIT_CTA}: {config.SUMMIT_URL}"
    caption = "\n\n".join([angle, fact, cta_line])
    hashtags = list(getattr(voice, "hashtags", []) or [])[:5]

    return Draft(
        draft_id=_make_id(account.key, "summit", day_key),
        account_key=account.key, platform=account.platform,
        caption=caption, hashtags=hashtags,
        creative_path=art["path"], creative_public_url=hosted,
        scheduled_for=schedule.scheduled_for(day_key), status=DraftStatus.PENDING,
        source_fragments=[angle, fact, cta_line],  # audit: approved lines only
    )
