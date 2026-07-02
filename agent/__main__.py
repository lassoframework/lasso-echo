"""
CLI entrypoint.

  python -m agent run-daily             # draft one post per account, post cards to Slack
  python -m agent dry-run               # run the whole Stage 1 loop OFFLINE, no tokens
  python -m agent intake-doc <path>     # turn a client PDF into draft posts (held for approval)
  python -m agent check-tokens          # manual token watchdog run (needs the flag armed)
  python -m agent capture-baseline      # MANUAL, read-only: pre-Echo posting baseline to /data
  python -m agent status                # show flag + gate state

Approval actions are handled by your Slack listener calling
agent.approvals.handle_action(...). A minimal manual hook is included for
testing the reply protocol locally.
"""
import os
import sys

from . import config
from .runner import run_daily


class ConsolePoster:
    """Stand-in for Slack: renders the approval card to the console."""
    def __init__(self):
        self.cards = []
    def post_approval_card(self, draft):
        self.cards.append(draft)
        print("\n" + "=" * 64)
        if draft.status.value == "blocked":
            print(f"  [BLOCKED] {draft.account_key}: {draft.blocked_reason}")
            return {"ok": True}
        kind = "STORY APPROVAL CARD" if getattr(draft, "is_story", False) else "APPROVAL CARD"
        print(f"  {kind}  ->  #echoclaude")
        print(f"  Account   : {draft.account_key} ({draft.platform})")
        print(f"  Scheduled : {draft.scheduled_for}")
        print(f"  Creative  : {draft.creative_public_url or draft.creative_path}")
        print(f"  Draft ID  : {draft.draft_id}")
        print("  " + "-" * 60)
        print("  CAPTION:")
        for line in (draft.caption or "(empty)").splitlines():
            print(f"    {line}")
        print(f"  HASHTAGS: {' '.join(draft.hashtags)}")
        print("  " + "-" * 60)
        print(f"  Reply:  approve {draft.draft_id}  |  edit {draft.draft_id} <note>  |  skip {draft.draft_id}")
        return {"ok": True}
    def post_notice(self, text):
        print(f"\n[NOTICE] {text}")
        return {"ok": True}


