"""
CLI entrypoint.

  python -m agent help                  # the FULL command list (all ~40 commands, grouped)
  python -m agent run-daily             # draft one post per account, post cards to Slack
  python -m agent dry-run               # run the whole Stage 1 loop OFFLINE, no tokens
  python -m agent status                # show every flag, gate, source path, and the schedule

Approval actions are handled by your Slack listener calling
agent.approvals.handle_action(...). A minimal manual hook is included for
testing the reply protocol locally.
"""
import os
import re
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
    print(f"  category_rotation: {config.category_rotation_enabled()}  "
          f"(env AGENT_CATEGORY_ROTATION)")
    print(f"  client_sources : {config.client_sources_enabled()}  (env AGENT_CLIENT_SOURCES)")
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
    print(f"  plan_month     : {config.plan_month_enabled()}  (env AGENT_PLAN_MONTH_ENABLED)")
    print(f"  review_cycle   : {config.review_cycle_enabled()}  (env AGENT_REVIEW_CYCLE_ENABLED)")
    print(f"  weekly_report  : {config.weekly_report_enabled()}  (env AGENT_WEEKLY_REPORT_ENABLED)")
    print(f"  episode_inbox  : {config.episode_inbox_enabled()}  (env AGENT_EPISODE_INBOX_ENABLED)")
    print(f"  media_inbox    : {config.media_inbox_enabled()}  (env AGENT_MEDIA_INBOX_ENABLED)")
    print(f"  ghl_intake     : {config.ghl_intake_enabled()}  (env AGENT_GHL_INTAKE_ENABLED)")
    print(f"  whatsapp_intake: {config.whatsapp_intake_enabled()}  (env AGENT_WHATSAPP_INTAKE_ENABLED)")
    print(f"  tenant_brain   : {config.tenant_brain_enabled()}  (env AGENT_TENANT_BRAIN_ENABLED)")
    print(f"  opus_factory   : {config.opus_factory_enabled()}  (env AGENT_OPUS_FACTORY_ENABLED)")
    print(f"  clipper        : {config.clipper_enabled()}  (env AGENT_CLIPPER_ENABLED)")
    print(f"  clipper_render : {config.clipper_render_enabled()}  (env AGENT_CLIPPER_RENDER_ENABLED)")
    print(f"  services_cat   : {config.services_category_enabled()}  (env AGENT_SERVICES_CATEGORY)")
    # sources & paths (where the drafting content actually comes from)
    print("  -- sources & paths --")
    print(f"  source doc     : {config.SOURCE_DOC_PATH}  (env AGENT_SOURCE_DOC_PATH)")
    print(f"  knowledge dir  : {config.KNOWLEDGE_DIR}  (env AGENT_KNOWLEDGE_DIR)")
    print(f"  book dir       : {config.BOOK_DIR}  (env AGENT_BOOK_DIR)")
    print(f"  slack channel  : {config.SLACK_CHANNEL_ID or '(unset)'}  (env AGENT_SLACK_CHANNEL_ID)")
    # posting schedule (2026 cadence)
    print("  -- posting schedule --")
    print(f"  primary time   : {config.POSTING_PRIMARY_TIME}")
    print(f"  morning time   : {config.POSTING_MORNING_TIME}")
    print(f"  posts per day  : {config.POSTS_PER_DAY}")
    print(f"  skip days      : {config.POSTING_SKIP_DAYS}")
    print(f"  priority days  : {config.POSTING_PRIORITY_DAYS}")
    print(f"  timezone       : {config.POSTING_TIMEZONE}")
    # scheduler process heartbeat (written by the listen loop each cycle)
    print("  -- scheduler --")
    from .listener import read_scheduler_heartbeat
    hb = read_scheduler_heartbeat()
    if hb:
        print(f"  heartbeat      : {hb.get('ts', '?')}")
        print(f"  next fire      : {hb.get('next_fire', '?')}")
    else:
        print("  heartbeat      : (none recorded — is the listen process running?)")


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


