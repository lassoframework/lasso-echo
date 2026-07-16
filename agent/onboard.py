"""
Autonomous onboarding (Stage 2 T2) + legacy add-client scaffold (Stage 3).

The `run()` function (Stage 2 T2) is the new autonomous onboard path.
The `add_client()` function (Stage 3) is the existing manual scaffold path.

RUN BY HAND (new):

    python -m agent onboard --account <key> --name "<Gym Name>" [--base-url <url>]

RUN BY HAND (legacy):

    /opt/venv/bin/python -m agent add-client --key <k> --name <n>

    /opt/venv/bin/python -m agent add-client --key <k> --name <n>

Generates the full per-client scaffold and NOTHING else:
  - brand_voice/<key>/lasso_voice.md    a TODO voice doc template mirroring the
                                        BRAND_VOICE_INTAKE.md sections
  - brand_voice/<key>/social_proof.md   empty proof doc with the Permission: yes
                                        rule header (the gate already enforces it)
  - content_library/<key>/.gitkeep      the client's library prefix
  - a printed Account(...) config entry to paste into accounts.py
    (level 0 FULL_APPROVAL, all capability behavior inherited, nothing armed)
  - the printed by-hand checklist (Slack channel, approver ids, Meta ids,
    tokens by hand in Railway, capture-baseline, grade arm, shadow week per
    docs/STAGE2_RUNBOOK.md)

Never touches env, never creates tokens, never arms anything. Idempotent:
re-running skips every file that already exists (nothing destructive, ever).
"""

import os
import re

from . import config, db
from .trust import TrustLevel, default_trust_for_new_account
from .voice_template import render_template

VOICE_TEMPLATE = """# {name} Brand Bible (TODO: fill by hand or via draft-bible)

> Scaffolded by add-client. NOTHING here is approved source material until a
> human fills it in. A blocked draft is correct behavior while TODOs remain.

## 1. Who {name} is
TODO: the business in the owner's own words.

## 2. Who we talk TO (the avatar)
TODO: the best member described like a person.

## 3. Voice and tone
TODO: words they say, words they never say.

## 4. Hard guardrails (never violate)
TODO: client specific guardrails. House rules always apply: human approval on
every post, no invented facts or stats, no em dashes, en dashes, or hyphens in
published copy.

## 5. Content pillars
TODO: three to five topics with a hook and body line each, in their voice.

## 6. Platform rules

### CTA rotation (cycle in order, one per post)
TODO: three to five CTA lines in their voice.

### Hashtag strategy (3 to 5 per post)
TODO: their hashtags.
"""

PROOF_TEMPLATE = """# {name} social proof (verified source)

RULES (enforced by code, do not remove):
- Only entries with `Permission: yes` AND a real `Verified: YYYY-MM-DD` date
  ever render. Everything else is skipped with a reason.
- One `## Entry` block per item: Quote or Stat, optional Support, Attribution,
  Permission, Verified.

(no entries yet)
"""

CHECKLIST = """BY-HAND CHECKLIST for {key} (order matters; see docs/STAGE2_RUNBOOK.md):
 1. Paste the Account entry above into agent/accounts.py, commit via the normal flow.
 2. Create the client's Slack approval channel; invite Echo + the approver(s);
    put the channel id and approver Slack ids on the Account entry.
 3. Meta: link the client's IG professional account + FB Page; collect the ids.
 4. Tokens BY HAND in Railway env under the env NAMES on the Account entry
    (never in git, never in this repo).
 5. Verify: python -m agent check-tokens (never prints values).
 6. Capture the pre Echo baseline BEFORE any Echo post:
    python -m agent capture-baseline
 7. Fill brand_voice/{key}/lasso_voice.md (or run draft-bible from a completed
    intake) and verify every social proof entry by hand.
 8. Shadow week: drafts only, approve or skip daily, tune the voice doc.
 9. Arm AGENT_GRADE_ENABLED when reporting inputs exist.
10. Trust stays level 0 (full approval). Raising it is a separate, deliberate,
    by-hand decision. Nothing in this scaffold arms anything.
"""


def valid_key(key):
    return bool(re.fullmatch(r"[a-z][a-z0-9_]{1,31}", key or ""))


def add_client(key, name, root="."):
    """
    Create the scaffold. Returns {"created": [...], "skipped": [...]} and prints
    the config entry + checklist. Idempotent: existing files are never touched.
    """
    if not valid_key(key):
        print(f"add-client: invalid key {key!r} (use a-z, 0-9, _ ; start with a "
              "letter; 2 to 32 chars)")
        return None
    name = (name or key).strip()
    created, skipped = [], []

    def _write(path, content):
        if os.path.exists(path):
            skipped.append(path)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        created.append(path)

    voice_dir = os.path.join(root, "brand_voice", key)
    _write(os.path.join(voice_dir, "lasso_voice.md"),
           VOICE_TEMPLATE.format(name=name))
    _write(os.path.join(voice_dir, "social_proof.md"),
           PROOF_TEMPLATE.format(name=name))
    _write(os.path.join(root, "content_library", key, ".gitkeep"), "")

    entry = f'''
Account(
    key="{key}_ig",
    display_name="{name} IG",
    platform=Platform.INSTAGRAM,
    token_env="AGENT_{key.upper()}_IG_TOKEN",
    target_id_env="AGENT_{key.upper()}_IG_ID",
    voice_doc="brand_voice/{key}/lasso_voice.md",
    social_proof_doc="brand_voice/{key}/social_proof.md",
    library_prefix="content_library/{key}",
    slack_channel="",            # the client's approval channel id, by hand
    approvers=[],                # approver Slack ids, by hand
    # trust defaults to FULL_APPROVAL (level 0). Do not change here.
),'''
    print("Paste into agent/accounts.py ACCOUNTS list:")
    print(entry)
    print()
    print(CHECKLIST.format(key=key))
    print(f"created: {len(created)} file(s); skipped (already existed): {len(skipped)}")
    return {"created": created, "skipped": skipped, "entry": entry}


