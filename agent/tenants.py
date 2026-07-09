"""
Tenant scaffold from the intake form (Stage 2 Part 3).

    python -m agent intake-create --payload <intake.json>

Dormant behind AGENT_INTAKE_ENABLED (default OFF: the CLI refuses, nothing is
written). Armed, one completed intake form payload creates one tenant under
brand_voice/<key>/:

    voice.md            the tenant's voice doc (loadable by voice.load_voice)
    avatar.md           who the tenant talks to, verbatim from the intake
    verified_facts.md   USE-line facts file; feeds the fabrication gate via
                        tenant_approved_claims() (rotation.is_gate_clean's
                        approved_claims parameter)
    tenant.json         approver name + phone, sender phone numbers (media
                        routing), chosen media lanes, trust level 0, quotas

MISSING OR EMPTY REQUIRED FIELDS BLOCK LOUD with the full list; nothing is
guessed and nothing partial is written. Facts land as USE lines ONLY when the
intake marks them verified; unverified text never enters the facts file.

TRUST: every new tenant starts at trust level 0 (FULL_APPROVAL), written
explicitly into tenant.json, regardless of what any other account has earned
(the per-account trust law). Nothing here publishes, arms, or touches env.
"""

import json
import os
import re
from datetime import datetime, timezone

from . import config

REQUIRED_FIELDS = (
    "key", "name", "avatar", "voice", "approver", "sender_phones", "media_lanes",
)

# tenant.json quota defaults (Part 9 enforces them; fields live on the tenant)
DEFAULT_STORAGE_QUOTA_MB = 2048
DEFAULT_MONTHLY_RECREATE_BUDGET = 20

_KEY_RE = re.compile(r"^[a-z0-9_]{2,40}$")
_PHONE_RE = re.compile(r"^\+?[0-9]{7,15}$")


def tenants_dir(base_dir=None):
    return base_dir or os.path.join("brand_voice", "tenants")


def tenant_dir(key, base_dir=None):
    return os.path.join(tenants_dir(base_dir), key)


def _clean_phone(raw):
    """Digits + leading plus only; None when it does not look like a phone."""
    s = re.sub(r"[^\d+]", "", str(raw or ""))
    return s if _PHONE_RE.match(s) else None


def validate_payload(payload):
    """The list of blocking problems ([] = clean). Loud, specific, never guessed."""
    problems = []
    if not isinstance(payload, dict):
        return ["payload is not a JSON object"]
    for f in REQUIRED_FIELDS:
        v = payload.get(f)
        if v is None or (isinstance(v, (str, list, dict)) and not v):
            problems.append(f"missing or empty required field: {f}")
    key = str(payload.get("key", ""))
    if key and not _KEY_RE.match(key):
        problems.append(f"key must match [a-z0-9_]{{2,40}}: {key!r}")
    approver = payload.get("approver") or {}
    if isinstance(approver, dict):
        if not str(approver.get("name", "")).strip():
            problems.append("approver.name is required")
        if not _clean_phone(approver.get("phone", "")):
            problems.append("approver.phone is required and must be a phone number")
    else:
        problems.append("approver must be an object with name and phone")
    phones = payload.get("sender_phones") or []
    if isinstance(phones, list):
        for p in phones:
            if not _clean_phone(p):
                problems.append(f"sender phone does not look like a phone number: {p!r}")
    else:
        problems.append("sender_phones must be a list")
    return problems


def _voice_md(payload):
    """The tenant voice doc, from intake fields only (verbatim; nothing invented).
    Shape matches what voice.load_voice reads: free text + a CTA rotation
    section + hashtags."""
    v = payload.get("voice") or {}
    lines = [f"# {payload['name']} voice doc", ""]
    for section in ("tone", "story", "dos", "donts"):
        if v.get(section):
            lines += [f"## {section.title()}", str(v[section]).strip(), ""]
    ctas = v.get("ctas") or []
    if ctas:
        lines += ["### CTA rotation"]
        lines += [f"- {c}" for c in ctas]
        lines += [""]
    tags = v.get("hashtags") or []
    if tags:
        lines += ["## Hashtags", " ".join(tags), ""]
    return "\n".join(lines)


def _facts_md(payload):
    """The verified facts file: USE lines ONLY for facts the intake marked
    verified. Unverified text is listed as PENDING (never postable, the gate
    ignores it)."""
    lines = [f"# {payload['name']} verified facts (approved source)",
             "Rule: only lines marked USE are postable. Wording must match exactly.",
             ""]
    verified = [f for f in (payload.get("verified_facts") or []) if str(f).strip()]
    pending = [f for f in (payload.get("unverified_facts") or []) if str(f).strip()]
    if verified:
        lines += ["## USE (client verified at intake)"]
        lines += [f'- USE: "{str(f).strip()}"' for f in verified]
        lines += [""]
    if pending:
        lines += ["## PENDING (not postable until the client verifies)"]
        lines += [f"- PENDING: {str(f).strip()}" for f in pending]
        lines += [""]
    return "\n".join(lines)