def _whatsapp_status():
    """python -m agent whatsapp-status: show WhatsApp intake env status.
    Never prints a secret or token value; only 'set' or 'not set'."""
    enabled = config.whatsapp_intake_enabled()
    app_secret = os.environ.get("AGENT_WHATSAPP_APP_SECRET", "")
    token = os.environ.get("AGENT_WHATSAPP_TOKEN", "")
    phone_id = os.environ.get("AGENT_WHATSAPP_PHONE_NUMBER_ID", "")
    verify_token = os.environ.get("AGENT_WHATSAPP_VERIFY_TOKEN", "")

    def _yn(v):
        return "yes" if v else "no"

    def _set(v):
        return "set" if v else "not set"

    print("WHATSAPP INTAKE STATUS")
    print(f"enabled: {_yn(enabled)} (AGENT_WHATSAPP_INTAKE_ENABLED)")
    print(f"app_secret: {_set(app_secret)}")
    print(f"token: {_set(token)}")
    print(f"phone_number_id: {_set(phone_id)}")
    print(f"verify_token: {_set(verify_token)}")

    if not enabled:
        print("preflight: WARN (disabled)")
    elif app_secret and token and phone_id and verify_token:
        print("preflight: PASS")
    else:
        print("preflight: FAIL (enabled but vars missing)")


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
    if not out["results"]:
        print("check-tokens: no accounts with tokens to check (no active "
              "accounts, or none has its token env set).")
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


def _config_check():
    """Audit env vars read in agent/ code against docs/ENV.md.
    Informational only: exit 0 always, never a CI blocker."""
    import pathlib

    agent_dir = pathlib.Path(__file__).parent
    repo_root = agent_dir.parent

    # --- 1. Scan all .py files in agent/ for os.environ reads ---
    # Match both os.environ.get("VARNAME" ...) and os.environ["VARNAME"]
    env_get_pattern = re.compile(r'os\.environ\.get\(\s*["\']([A-Z][A-Z0-9_]+)["\']')
    env_index_pattern = re.compile(r'os\.environ\[\s*["\']([A-Z][A-Z0-9_]+)["\']')

    code_vars = {}  # varname -> first filename found
    for py_file in sorted(agent_dir.glob("*.py")):
        text = py_file.read_text(errors="replace")
        for name in env_get_pattern.findall(text):
            if name not in code_vars:
                code_vars[name] = py_file.name
        for name in env_index_pattern.findall(text):
            if name not in code_vars:
                code_vars[name] = py_file.name

    # --- 2. Parse docs/ENV.md for documented var names ---
    env_md_path = repo_root / "docs" / "ENV.md"
    documented = set()
    if env_md_path.exists():
        md_text = env_md_path.read_text(errors="replace")
        # Table rows like: | VARNAME | ...
        table_pattern = re.compile(r'\|\s*([A-Z][A-Z0-9_]+(?:[/<>][A-Z_][A-Z0-9_/<>]*)*)\s*[|/]')
        for match in table_pattern.finditer(md_text):
            raw = match.group(1)
            # Compound entries like AGENT_S3_BUCKET / AGENT_S3_ENDPOINT split on /
            for part in re.split(r'[/<>]', raw):
                part = part.strip()
                if re.match(r'^[A-Z][A-Z0-9_]{1,}$', part):
                    documented.add(part)
        # Also pick up bare ALL_CAPS identifiers in code blocks and prose
        bare_pattern = re.compile(r'\b([A-Z][A-Z0-9_]{3,})\b')
        for name in bare_pattern.findall(md_text):
            documented.add(name)

    # --- 3. Compute undocumented vars ---
    # PORT is Railway-injected; skip it.  Only flag AGENT_* and known external vars.
    known_external = {"META_APP_ID", "META_APP_SECRET", "OPUS_API_KEY",
                      "ANTHROPIC_API_KEY"}
    skip_vars = {"PORT"}
    undocumented = {}
    for var, fname in sorted(code_vars.items()):
        if var in skip_vars:
            continue
        is_agent = var.startswith("AGENT_")
        is_known_external = var in known_external
        if not is_agent and not is_known_external:
            continue
        if var not in documented:
            undocumented[var] = fname

    # --- 4. Print report ---
    print("=== config-check ===")
    print(f"Vars read in code: {len(code_vars)}")
    print(f"Vars documented in ENV.md: {len(documented)}")
    print(f"Potentially undocumented ({len(undocumented)}):")
    for var, fname in sorted(undocumented.items()):
        print(f"  {var}  ({fname})")
    print("=== done ===")