def _status():
    print("AGENT status")
    # gates (all read from config at call time; display only)
    print("  -- gates --")
    print(f"  master_enabled : {config.master_enabled()}  (env AGENT_ENABLED)")
    print(f"  publish_enabled: {config.publish_enabled()}  (env AGENT_PUBLISH_ENABLED)")
    print(f"  approver       : {config.APPROVER_SLACK_ID}")
    print(f"  voice doc      : {config.VOICE_DOC_PATH}")
    print(f"  library        : {config.LIBRARY_PATH}")
    mode = "DRAFT-ONLY" if not config.publish_enabled() else "PUBLISH ARMED"
    print(f"  mode           : {mode}")
    # capability flags (all default OFF)
    print("  -- capability flags --")
    print(f"  content_brain  : {config.content_brain_enabled()}  (env AGENT_CONTENT_BRAIN_ENABLED)")
    print(f"  creative_studio: {config.creative_studio_enabled()}  (env AGENT_NANO_ENABLED)")
    print(f"  hosting        : {config.hosting_enabled()}  (env AGENT_HOSTING_ENABLED)")
    print(f"  gbp            : {config.gbp_enabled()}  (env AGENT_GBP_ENABLED)")
    print(f"  reporting      : {config.reporting_enabled()}  (env AGENT_REPORTING_ENABLED)")
    print(f"  comments       : {config.comments_enabled()}  (env AGENT_COMMENTS_ENABLED)")
    print(f"  doc_intake     : {config.doc_intake_enabled()}  (env AGENT_DOC_INTAKE_ENABLED)")
    print(f"  social_proof   : {config.social_proof_enabled()}  (env AGENT_SOCIAL_PROOF_ENABLED)")
    print(f"  intake         : {config.intake_enabled()}  (env AGENT_INTAKE_ENABLED)")
    print(f"  grade          : {config.grade_enabled()}  (env AGENT_GRADE_ENABLED)")
    print(f"  knowledge      : {config.knowledge_enabled()}  (env AGENT_KNOWLEDGE_ENABLED)")
    print(f"  rotation       : {config.rotation_enabled()}  (env AGENT_ROTATION_ENABLED, "
          f"window {config.ROTATION_WINDOW_DAYS}d)")
    print(f"  summit         : {config.summit_campaign_enabled()}  (env AGENT_SUMMIT_CAMPAIGN_ENABLED)")
    print(f"  stories        : {config.stories_enabled()}  (env AGENT_STORIES_ENABLED)")
    print(f"  caption_seo    : {config.caption_seo_enabled()}  (env AGENT_CAPTION_SEO_ENABLED)")
    print(f"  platform_var   : {config.platform_variants_enabled()}  (env AGENT_PLATFORM_VARIANTS_ENABLED)")
    print(f"  idempotent     : {config.idempotent_drafts_enabled()}  (env AGENT_IDEMPOTENT_DRAFTS_ENABLED)")
    print(f"  ops_alerts     : {config.ops_alerts_enabled()}  (env AGENT_OPS_ALERTS_ENABLED)")
    print(f"  publish_confirm: {config.publish_confirm_enabled()}  (env AGENT_PUBLISH_CONFIRM_ENABLED)")
    print(f"  token_watchdog : {config.token_watchdog_enabled()}  (env AGENT_TOKEN_WATCHDOG_ENABLED, "
          f"warn at {config.token_warn_days()} days)")
    # posting schedule (2026 cadence)
    print("  -- posting schedule --")
    print(f"  primary time   : {config.POSTING_PRIMARY_TIME}")
    print(f"  morning time   : {config.POSTING_MORNING_TIME}")
    print(f"  posts per day  : {config.POSTS_PER_DAY}")
    print(f"  skip days      : {config.POSTING_SKIP_DAYS}")
    print(f"  priority days  : {config.POSTING_PRIORITY_DAYS}")
    print(f"  timezone       : {config.POSTING_TIMEZONE}")


def _dry_run():
    """Run the full Stage 1 loop offline: draft -> card -> approve -> log. No tokens."""
    from .store import PendingStore
    from .approvals import handle_action
    from .accounts import get_account

    os.environ["AGENT_ENABLED"] = "true"            # arm master for the run
    os.environ.pop("AGENT_PUBLISH_ENABLED", None)   # ensure publish OFF (draft-only)

    print("\n#### ECHO DRY RUN  ·  draft-only, no Meta writes, no tokens ####")
    _status()

    poster = ConsolePoster()
    store = PendingStore(path="dry_run_pending.json")
    out = run_daily(poster=poster)
    if out["status"] != "drafted":
        print(f"\nRun ended early: {out['status']}")
        return

    for d in out["drafts"]:
        if d.status.value != "blocked":
            store.put(d)

    # simulate Blake approving the first non-blocked draft
    target = next((d for d in out["drafts"] if d.status.value != "blocked"), None)
    if not target:
        return
    print("\n" + "#" * 64)
    print(f"  SIMULATING APPROVE from {config.APPROVER_SLACK_ID}: approve {target.draft_id}")
    res = handle_action("approve", target, actor_slack_id=config.APPROVER_SLACK_ID,
                        account=get_account(target.account_key))
    print(f"  RESULT: ok={res.ok}  ->  {res.detail}")
    print("  (mode 'would_publish' means draft-only worked: NOTHING was sent to Meta)")
    print("#" * 64 + "\n")