def intake_create(payload, base_dir=None):
    """
    Create one tenant from an intake payload. Returns a summary dict, or
    {"blocked": [...]} listing every problem, or None while the flag is OFF.
    All-or-nothing: a blocked intake writes NOTHING.
    """
    if not config.intake_enabled():
        return None
    problems = validate_payload(payload)
    if problems:
        return {"blocked": problems}
    key = payload["key"]
    tdir = tenant_dir(key, base_dir)
    if os.path.isdir(tdir) and os.listdir(tdir):
        return {"blocked": [f"tenant {key} already exists at {tdir}; refusing to "
                            "overwrite (delete by hand first)"]}
    os.makedirs(tdir, exist_ok=True)

    approver = payload["approver"]
    record = {
        "key": key,
        "name": payload["name"],
        "created": datetime.now(timezone.utc).isoformat(),
        "approver_name": str(approver["name"]).strip(),
        "approver_phone": _clean_phone(approver["phone"]),
        "sender_phones": [_clean_phone(p) for p in payload["sender_phones"]],
        "media_lanes": list(payload["media_lanes"]),
        # every new tenant starts at FULL approval, per-account, non-negotiable
        "trust": 0,
        # Part 9 quota fields (enforced at upload time)
        "storage_quota_mb": int(payload.get("storage_quota_mb",
                                            DEFAULT_STORAGE_QUOTA_MB)),
        "monthly_recreate_budget": int(payload.get("monthly_recreate_budget",
                                                   DEFAULT_MONTHLY_RECREATE_BUDGET)),
    }
    with open(os.path.join(tdir, "voice.md"), "w", encoding="utf-8") as fh:
        fh.write(_voice_md(payload))
    with open(os.path.join(tdir, "avatar.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# {payload['name']} avatar\n\n{str(payload['avatar']).strip()}\n")
    with open(os.path.join(tdir, "verified_facts.md"), "w", encoding="utf-8") as fh:
        fh.write(_facts_md(payload))
    with open(os.path.join(tdir, "tenant.json"), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)

    from . import db
    db.audit("tenant_created", key,
             f"tenant scaffold written to {tdir} (trust 0, "
             f"{len(record['sender_phones'])} sender phone(s), "
             f"lanes: {', '.join(record['media_lanes'])})")
    return {"key": key, "dir": tdir,
            "files": sorted(os.listdir(tdir)), "trust": 0}


# ---- reads the rest of Stage 2 shares -----------------------------------------------------
def load_tenant(key, base_dir=None):
    """The tenant.json record, or None."""
    try:
        with open(os.path.join(tenant_dir(key, base_dir), "tenant.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def list_tenants(base_dir=None):
    root = tenants_dir(base_dir)
    if not os.path.isdir(root):
        return []
    return sorted(k for k in os.listdir(root)
                  if os.path.isfile(os.path.join(root, k, "tenant.json")))


def tenant_for_sender(phone, base_dir=None):
    """The tenant key a sender phone belongs to, or None (the media inbox NEVER
    guesses: an unresolved phone is held, not routed)."""
    p = _clean_phone(phone)
    if not p:
        return None
    for key in list_tenants(base_dir):
        rec = load_tenant(key, base_dir) or {}
        if p in (rec.get("sender_phones") or []):
            return key
    return None


_USE_RE = re.compile(r'^\s*-\s*USE:\s*"(.+)"\s*$')


def tenant_approved_claims(key, base_dir=None):
    """
    The tenant's verified USE facts, verbatim: the approved_claims input to
    rotation.is_gate_clean, so a tenant note citing a verified intake fact
    clears the fabrication gate and an uncited stat still blocks. PENDING
    lines never appear here.
    """
    path = os.path.join(tenant_dir(key, base_dir), "verified_facts.md")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []
    return [m.group(1) for line in text.splitlines()
            if (m := _USE_RE.match(line))]


def intake_create_cli(payload_path):
    """python -m agent intake-create --payload <intake.json>."""
    if not config.intake_enabled():
        print("intake-create: OFF (set AGENT_INTAKE_ENABLED=true). Nothing written.")
        return
    if not payload_path:
        print("usage: python -m agent intake-create --payload <intake.json>")
        return
    try:
        with open(payload_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as e:
        print(f"intake-create: cannot read payload: {type(e).__name__}: {e}")
        return
    out = intake_create(payload)
    if out is None:
        print("intake-create: OFF. Nothing written.")
        return
    if out.get("blocked"):
        print("intake-create: BLOCKED, nothing written. Fix these and re-run:")
        for p in out["blocked"]:
            print(f"  - {p}")
        return
    print(f"intake-create: tenant {out['key']} scaffolded at {out['dir']}")
    for f in out["files"]:
        print(f"  {f}")
    print("  trust level 0 (full approval): every post cards for the tap.")
    print("  Secrets and tokens stay by-hand env steps; nothing was armed.")