_COMMANDS = {
    "daily loop": [
        ("run-daily", "draft one post per account, card each for approval"),
        ("listen", "start the Slack listener + scheduler (the deployed worker)"),
        ("dry-run", "the whole Stage 1 loop OFFLINE, no tokens"),
        ("status", "flag + gate + schedule state"),
        ("help", "this list"),
    ],
    "planning & calendar": [
        ("plan-month", "fill open days for a month (--replan previews/rebuilds)"),
        ("approve-month", "approve a planned month through a date"),
        ("calendar / calendar-html", "client-facing month calendar HTML"),
        ("calendar-export", "export calendar plan to JSON"),
        ("seed-calendar", "seed a month from approval evidence"),
        ("monday-preview", "the week-ahead preview card"),
        ("runway", "days of approved content left per account"),
    ],
    "onboarding & intake": [
        ("onboard-client / add-client", "scaffold a new client account"),
        ("preflight", "is this account safe to draft for? (--account/--all, --live)"),
        ("seed-sources", "stock a gym's intake bundle into client sources (--review holds)"),
        ("intake-onboard", "one command: intake payload -> bible draft + pending sources + scan + plan + preflight"),
        ("welcome-kit", "client welcome kit PDF"),
        ("draft-bible", "draft a brand bible from an intake doc"),
        ("intake-doc", "turn a client PDF into held draft posts"),
        ("intake-web", "the upload web surface (own service)"),
        ("intake-create", "create drafts from an intake payload"),
    ],
    "content & library": [
        ("regen-library", "regenerate the creative library"),
        ("dam-scan", "scan/tag the library"),
        ("contact-sheet", "creative contact sheet"),
        ("backfill-insights", "pull insights for published posts"),
    ],
    "podcast & opus": [
        ("podcast-draft / podcast-status / podcast-transcript / podcast-cards "
         "/ podcast-learn", "podcast pipeline"),
        ("pull-opus / opus-pull / opus-check / opus-doctor / opus-organize",
         "Opus clip factory"),
        ("clip-episode", "score one episode's clip moments"),
        ("inbox-status", "episode inbox state"),
    ],
    "reporting": [
        ("report", "one account report"),
        ("monthly-report / monthly-review / grade-card", "month-end artifacts"),
        ("audit / fleet-status", "cross-account state"),
        ("gbp-check", "Google Business Profile check"),
    ],
    "ops": [
        ("check-tokens", "token watchdog run (flag must be armed)"),
        ("capture-baseline", "pre-Echo posting baseline (read-only)"),
        ("restore-store", "restore the draft store from a backup"),
        ("whatsapp-status", "show WhatsApp intake env status"),
        ("config-check", "audit env vars: code vs docs/ENV.md"),
    ],
}


def _usage():
    print("usage: python -m agent <command> [args]\n")
    for group, cmds in _COMMANDS.items():
        print(f"  -- {group} --")
        for name, desc in cmds:
            print(f"  {name:<28} {desc}")
    print("\n  run a command with missing args to see its own usage line")


def _print_run_daily(out):
    """One honest line per run: the status word, the reason, and the
    pending/blocked split — 'drafted, 0 draft(s)' with no cause was
    indistinguishable from a clean skip day or an all-blocked run."""
    status = (out or {}).get("status", "unknown")
    drafts = (out or {}).get("drafts") or []
    if status == "disabled":
        print("run-daily: disabled (set AGENT_ENABLED=true to arm the daily "
              "run). Nothing drafted.")
        return
    if status == "no_voice":
        print("run-daily: brand voice doc missing or empty; drafted nothing "
              "(see the Slack notice).")
        return
    pending = sum(1 for d in drafts
                  if getattr(getattr(d, "status", None), "value", "") == "pending")
    blocked = sum(1 for d in drafts
                  if getattr(getattr(d, "status", None), "value", "") == "blocked")
    line = (f"run-daily -> {status}, {len(drafts)} draft(s): "
            f"{pending} pending, {blocked} blocked")
    if not drafts:
        line += " (skip day, every account off-cadence, or nothing eligible)"
    print(line)


