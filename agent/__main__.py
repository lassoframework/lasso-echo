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
    print(f"  connect        : {config.connect_enabled()}  (env AGENT_CONNECT_ENABLED)")
    print(f"  connect_tokens : {config.connect_tokens_enabled()}  (env AGENT_CONNECT_TOKENS_ENABLED)")
    print(f"  connect_grade  : {config.connect_grade_enabled()}  (env AGENT_CONNECT_GRADE_ENABLED)")
    print(f"  grade          : {config.grade_enabled()}  (env AGENT_GRADE_ENABLED)")
    print(f"  monthly_review : {config.monthly_review_enabled()}  (env AGENT_MONTHLY_REVIEW_ENABLED)")
    print(f"  knowledge      : {config.knowledge_enabled()}  (env AGENT_KNOWLEDGE_ENABLED)")
    print(f"  runway         : {config.runway_enabled()}  (env AGENT_RUNWAY_ENABLED)")
    print(f"  trust_ladder   : {config.trust_ladder_enabled()}  (env AGENT_TRUST_LADDER_ENABLED)")
    print(f"  trust_dryrun   : {config.trust_dryrun_enabled()}  (env AGENT_TRUST_DRYRUN)")
    print(f"  trust_autopub  : {config.trust_autopublish_enabled()}  (env AGENT_TRUST_AUTOPUBLISH)")
    print(f"  ocr_check      : {config.ocr_check_enabled()}  (env AGENT_OCR_CHECK_ENABLED)")
    print(f"  consent_guard  : {config.consent_guard_enabled()}  (env AGENT_CONSENT_GUARD_ENABLED)")
    print(f"  autotag        : {config.autotag_enabled()}  (env AGENT_AUTOTAG_ENABLED)")
    print(f"  spend_cap      : {config.spend_cap_enabled()}  (env AGENT_SPEND_CAP_ENABLED)")
    print(f"  digest         : {config.digest_enabled()}  (env AGENT_DIGEST_ENABLED)")
    print(f"  brain          : {config.brain_proposals_enabled()}  (env AGENT_BRAIN_PROPOSALS_ENABLED)")
    print(f"  backup         : {config.backup_enabled()}  (env AGENT_BACKUP_ENABLED)")
    print(f"  opus           : {config.opus_enabled()}  (env AGENT_OPUS_ENABLED)")
    print(f"  opus_poll      : {config.opus_poll_enabled()}  (env AGENT_OPUS_POLL_ENABLED)")
    print(f"  podcast        : {config.podcast_enabled()}  (env AGENT_PODCAST_ENABLED)")
    print(f"  rotation       : {config.rotation_enabled()}  (env AGENT_ROTATION_ENABLED, "
          f"window {config.ROTATION_WINDOW_DAYS}d)")
    print(f"  summit         : {config.summit_campaign_enabled()}  (env AGENT_SUMMIT_CAMPAIGN_ENABLED)")
    print(f"  book_campaign  : {config.book_campaign_enabled()}  (env AGENT_BOOK_CAMPAIGN_ENABLED)")
    print(f"  stories        : {config.stories_enabled()}  (env AGENT_STORIES_ENABLED)")
    print(f"  story_premade  : {config.story_premade_enabled()}  (env AGENT_STORY_PREMADE_ENABLED)")
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
        only, set_name, dry_run, err = parse_args(argv[1:])
        if err:
            print(err)
        else:
            regen_run(only=only, dry_run=dry_run, set_name=set_name)
    elif cmd == "onboard-client":
        # ONE-COMMAND Stage 3 onboarding from a completed intake. Missing fields
        # block with the list; touches no env, arms nothing.
        intake, key, name, args = "", "", "", argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--intake" and i + 1 < len(args):
                intake = args[i + 1]; i += 2; continue
            if args[i] == "--key" and i + 1 < len(args):
                key = args[i + 1]; i += 2; continue
            if args[i] == "--name" and i + 1 < len(args):
                name = args[i + 1]; i += 2; continue
            i += 1
        if not intake or not key:
            print("usage: python -m agent onboard-client --intake <file> --key <k> [--name <n>]")
        else:
            from .onboard_pipeline import onboard
            onboard(intake, key, name or None)
    elif cmd == "add-client":
        # MANUAL onboarding scaffold: config entry + voice/proof templates +
        # library folder + the by-hand checklist. Touches no env, arms nothing.
        key, name, args = "", "", argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--key" and i + 1 < len(args):
                key = args[i + 1]; i += 2; continue
            if args[i] == "--name" and i + 1 < len(args):
                name = args[i + 1]; i += 2; continue
            i += 1
        if not key:
            print("usage: python -m agent add-client --key <k> --name <n>")
        else:
            from .onboard import add_client
            add_client(key, name)
    elif cmd == "welcome-kit":
        # MANUAL client welcome kit (HTML + PDF): fixed template language only,
        # no pricing, no dashes. Renders to /data/reports/.
        key, args = "", argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--account" and i + 1 < len(args):
                key = args[i + 1]; i += 2; continue
            if args[i].startswith("--account="):
                key = args[i].split("=", 1)[1]
            i += 1
        if not key:
            print("usage: python -m agent welcome-kit --account <key>")
        else:
            from .welcome_kit import run as kit_run
            kit_run(key)
    elif cmd == "restore-store":
        # MANUAL restore: staging + verification counts; NEVER touches the live
        # db without --confirm (and then keeps it as .pre_restore.bak).
        from_key, confirm, args = "", False, argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--from" and i + 1 < len(args):
                from_key = args[i + 1]; i += 2; continue
            if args[i] == "--confirm":
                confirm = True
            i += 1
        if not from_key:
            print("usage: python -m agent restore-store --from <r2 key> [--confirm]")
        else:
            from .backup import restore_store
            restore_store(from_key, confirm=confirm)
    elif cmd == "fleet-status":
        # One line per account: name, trust level, runway days, last publish,
        # last error. Fixed-width so it reads clean at 100 accounts. No flag.
        from . import config as _cfg, db as _db
        from .accounts import active_accounts as _actives
        from .runway import runway_days as _runway
        from .trust import effective_level as _level
        with _db.connect() as conn:
            for a in _actives():
                try:
                    rw = _runway(a.key, a.library_prefix or _cfg.LIBRARY_PATH)
                except Exception:
                    rw = "?"
                row = conn.execute(
                    "SELECT MAX(published_at) AS lp FROM posts WHERE account_key=? "
                    "AND mode='published'", (a.key,)).fetchone()
                last_pub = (row["lp"] or "never")[:16]
                err = conn.execute(
                    "SELECT reason FROM audit WHERE kind='account_error' AND "
                    "account_key=? ORDER BY id DESC LIMIT 1", (a.key,)).fetchone()
                last_err = (err["reason"][:40] if err else "none")
                print(f"{a.key:<16} trust L{int(_level(a))}  runway {str(rw):>6}d  "
                      f"last publish {last_pub:<16}  last error {last_err}")
    elif cmd == "audit":
        # The readable decision trail. No flag: logging truth is always on.
        day, acct_f, args = None, None, argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--day" and i + 1 < len(args):
                day = args[i + 1]; i += 2; continue
            if args[i] == "--account" and i + 1 < len(args):
                acct_f = args[i + 1]; i += 2; continue
            i += 1
        from .db import audit_rows
        rows = audit_rows(day=day, account_key=acct_f)
        if not rows:
            print("audit: no decisions recorded for that filter.")
        for r in reversed(rows):
            who = r["account_key"] or "-"
            print(f"{r['ts']}  [{r['kind']:<15}] {who:<12} {r['subject']}: {r['reason']}")
    elif cmd == "dam-scan":
        # MANUAL DAM pass over the library: mark perceptual near-dupe groups in
        # sidecars, and (when AGENT_AUTOTAG_ENABLED) tag untagged assets.
        from . import config as _cfg
        from .dam import autotag, mark_near_dupes, read_sidecar
        lib = _cfg.LIBRARY_PATH
        groups = mark_near_dupes(lib)
        print(f"dam-scan: {len(groups)} near-dupe group(s) marked")
        if _cfg.autotag_enabled():
            import os as _os
            tagged = 0
            for name in sorted(_os.listdir(lib)):
                path = _os.path.join(lib, name)
                if (_os.path.splitext(name)[1].lower() in (".jpg", ".jpeg", ".png", ".webp")
                        and "people" not in read_sidecar(path)):
                    if autotag(path):
                        tagged += 1
            print(f"dam-scan: {tagged} asset(s) tagged")
    elif cmd == "seed-calendar":
        # Build the human-approved monthly calendar for the trust ladder from
        # approval evidence only. --write stores it in kv; default prints.
        acct_f, month, write, args = "", "", False, argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--account" and i + 1 < len(args):
                acct_f = args[i + 1]; i += 2; continue
            if args[i] == "--month" and i + 1 < len(args):
                month = args[i + 1]; i += 2; continue
            if args[i] == "--write":
                write = True
            i += 1
        if not acct_f or not month:
            print("usage: python -m agent seed-calendar --account <key> "
                  "--month YYYY-MM [--write]")
        else:
            from .seed_calendar import run as seed_run
            seed_run(acct_f, month, write=write)
    elif cmd == "backfill-insights":
        # By-hand per-post metrics backfill from the store's publish records
        # (views, never impressions). --dry lists work, touches nothing.
        acct_f, since, dry, args = "", "", False, argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--account" and i + 1 < len(args):
                acct_f = args[i + 1]; i += 2; continue
            if args[i] == "--since" and i + 1 < len(args):
                since = args[i + 1]; i += 2; continue
            if args[i] == "--dry":
                dry = True
            i += 1
        if not acct_f or not since:
            print("usage: python -m agent backfill-insights --account <key> "
                  "--since YYYY-MM-DD [--dry]")
        else:
            from .backfill import backfill_insights
            backfill_insights(acct_f, since, dry=dry)
    elif cmd == "monthly-review":
        # The 30 day loop: digest + PDF per account (AGENT_MONTHLY_REVIEW_ENABLED).
        # --dry is READ ONLY: prints everything, posts/writes nothing, and runs
        # even while the flag is OFF (evidence gathering without arming).
        acct_f, dry, args = None, False, argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--account" and i + 1 < len(args):
                acct_f = args[i + 1]; i += 2; continue
            if args[i].startswith("--account="):
                acct_f = args[i].split("=", 1)[1]
            if args[i] == "--dry":
                dry = True
            i += 1
        from .monthly_review import run as review_run
        review_run(account=acct_f, dry=dry, poster=ConsolePoster())
    elif cmd == "grade-card":
        # One page Social Grade card (HTML + PDF) from live store data. Respects
        # AGENT_GRADE_ENABLED; drafts nothing, posts nothing.
        acct_filter, args = None, argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--account" and i + 1 < len(args):
                acct_filter = args[i + 1]; i += 2; continue
            if args[i].startswith("--account="):
                acct_filter = args[i].split("=", 1)[1]
            i += 1
        from .grade_card import run as grade_run
        grade_run(account=acct_filter)
    elif cmd == "monthly-report":
        # The per-account 30 day cycle report from /data snapshots + posts, plus
        # the creative REFRESH proposal. Gated by AGENT_REPORTING_ENABLED.
        acct_filter, args = None, argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--account" and i + 1 < len(args):
                acct_filter = args[i + 1]; i += 2; continue
            if args[i].startswith("--account="):
                acct_filter = args[i].split("=", 1)[1]
            i += 1
        from .monthly_report import run as monthly_run
        monthly_run(account=acct_filter, poster=ConsolePoster(),
                    pdf="--pdf" in argv[1:])
    elif cmd == "pull-opus":
        # MANUAL Opus Clip ingest: list new finished clips since the watermark,
        # host to R2, file as video assets (future Reel DRAFTS via the normal
        # path). Nothing publishes; the key is env-only and never printed.
        # --verbose prints discovery route, per-source counts, and skip reasons.
        from .opus_ingest import pull as opus_pull
        out = opus_pull(verbose="--verbose" in argv[1:])
        if out is None:
            print("opus ingest is OFF (set AGENT_OPUS_ENABLED=true to arm it). Nothing done.")
        else:
            print(f"pull-opus: {out['pulled']} pulled, {out['skipped']} skipped, "
                  f"{out['failed']} failed")
    elif cmd == "podcast-transcript":
        # Podcast transcript ingest (AGENT_PODCAST_ENABLED): store one episode's
        # transcript as its APPROVED SOURCE (citation id podcast_ep<N>), from a
        # file or a url. Prints a short preview at most, never the transcript.
        episode, fpath, furl, args = None, "", "", argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--episode" and i + 1 < len(args):
                try:
                    episode = int(args[i + 1])
                except ValueError:
                    episode = None
                i += 2; continue
            if args[i] == "--file" and i + 1 < len(args):
                fpath = args[i + 1]; i += 2; continue
            if args[i] == "--url" and i + 1 < len(args):
                furl = args[i + 1]; i += 2; continue
            i += 1
        from .podcast_transcripts import ingest_cli
        ingest_cli(episode, fpath, furl)
    elif cmd == "podcast-cards":
        # Episode infographics (AGENT_PODCAST_ENABLED): extract 2 or 3 card
        # concepts VERBATIM from the stored transcript, every card citing
        # podcast_ep<N>, queued max one per day behind book priority, all held
        # for approval. Renders through the same house builder at serve time.
        episode, count, args = None, 2, argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--episode" and i + 1 < len(args):
                try:
                    episode = int(args[i + 1])
                except ValueError:
                    episode = None
                i += 2; continue
            if args[i] == "--count" and i + 1 < len(args):
                try:
                    count = int(args[i + 1])
                except ValueError:
                    count = 0
                i += 2; continue
            i += 1
        from .podcast_cards import cards_cli
        cards_cli(episode, count)
    elif cmd == "podcast-learn":
        # Episode learnings memory (AGENT_PODCAST_ENABLED): 3 to 7 verbatim
        # learnings from the stored transcript into
        # brand_voice/knowledge/podcast/ep<N>_learnings.md plus the rolling
        # index. Additive only; episode scoped citations (podcast_ep<N>).
        episode, count, args = None, None, argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--episode" and i + 1 < len(args):
                try:
                    episode = int(args[i + 1])
                except ValueError:
                    episode = None
                i += 2; continue
            if args[i] == "--count" and i + 1 < len(args):
                try:
                    count = int(args[i + 1])
                except ValueError:
                    count = 0
                i += 2; continue
            i += 1
        from .podcast_learn import learn_cli
        learn_cli(episode, count)
    elif cmd == "gbp-check":
        # READ-ONLY Google Business Profile probe: one honest status line.
        from .gbp_check import gbp_check
        gbp_check()
    elif cmd == "opus-check":
        # READ-ONLY connectivity probe: HTTP status + collection count, and the
        # truncated key-scrubbed body when the account looks empty to this key.
        from .opus_ingest import opus_check
        opus_check()
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
