"""
Daily Studio: the fully-automated daily post path.

When (and ONLY when) all three capabilities are armed — content brain + creative
studio + hosting — Echo drafts a LASSO infographic from the approved source doc,
generates the image (Nano Banana), hosts it for a public URL, and returns a PENDING
Draft for human approval.

Dormant by default: if any of the three flags is OFF it returns None and the caller
falls back to library creatives. NO FABRICATION: the infographic is built only from
the chosen pillar's approved hook (headline) + body lines (facts); a missing doc or a
blocked plan returns a BLOCKED Draft. Nothing here publishes — publishing stays behind
the approver gate and AGENT_PUBLISH_ENABLED.
"""

from . import config, content_planner, creative_studio, media_host, ops_alerts, schedule
from .drafter import Draft, DraftStatus, _make_id, variant_hashtags


def build_daily_infographic_draft(account, day_key, *, nano_client=None,
                                  s3_client=None, source_path=None):
    # All three capabilities must be armed; otherwise dormant (caller falls back).
    if not (config.content_brain_enabled()
            and config.creative_studio_enabled()
            and config.hosting_enabled()):
        return None

    draft_id = _make_id(account.key, "daily_infographic", day_key)

    def _blocked(reason):
        # A blocked plan surfaces on the Slack card AND (flag ON) as an ops alert.
        ops_alerts.alert(f"content plan blocked for {account.key}: {reason}")
        return Draft(
            draft_id=draft_id, account_key=account.key, platform=account.platform,
            caption="", hashtags=[], creative_path="", creative_public_url="",
            scheduled_for=day_key, status=DraftStatus.BLOCKED,
            blocked_reason="Daily studio: " + reason,
        )

    # Load the source doc + plan. A missing doc or a blocked plan blocks the draft.
    doc = content_planner.load_source_doc(source_path)
    if doc is None:
        return _blocked("source doc missing or empty. Not drafting.")

    plan = content_planner.plan_for(day_key, path=source_path)
    if plan.get("blocked"):
        return _blocked(plan["reason"])

    # Build the infographic ONLY from that pillar's approved lines. copy_bank stores
    # each pillar as {"hooks": [...], "bodies": [...]} (content_planner._parse_copy_bank);
    # read those exact keys. A missing block or empty hook/body is a doc/parse problem:
    # surface it as BLOCKED (it shows on the Slack card) rather than a silent None that
    # would masquerade as a normal library fallback.
    block = doc.copy_bank.get(plan["pillar"], {})
    hooks = list(block.get("hooks", []))
    facts = list(block.get("bodies", []))
    if not hooks and not facts:
        return _blocked(f"pillar '{plan['pillar']}' has no approved hook/body lines in lasso_now.md")
    headline = hooks[0] if hooks else ""
    if not facts:
        return _blocked(f"pillar '{plan['pillar']}' has no approved body lines in lasso_now.md")

    # The daily card draws a layout archetype on a deterministic rotation, so the
    # generated run varies in composition day to day (the brand never varies).
    art = creative_studio.generate(headline, facts, client=nano_client,
                                   account_key=account.key,
                                   archetype=creative_studio.archetype_for_day(day_key))
    if not art:
        # Acceptable library fallback for now, but make it VISIBLE: run-daily output
        # always, plus one ops alert when AGENT_OPS_ALERTS_ENABLED is armed.
        print(f"[daily-studio] {account.key}: image generation produced nothing; "
              "falling back to library creative.")
        ops_alerts.alert(f"creative generation returned empty for {account.key}; "
                         "fell back to a library creative.")
        return None

    hosted = media_host.host_media(art["path"], account.key, client=s3_client)
    if not hosted:
        # media_host already alerted with the exception detail; this line keeps the
        # existing run-daily visibility for the fallback itself.
        print(f"[daily-studio] {account.key}: media hosting failed (no public URL); "
              "falling back to library creative.")
        return None

    # Headline OCR check (AGENT_OCR_CHECK_ENABLED, OFF): a mismatch adds a
    # warning line to the card; it never blocks. Blake decides at the tap.
    warnings = []
    from .ocr_check import headline_warning
    warning = headline_warning(art["path"], headline)
    if warning:
        warnings.append(warning)

    return Draft(
        draft_id=draft_id, account_key=account.key, platform=account.platform,
        caption=plan["caption"],
        # Per-platform variant (flag OFF -> unchanged): selection from the same
        # approved tag set only. FB keeps at most 2; IG keeps up to 5.
        hashtags=variant_hashtags(account.platform, plan["hashtags"]),
        creative_path=art["path"], creative_public_url=hosted,
        scheduled_for=schedule.scheduled_for(day_key), status=DraftStatus.PENDING,
        source_fragments=[headline] + facts,  # no-fabrication audit trail
        warnings=warnings,
    )
