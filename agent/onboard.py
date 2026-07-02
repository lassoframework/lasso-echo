"""
Client onboarding CLI (Stage 3 templating). RUN BY HAND:

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
