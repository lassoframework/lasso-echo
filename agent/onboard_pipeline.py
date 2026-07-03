"""
Stage 3 one-command client onboarding. RUN BY HAND:

    /opt/venv/bin/python -m agent onboard-client --intake <file> --key <k> [--name <n>]

Takes a COMPLETED intake (knowledge/intake_template.md structure, sections 1 to
9) and produces the full local scaffold:
  - the DRAFT brand bible + social proof via the existing draft-bible path
    (brand_voice/drafts/<key>/, never auto-activated)
  - the Account config entry + brand_voice/<key>/ templates via add-client
  - content_library/<key>/ with a consent-guard README
  - the welcome kit PDF (existing generator path)
  - the printed GO LIVE checklist: exactly the by-hand steps remaining
    (secrets, the connect link, the first approval)

MISSING OR EMPTY INTAKE FIELDS BLOCK with the list, never guessed. Touches no
env, creates no tokens, arms nothing.
"""

import os

from .bible_drafter import parse_intake

REQUIRED_SECTIONS = {
    1: "Who you are (business + offers)",
    2: "Who you talk to (avatar)",
    3: "Voice and tone",
    4: "Hard guardrails and locked claims",
    5: "Content pillars",
    6: "Social proof",
    7: "CTAs, links, and hashtags",
    8: "Posting preferences",
    9: "Consent policy",
}

CONSENT_README = """# {name} content library

Drop approved photos and clips here (or let the intake link file them).

CONSENT GUARD: when AGENT_CONSENT_GUARD_ENABLED is armed, an asset showing
people is selectable ONLY with consent recorded as granted in its sidecar.
Unknown consent means excluded, by design. Record consent per the client's
consent policy from their intake (section 9).
"""

GO_LIVE = """GO LIVE CHECKLIST for {key} (exactly what remains, all by hand):
 1. Review brand_voice/drafts/{key}/ and fill every TODO; activate by copying
    the reviewed files onto the paths in the Account entry above.
 2. Paste the Account entry into agent/accounts.py and commit.
 3. SECRETS by hand in Railway env under the names on the entry (never in git).
    Verify: python -m agent check-tokens
 4. Or send the CONNECT LINK instead: https://<listener-domain>/connect
    (AGENT_CONNECT_ENABLED + AGENT_CONNECT_TOKENS_ENABLED when you choose kv
    tokens; env tokens always win if both exist).
 5. Slack: create the approval channel, invite Echo + approver, ids on the entry.
 6. python -m agent capture-baseline BEFORE any Echo post.
 7. FIRST APPROVAL: the first post to this audience is never automated. You (a
    human) review and tap the first card; trust stays level 0 until you decide
    otherwise, per account, by hand.
"""


def missing_fields(intake_text):
    """The list of REQUIRED sections that are absent, empty, or still TODO."""
    sections = parse_intake(intake_text)
    missing = []
    for n, label in REQUIRED_SECTIONS.items():
        body = (sections.get(n) or "").strip()
        if not body or "TODO" in body.upper().split():
            missing.append(f"{n}. {label}")
    return missing


def onboard(intake_path, key, name=None, root="."):
    """The pipeline. Returns a summary dict, or None when blocked/invalid."""
    from . import onboard as onboard_scaffold
    from .bible_drafter import run as draft_bible_run
    from .pdf_report import build_pdf
    from .welcome_kit import SECTIONS as KIT_SECTIONS

    if not onboard_scaffold.valid_key(key):
        print(f"onboard-client: invalid key {key!r}")
        return None
    try:
        with open(intake_path, encoding="utf-8") as fh:
            intake_text = fh.read()
    except OSError as e:
        print(f"onboard-client: cannot read intake: {e}")
        return None

    missing = missing_fields(intake_text)
    if missing:
        print("onboard-client: BLOCKED. The intake is incomplete; fill these "
              "sections (nothing is ever guessed):")
        for m in missing:
            print(f"  - {m}")
        return None

    name = (name or key.replace("_", " ").title()).strip()

    # 1. the draft bible via the existing path (never auto-activated)
    bible_path, proof_path = draft_bible_run(key, intake_path)

    # 2. account entry + voice/proof templates + library folder
    scaffold = onboard_scaffold.add_client(key, name, root=root)

    # 3. consent-guard README in the client library
    readme = os.path.join(root, "content_library", key, "README.md")
    if not os.path.exists(readme):
        with open(readme, "w", encoding="utf-8") as fh:
            fh.write(CONSENT_README.format(name=name))

    # 4. the welcome kit PDF (existing generator sections + brand name)
    kit_dir = os.path.join(root, "brand_voice", "drafts", key)
    os.makedirs(kit_dir, exist_ok=True)
    kit_pdf = os.path.join(kit_dir, "welcome_kit.pdf")
    build_pdf(kit_pdf, "Welcome", "How your done for you social works",
              KIT_SECTIONS, brand={"name": name, "logo": None})

    # 5. the go live checklist
    print(GO_LIVE.format(key=key))
    print(f"onboard-client: scaffold complete for {key}. Drafts: {bible_path}, "
          f"{proof_path}. Welcome kit: {kit_pdf}. Nothing armed.")
    return {"bible": bible_path, "proof": proof_path, "kit": kit_pdf,
            "scaffold": scaffold}
