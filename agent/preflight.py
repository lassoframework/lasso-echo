"""
Onboarding preflight: is this account safe to draft for?

    python -m agent preflight --account <key> [--live]
    python -m agent preflight --all [--live]

One line per check, PASS/WARN/FAIL, and a final verdict READY or NOT READY
with the blocking items listed. Exits nonzero on any FAIL so it can gate
scripts. READ ONLY without --live; --live additionally posts one dry test
message to the account's Slack channel and calls the Graph debug_token
endpoint (reusing the token watchdog's check).

The checks exist because every one of them has already failed silently once:
an account with no slack_channel routed its cards to LASSO's internal
channel; a thin library produced blocked cards every day; a missing voice
doc blocked drafting with nothing saying why up front.
"""

import os
import sys

from . import config
from .accounts import active_accounts, get_account

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _min_library():
    return int(os.environ.get("AGENT_PREFLIGHT_MIN_LIBRARY", "15"))


def _warn_library():
    return int(os.environ.get("AGENT_PREFLIGHT_WARN_LIBRARY", "30"))


_MEDIA_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov")


def _count_library(path):
    if not path or not os.path.isdir(path):
        return 0
    return sum(1 for n in os.listdir(path)
               if os.path.splitext(n)[1].lower() in _MEDIA_EXTS)


def check_account(account, live=False, poster=None, http=None):
    """Run every check for one account. Returns
    {"account", "checks": [{"name", "status", "detail"}], "verdict",
     "blocking": [names]}. Never raises; a check that errors is a FAIL."""
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    # 1. Slack channel: an account without its own channel routes cards to
    #    the shared default — silent for a client gym, and exactly the bug
    #    this preflight exists to catch. LASSO accounts (client zero) own
    #    the default channel by design.
    is_lasso = account.key.startswith("lasso")
    if account.slack_channel:
        add("slack_channel", PASS, f"cards route to {account.slack_channel}")
        if live:
            try:
                from .slack_surface import SlackPoster
                p = poster or SlackPoster(channel=account.slack_channel)
                resp = p.post_notice(
                    f"preflight test for {account.key}: this is the channel "
                    "your approval cards will use. No action needed.") or {}
                if resp.get("ok"):
                    add("slack_post", PASS, "dry test message posted")
                else:
                    add("slack_post", FAIL,
                        f"bot cannot post: {resp.get('error', 'not ok')}")
            except Exception as e:
                add("slack_post", FAIL, f"post failed: {type(e).__name__}")
    elif is_lasso:
        add("slack_channel", PASS,
            "uses the shared default channel (LASSO client zero)")
    else:
        add("slack_channel", FAIL,
            "no slack_channel set: cards would silently route to LASSO's "
            "internal channel. Set Account.slack_channel.")

    # 2. Approvers
    if account.approvers:
        add("approvers", PASS, f"{len(account.approvers)} approver(s) set")
    else:
        add("approvers", WARN,
            "no per-account approvers: only the global approver can act")

    # 3. Meta token presence (+ expiry when --live, via the watchdog's check)
    token = account.get_token()
    if not token:
        add("meta_token", FAIL,
            f"token env {account.token_env} is unset or empty")
    elif live:
        try:
            import time as _time
            from .token_watchdog import _check_one, _requests
            r = _check_one(http or _requests(), account, _time.time(),
                           config.token_warn_days(), None)
            status = r.get("status")
            days = r.get("days_remaining")
            if status in ("ok", "never_expires"):
                add("meta_token", PASS,
                    "valid" + (f", {days} day(s) remaining" if days is not None
                               else ", never expires"))
            elif status == "expiring":
                add("meta_token", WARN, f"expiring in {days} day(s)")
            else:
                add("meta_token", FAIL, f"debug_token says {status}")
        except Exception as e:
            add("meta_token", FAIL, f"expiry check failed: {type(e).__name__}")
    else:
        add("meta_token", PASS,
            "token env set (expiry not checked; use --live)")

    # 4. Target id env
    if os.environ.get(account.target_id_env):
        add("target_id", PASS, f"{account.target_id_env} set")
    else:
        add("target_id", FAIL, f"{account.target_id_env} is unset")

    # 5. Library depth: under the minimum the account drafts blocked cards
    n = _count_library(account.library_path())
    lo, hi = _min_library(), _warn_library()
    if n < lo:
        add("library", FAIL,
            f"{n} media item(s), minimum {lo}: this account will card "
            "blocked drafts. Stock the library first.")
    elif n < hi:
        add("library", WARN, f"{n} media item(s), thin (warn under {hi})")
    else:
        add("library", PASS, f"{n} media item(s)")

    # 6. Brand voice doc exists and is nonempty
    from .voice import load_voice
    if load_voice(account.voice_doc_path()) is not None:
        add("voice_doc", PASS, account.voice_doc_path())
    else:
        add("voice_doc", FAIL,
            f"voice doc missing or empty: {account.voice_doc_path()}")

    # 7. Gemini spend cap
    if config.spend_cap_enabled():
        add("spend_cap", PASS,
            f"armed, {os.environ.get('AGENT_GEMINI_DAILY_CAP', '40')}/day "
            "per account")
    else:
        add("spend_cap", WARN,
            "AGENT_SPEND_CAP_ENABLED off: image generation is uncapped")

    # 8. Category rotation flag state (informational, never blocks)
    add("category_rotation", PASS,
        "ON" if config.category_rotation_enabled() else
        "OFF (legacy priority chain)")

    blocking = [c["name"] for c in checks if c["status"] == FAIL]
    return {"account": account.key, "checks": checks,
            "verdict": "READY" if not blocking else "NOT READY",
            "blocking": blocking}


def print_report(report):
    print(f"preflight: {report['account']}")
    for c in report["checks"]:
        print(f"  {c['status']:<4} {c['name']:<17} {c['detail']}")
    if report["verdict"] == "READY":
        print(f"  verdict: READY")
    else:
        print(f"  verdict: NOT READY — fix: {', '.join(report['blocking'])}")


def cli(args):
    key, run_all, live = None, False, False
    i = 0
    while i < len(args):
        if args[i] == "--account" and i + 1 < len(args):
            key = args[i + 1]; i += 2; continue
        if args[i] == "--all":
            run_all = True; i += 1; continue
        if args[i] == "--live":
            live = True; i += 1; continue
        print(f"unrecognized: {args[i]}\n"
              "usage: python -m agent preflight --account <key> [--live] | "
              "--all [--live]")
        sys.exit(2)
    if not key and not run_all:
        print("usage: python -m agent preflight --account <key> [--live] | "
              "--all [--live]")
        sys.exit(2)

    if run_all:
        accounts = active_accounts()
    else:
        acct = get_account(key)
        if acct is None:
            known = ", ".join(a.key for a in active_accounts())
            print(f"preflight: no account matches '{key}' (known: {known}).")
            sys.exit(2)
        accounts = [acct]

    any_fail = False
    for acct in accounts:
        report = check_account(acct, live=live)
        print_report(report)
        if report["verdict"] != "READY":
            any_fail = True
    sys.exit(1 if any_fail else 0)