def main(argv=None):
    argv = argv or sys.argv[1:]
    cmd = argv[0] if argv else "status"
    if cmd == "run-daily":
        out = run_daily()
        _print_run_daily(out)
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
    elif cmd == "intake-create":
        # Tenant scaffold from a completed intake form JSON (AGENT_INTAKE_ENABLED).
        from .tenants import intake_create_cli
        payload_path, args = "", argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--payload" and i + 1 < len(args):
                payload_path = args[i + 1]; i += 2; continue
            i += 1
        intake_create_cli(payload_path)
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
        # --upload: upload HTML to R2 and post the public URL to Slack.
        acct_filter, do_upload, args = None, False, argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--account" and i + 1 < len(args):
                acct_filter = args[i + 1]; i += 2; continue
            if args[i].startswith("--account="):
                acct_filter = args[i].split("=", 1)[1]
            if args[i] == "--upload":
                do_upload = True
            i += 1
        from .monthly_report import run as monthly_run
        monthly_run(account=acct_filter, poster=ConsolePoster(),
                    pdf="--pdf" in argv[1:], upload=do_upload)
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
    elif cmd == "opus-pull":
        # Opus video factory (AGENT_OPUS_FACTORY_ENABLED): scan ALL projects,
        # score-gate, tag, hook-check, caption, dedupe, route to DRAFTS held for
        # the tap. Dry-run by default (prints the ranked plan, writes nothing);
        # --write builds the held drafts and posts them to the ops channel.
        from .opus_factory import opus_pull_cli
        start = None
        args = argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--start" and i + 1 < len(args):
                start = args[i + 1]; i += 2; continue
            i += 1
        poster = ConsolePoster() if "--write" in args else None
        store = None
        if "--write" in args:
            from .store import PendingStore
            store = PendingStore()
        opus_pull_cli(write="--write" in args, start_day=start,
                      poster=poster, store=store)
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
    elif cmd == "report":
        # Day 30 report, per account framing (frequency story for FB, the
        # engagement story for IG, frequency never published there). --dry
        # prints the exact Slack text, watermarked, and writes NOTHING.
        # --html: also build the monthly HTML report and upload it to R2
        #   (requires AGENT_REPORTING_ENABLED=true).
        account, dry, html_flag, args = None, False, False, argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--account" and i + 1 < len(args):
                account = args[i + 1]; i += 2; continue
            if args[i] == "--dry":
                dry = True; i += 1; continue
            if args[i] == "--html":
                html_flag = True; i += 1; continue
            i += 1
        from . import config as _cfg
        from .reporting import take_daily_snapshot
        if _cfg.reporting_enabled():
            print("Reporting: enabled")
            if account:
                take_daily_snapshot(account)
        else:
            print("Reporting: disabled (AGENT_REPORTING_ENABLED=false)")
        from .day30 import report_cli
        report_cli(account, dry)
        if html_flag:
            if not _cfg.reporting_enabled():
                print("report --html: AGENT_REPORTING_ENABLED is OFF. "
                      "HTML report not built.")
            else:
                from .monthly_report import run as monthly_run
                result = monthly_run(account=account, upload=True, poster=None)
                if result:
                    for key, val in result.items():
                        if key.endswith(":url"):
                            print(f"HTML report URL: {val}")
    elif cmd == "runway":
        # READ ONLY runway math in plain lines: eligible concepts by name,
        # exclusions with reasons, the consumption assumption, and the same
        # days number the digest prints. No digest change, no store writes.
        account, want_explain, args = None, False, argv[1:]
        i = 0
        while i < len(args):
            if args[i] == "--account" and i + 1 < len(args):
                account = args[i + 1]; i += 2; continue
            if args[i] == "--explain":
                want_explain = True; i += 1; continue
            i += 1
        if not account or not want_explain:
            print("usage: python -m agent runway --account <key> --explain")
        else:
            from .runway import explain as runway_explain
            runway_explain(account)
    elif cmd == "plan-month":
        # Fill open posting days from the eligible pool (AGENT_PLAN_MONTH_ENABLED).
        # --write saves pending drafts; without it the run is a dry print.
        from .plan_month import plan_cli
        plan_cli(argv[1:])
    elif cmd == "approve-month":
        # Bulk-approve pending plan drafts; first post per account held for tap.
        from .plan_month import approve_cli
        approve_cli(argv[1:])
    elif cmd in ("calendar", "calendar-html"):
        # Month calendar HTML from real draft store data. --out <path> writes
        # locally; --upload posts to R2. Cells show real image, category tile,
        # and full caption. Read only against state; buttons are display previews.
        from .calendar_artifact import cli as calendar_cli
        calendar_cli(argv[1:])
    elif cmd == "calendar-export":
        # Export month plan to JSON and a standalone HTML grid for all specified
        # accounts. Read only against state; never touches publishing gates.
        # Usage: calendar-export --account <key> [--account <key2>]
        #                        --month YYYY-MM [--out <json-path>]
        #                        [--html-out <html-path>]
        import re as _re
        from .calendar_artifact import assemble_month, generate_standalone_html
        args_rest = argv[1:]
        account_keys = []
        month_arg = None
        out_arg = None
        html_out_arg = None
        i = 0
        while i < len(args_rest):
            if args_rest[i] == "--account" and i + 1 < len(args_rest):
                account_keys.append(args_rest[i + 1]); i += 2; continue
            if args_rest[i] == "--month" and i + 1 < len(args_rest):
                month_arg = args_rest[i + 1]; i += 2; continue
            if args_rest[i] == "--out" and i + 1 < len(args_rest):
                out_arg = args_rest[i + 1]; i += 2; continue
            if args_rest[i] == "--html-out" and i + 1 < len(args_rest):
                html_out_arg = args_rest[i + 1]; i += 2; continue
            print(f"calendar-export: unrecognized argument: {args_rest[i]}")
            i += 1
        if not account_keys or not month_arg:
            print("usage: python -m agent calendar-export "
                  "--account <key> [--account <key2>] --month YYYY-MM "
                  "[--out <json-path>] [--html-out <html-path>]")
        elif not _re.fullmatch(r"\d{4}-\d{2}", month_arg):
            print(f"calendar-export: --month must be YYYY-MM, got {month_arg!r}")
        else:
            from .accounts import get_account
            import json as _json
            plans = {}
            for ak in account_keys:
                if get_account(ak) is None:
                    print(f"calendar-export: unknown account {ak!r}")
                    continue
                plans[ak] = assemble_month(ak, month_arg)
            if plans:
                payload = {"month": month_arg, "accounts": plans}
                json_path = out_arg or f"/tmp/echo_calendar_{month_arg}.json"
                with open(json_path, "w", encoding="utf-8") as _fh:
                    _json.dump(payload, _fh, indent=2)
                print(f"Calendar JSON exported: {json_path}")
                html_text = generate_standalone_html(plans, month_arg)
                html_path = html_out_arg or f"/tmp/echo_calendar_{month_arg}.html"
                with open(html_path, "w", encoding="utf-8") as _fh:
                    _fh.write(html_text)
                print(f"Calendar HTML generated: {html_path}")
    elif cmd == "monday-preview":
        # READ ONLY preflight: feed forecast, runway, tokens, heartbeats,
        # pending approvals, flags snapshot; one GO / NO GO verdict. Zero
        # side effects: the store is byte identical after a run.
        from .monday_preview import run as monday_run
        monday_run()
    elif cmd == "podcast-draft":
        # Manual release card recovery (AGENT_PODCAST_ENABLED): build a release
        # card for a specific episode on demand, bypassing the once-per-episode
        # guard. Held for Blake's tap. Use when the studio was dark on the
        # scheduled poll and the episode needs to be recovered by hand.
        episode, account_key, day_key_arg = None, None, None
        args_rest = argv[1:]
        i = 0
        while i < len(args_rest):
            if args_rest[i] == "--episode" and i + 1 < len(args_rest):
                try:
                    episode = int(args_rest[i + 1])
                except ValueError:
                    episode = None
                i += 2; continue
            if args_rest[i] == "--account" and i + 1 < len(args_rest):
                account_key = args_rest[i + 1]; i += 2; continue
            if args_rest[i] == "--day" and i + 1 < len(args_rest):
                day_key_arg = args_rest[i + 1]; i += 2; continue
            i += 1
        if episode is None:
            print("usage: python -m agent podcast-draft --episode N "
                  "[--account KEY] [--day YYYY-MM-DD]")
        else:
            from datetime import date
            from .accounts import active_accounts, get_account
            from .podcast_release import release_draft_for_episode
            accounts = ([get_account(account_key)] if account_key
                        else active_accounts())
            day = day_key_arg or date.today().isoformat()
            drafted = 0
            for acct in accounts:
                if acct is None:
                    print(f"podcast-draft: account {account_key!r} not found")
                    continue
                d = release_draft_for_episode(acct, episode, day)
                if d is not None:
                    print(f"podcast-draft: episode {episode} drafted for "
                          f"{acct.key} ({d.draft_id}) — held for approval")
                    drafted += 1
                else:
                    print(f"podcast-draft: episode {episode} not drafted "
                          f"for {acct.key} (flag off, episode not found, "
                          f"or studio unavailable)")
            if not drafted:
                print("podcast-draft: nothing drafted")
    elif cmd == "podcast-status":
        # READ ONLY probe: feed reachable, items seen, latest episode parsed,
        # the armed watermark, and an honest forecast of the next poll.
        from .podcast_feed import status_cli as podcast_status
        podcast_status()
    elif cmd == "contact-sheet":
        # Review sheet: one self contained HTML grid of the CURRENT library
        # renders per set, from library state (read only), uploaded to R2 under
        # echo/contact_sheets/<set>_<date>.html with the public URL printed.
        from .contact_sheet import cli as sheet_cli
        sheet_cli(argv[1:])
    elif cmd == "gbp-check":
        # READ-ONLY Google Business Profile probe: one honest status line.
        from .gbp_check import gbp_check
        gbp_check()
    elif cmd == "opus-check":
        # READ-ONLY connectivity probe: HTTP status + collection count, and the
        # truncated key-scrubbed body when the account looks empty to this key.
        from .opus_ingest import opus_check
        opus_check()
    elif cmd == "opus-doctor":
        # READ-ONLY factory preflight (AGENT_OPUS_FACTORY_ENABLED): hits the
        # proven /api/collections route and prints key prefix, base URL, HTTP
        # status, collection count, first collection's raw status. Separates
        # 404 (endpoint wrong) from 401 (auth wrong) — the operator's
        # is-it-key-or-route test before running opus-pull.
        from .opus_ingest import opus_doctor
        opus_doctor()
    elif cmd == "inbox-status":
        # READ ONLY episode inbox watcher state: prefix, poll interval,
        # files seen/claimed/processed/failed, last run time.
        from .episode_inbox import inbox_status_cli
        inbox_status_cli()
    elif cmd == "clip-episode":
        # Native clipper (AGENT_CLIPPER_ENABLED): stage a full episode video, get
        # word-level transcription, and let Claude pick 4-5 candidate Reel moments.
        # Phase 1 is SELECTION only: with no --render it prints the ranked plan and
        # writes/renders nothing (the approval checkpoint before any video work).
        from .clipper import clip_episode_cli
        clip_episode_cli(argv[1:])
    elif cmd == "opus-organize":
        # Add each pinned project's finished clips to one target collection so the
        # factory scan (collections only) can read them (AGENT_OPUS_FACTORY_ENABLED).
        # Dry-run by default (prints the plan, writes nothing); --write creates the
        # collection if absent and adds qualifying clips, idempotently. --name
        # overrides the collection name (default AGENT_OPUS_PODCAST_SHOW or
        # "LASSO Clips"). Projects come from AGENT_OPUS_PROJECT_IDS (no bulk
        # project-listing endpoint exists).
        from .opus_organize import organize_cli
        organize_cli(argv[1:])
    elif cmd == "preflight":
        from .preflight import cli as preflight_cli
        preflight_cli(argv[1:])
    elif cmd == "seed-sources":
        from .seed_sources import cli as seed_sources_cli
        seed_sources_cli(argv[1:])
    elif cmd == "intake-onboard":
        from .intake_onboard import cli as intake_onboard_cli
        intake_onboard_cli(argv[1:])
    elif cmd == "check-tokens":
        _check_tokens()
    elif cmd == "capture-baseline":
        _capture_baseline()
    elif cmd == "whatsapp-status":
        _whatsapp_status()
    elif cmd == "config-check":
        _config_check()
    elif cmd == "status":
        _status()
    elif cmd in ("help", "--help", "-h"):
        _usage()
    else:
        print(f"unknown command: {cmd}")
        _usage()


if __name__ == "__main__":
    main()