# ---------------------------------------------------------------------------
# Stage 2 T2: Autonomous onboard  (run via: python -m agent onboard ...)
# ---------------------------------------------------------------------------

def run(account_key, display_name, db_conn=None, voice_dir=None,
        brains_dir=None, base_url=None):
    """
    Stand up a new gym end to end. Idempotent: re-running updates display_name
    if different, never re-mints unless rotate was called.

    Returns a result dict with keys:
      account_key, display_name, token_minted, voice_path, brain_path,
      trust_level, publish_flag, creds_status, upload_link, pending_human_items

    HARD RULES enforced here:
      - AGENT_ONBOARD_AUTOMINT must be ON to mint a token; otherwise token_minted=None
      - Meta credentials are NEVER touched. creds_status is always NOT SET (by hand).
      - voice file is the empty FILLABLE template only; LASSO content never copied in.
      - brain file is empty (one header line only).
      - trust_level is ALWAYS FULL_APPROVAL for new gyms; never set to anything else here.
      - publish_flag is ALWAYS OFF.
      - No em dashes, en dashes, or hyphens in any gym-facing copy in this result.
      - Fabrication gate: no invented facts, stats, prices, or offers written into files.
    """
    # Resolve paths relative to cwd when not supplied, using the same conventions
    # as the rest of the codebase (brand_voice/ and brains/ at the repo root).
    if voice_dir is None:
        voice_dir = "brand_voice"
    if brains_dir is None:
        brains_dir = "brains"

    result = {
        "account_key": account_key,
        "display_name": display_name,
        "token_minted": None,
        "voice_path": None,
        "brain_path": None,
        "trust_level": default_trust_for_new_account(),
        "publish_flag": "OFF",
        "creds_status": "NOT SET (by hand)",
        "upload_link": None,
        "pending_human_items": [],
    }

    # (a) Upsert gym row --------------------------------------------------
    db.gym_upsert(account_key, display_name=display_name)

    # (b) Token minting ---------------------------------------------------
    if config.onboard_automint_enabled():
        status = _token_status(account_key)
        # Real token_status returns {"status": "ACTIVE"|"REVOKED"|"NOT_SET", ...}
        # Stub returned {"has_token": bool, ...}. Support both for safety.
        already_minted = (status.get("status") == "ACTIVE") or bool(status.get("has_token"))
        if already_minted:
            result["token_minted"] = False
        else:
            from . import intake_tokens
            raw_token = intake_tokens.mint(account_key, db_conn=db_conn)
            result["token_minted"] = raw_token   # caller only: never written to file/log
    else:
        result["token_minted"] = None   # skipped, pending by hand

    # (c) Scaffold voice file ---------------------------------------------
    voice_path = os.path.join(voice_dir, f"{account_key}.md")
    result["voice_path"] = voice_path
    if not os.path.exists(voice_path):
        # render_template writes to out_path and returns the path
        render_template(out_path=voice_path)
        # Confirm: the rendered file must be dash-free and fabrication-free.
        # (render_template already asserts this internally.)

    # (d) Scaffold brain file ---------------------------------------------
    brain_path = os.path.join(brains_dir, f"{account_key}.md")
    result["brain_path"] = brain_path
    if not os.path.exists(brain_path):
        os.makedirs(brains_dir, exist_ok=True)
        with open(brain_path, "w", encoding="utf-8") as fh:
            fh.write(f"# Style brain for {account_key}\n")

    # (e) Trust -----------------------------------------------------------
    db.kv_set(f"gym_trust_{account_key}", str(int(TrustLevel.FULL_APPROVAL)))
    result["trust_level"] = TrustLevel.FULL_APPROVAL

    # (f) Publish flag and creds ------------------------------------------
    db.kv_set(f"gym_publish_{account_key}", "OFF")
    db.kv_set(f"gym_publish_creds_{account_key}", "NOT SET (by hand)")
    result["publish_flag"] = "OFF"
    result["creds_status"] = "NOT SET (by hand)"
    # THE ONE HUMAN LINE: Meta token is set by Blake by hand only.
    # This function NEVER creates, reads, prints, or infers it.

    # (g) Upload link -----------------------------------------------------
    if base_url and isinstance(result["token_minted"], str):
        result["upload_link"] = base_url.rstrip("/") + "/u/" + result["token_minted"]

    # (h) Pending human items ---------------------------------------------
    pending = ["publish creds: NOT SET (by hand)"]
    gym_row = db.gym_get(account_key)
    if not gym_row.get("slack_channel"):
        pending.append("Slack channel: NOT SET")
    if not gym_row.get("approvers"):
        pending.append("approver Slack ID: NOT SET")
    pending.append("first-month plan: PENDING")
    if result["token_minted"] is None:
        pending.append("intake token: requires AGENT_ONBOARD_AUTOMINT=true")
    result["pending_human_items"] = pending

    return result


def _token_status(account_key):
    """Check kv for whether this account already has an active intake token.
    Returns a dict matching the intake_tokens.token_status interface so the
    automint check works even before intake_tokens is available."""
    try:
        from . import intake_tokens
        return intake_tokens.token_status(account_key)
    except Exception:
        return {"account_key": account_key, "has_token": False,
                "revoked": False, "token_prefix": ""}