def _intake_doc(args):
    """python -m agent intake-doc <path> [--max N]: turn a client PDF into draft posts,
    all held for approval. Nothing publishes; the PDF is raw material, not approved fact."""
    path, max_posts, i = None, 7, 0
    while i < len(args):
        if args[i] == "--max" and i + 1 < len(args):
            max_posts = int(args[i + 1]); i += 2; continue
        if path is None and not args[i].startswith("--"):
            path = args[i]
        i += 1
    if not path:
        print("usage: python -m agent intake-doc <path> [--max N]")
        return
    from .doc_intake import process_document
    drafts = process_document(path, max_posts=max_posts)
    if drafts is None:
        print("doc intake is OFF (set AGENT_DOC_INTAKE_ENABLED=true to arm it). Nothing done.")
        return
    pending = sum(1 for d in drafts if d.status.value != "blocked")
    print(f"\nintake-doc: {len(drafts)} draft(s), {pending} pending, "
          f"{len(drafts) - pending} blocked (all held for approval, nothing published)")
    poster = ConsolePoster()
    for d in drafts:
        poster.post_approval_card(d)


def _check_tokens():
    """python -m agent check-tokens: manual token watchdog run. Prints which
    credential and days remaining ONLY; a token value is never printed."""
    from .token_watchdog import check_tokens
    out = check_tokens()
    if out["status"] == "disabled":
        print("token watchdog is OFF (set AGENT_TOKEN_WATCHDOG_ENABLED=true to arm it). "
              "Nothing checked.")
        return
    print(f"check-tokens: {len(out['results'])} credential(s) checked "
          f"(warn at {config.token_warn_days()} days)")
    for r in out["results"]:
        days = r["days_remaining"]
        days_str = f"{days} day(s) remaining" if days is not None else "expiry unknown"
        print(f"  {r['account']}: {r['status']} ({days_str})")


def _capture_baseline():
    """python -m agent capture-baseline: MANUAL, READ-ONLY pre-Echo baseline.
    Run by hand once; it is never scheduled and never writes to Meta."""
    from .baseline import capture_baseline
    print("capture-baseline: reading recent posting history (READ-ONLY, run by hand)")
    capture_baseline()


def main(argv=None):
    argv = argv or sys.argv[1:]
    cmd = argv[0] if argv else "status"
    if cmd == "run-daily":
        out = run_daily()
        print(f"run-daily -> {out['status']}, {len(out['drafts'])} draft(s)")
    elif cmd == "listen":
        from .listener import run_listener
        run_listener()
    elif cmd == "dry-run":
        _dry_run()
    elif cmd == "intake-doc":
        _intake_doc(argv[1:])
    elif cmd == "intake-web":
        # SEPARATE web process (own Railway service). R2 only, never /data.
        from .intake_web import serve
        serve()
    elif cmd == "draft-bible":
        # MANUAL onboarding tool: intake doc -> DRAFT bible + social proof under
        # brand_voice/drafts/<client>/. Never auto-activated; a human copies files.
        client, intake, args = "", "", argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--client" and i + 1 < len(args):
                client = args[i + 1]; i += 2; continue
            if args[i] == "--intake" and i + 1 < len(args):
                intake = args[i + 1]; i += 2; continue
            i += 1
        if not client or not intake:
            print("usage: python -m agent draft-bible --client <key> --intake <path>")
        else:
            from .bible_drafter import run as draft_bible_run
            bible_path, proof_path = draft_bible_run(client, intake)
            print(f"DRAFTS written (review + activate by hand):\n  {bible_path}\n  {proof_path}")
    elif cmd == "regen-library":
        # MANUAL batch rebuild of the seed library in the v2 house style (never
        # scheduled, no flag arms it into the daily path). Prints one public URL
        # per card for the eyeball pass. Nothing it makes can post on its own.
        # STRICT parsing: a typo or unsupported form errors out loudly; it can
        # never silently fall through to the full 10-card batch.
        from .regen_library import parse_args, run as regen_run
        only, dry_run, err = parse_args(argv[1:])
        if err:
            print(err)
        else:
            regen_run(only=only, dry_run=dry_run)
    elif cmd == "check-tokens":
        _check_tokens()
    elif cmd == "capture-baseline":
        _capture_baseline()
    elif cmd == "status":
        _status()
    else:
        print(f"unknown command: {cmd}")
        _status()


if __name__ == "__main__":
    main()
