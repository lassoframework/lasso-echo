"""
DAM v1: consent tracking, perceptual near-dupe collapse, auto-tag.
Flags: AGENT_CONSENT_GUARD_ENABLED, AGENT_AUTOTAG_ENABLED (both default OFF).

CONSENT GUARD (fail safe, absolute): with the flag ON, an asset may only be
selected when its sidecar says people=false, OR people=true AND
consent="granted". Missing people flag, missing consent, or consent anything
other than "granted" EXCLUDES the asset; the card path can never see it. The
people flag is set by the auto-tag pass or by hand in the sidecar. NOTE: arming
the guard on an untagged library excludes everything until assets are tagged;
that is the fail safe working, not a bug.

NEAR-DUPE COLLAPSE: dam-scan computes a perceptual hash per image (alongside
the sha256 exact dedupe ingest already does) and writes a shared dupe_group
into every member's sidecar. Rotation keys on the group, so near-identical
creatives count as ONE creative inside the no-repeat window.

AUTO-TAG: one lowest-cost Gemini vision call per new asset writes tags, a
people flag, and a one-line description into the sidecar; low confidence marks
review=true for the human queue. Counts against the daily Gemini spend cap.
"""

import io
import json
import os

from . import config, db, ops_alerts

REVIEW_CONFIDENCE = 0.7
_TAG_PROMPT = (
    "Look at this image and reply with ONLY a JSON object, no other text: "
    '{"tags": [up to 5 short lowercase tags], "people": true or false (are any '
    'human faces or people visible?), "description": one short sentence, '
    '"confidence": 0.0 to 1.0}')


# ---- sidecars ---------------------------------------------------------------------
def sidecar_path(creative_path):
    return os.path.splitext(creative_path)[0] + ".json"


def read_sidecar(creative_path):
    try:
        with open(sidecar_path(creative_path), encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def write_sidecar(creative_path, updates):
    """Merge-write: existing fields (note, public_url, archetype...) survive."""
    data = read_sidecar(creative_path)
    data.update(updates)
    with open(sidecar_path(creative_path), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return data


# ---- consent guard ------------------------------------------------------------------
def consent_blocked(creative_path):
    """
    True when the consent guard must EXCLUDE this asset. Flag OFF: never blocks.
    Flag ON, fail safe: only people=false, or people=true with consent="granted",
    may pass. Unknown people, unknown consent, denied consent: excluded.
    """
    if not config.consent_guard_enabled():
        return False
    side = read_sidecar(creative_path)
    people = side.get("people", None)
    if people is False:
        return False
    if people is True:
        return str(side.get("consent", "")).lower() != "granted"
    return True  # unknown = excluded while the guard is armed


# ---- near-dupe collapse ---------------------------------------------------------------
def _phash_default(data):
    """8x8 average hash (pillow lazy); None when unreadable / not an image."""
    try:
        from PIL import Image  # lazy
        img = Image.open(io.BytesIO(data)).convert("L").resize((8, 8))
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels)
        return "".join("1" if p > avg else "0" for p in pixels)
    except Exception:
        return None


def rotation_key(creative_path):
    """The no-repeat key rotation uses: the near-dupe group when marked, else the
    filename. Serving one member of a group blocks the whole group."""
    group = read_sidecar(creative_path).get("dupe_group", "")
    return group or os.path.basename(creative_path)


def mark_near_dupes(library_path, phash=None):
    """Scan the library, group images by perceptual hash, write dupe_group into
    every member of each multi-asset group. Returns {group_key: [members]}."""
    phash = phash or _phash_default
    by_hash = {}
    for name in sorted(os.listdir(library_path) if os.path.isdir(library_path) else []):
        if os.path.splitext(name)[1].lower() not in (".jpg", ".jpeg", ".png", ".webp"):
            continue
        path = os.path.join(library_path, name)
        try:
            with open(path, "rb") as fh:
                h = phash(fh.read())
        except OSError:
            continue
        if h:
            by_hash.setdefault(h, []).append(name)
    groups = {}
    for members in by_hash.values():
        if len(members) < 2:
            continue
        leader = members[0]
        groups[leader] = members
        for m in members:
            write_sidecar(os.path.join(library_path, m), {"dupe_group": leader})
    return groups


# ---- auto-tag ----------------------------------------------------------------------------
def _default_reader():
    """Gemini vision (lazy). None when the studio is unarmed or keyless."""
    if not config.creative_studio_enabled():
        return None
    key = os.environ.get("AGENT_NANO_API_KEY")
    if not key:
        return None
    from google import genai  # lazy
    from google.genai import types as gtypes
    client = genai.Client(api_key=key)

    def _read(image_bytes):
        resp = client.models.generate_content(
            model=config.NANO_MODEL,
            contents=[gtypes.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                      _TAG_PROMPT])
        return getattr(resp, "text", "") or ""

    return _read


def _spend_allowed(day):
    """Shares the Gemini spend gate with creative_studio (global bucket:
    DAM autotag is maintenance work, not client-driven generation)."""
    from .creative_studio import spend_allowed
    return spend_allowed(account_key=None, day=day)


def autotag(creative_path, reader=None, day=None):
    """
    Tag one asset: tags + people flag + description into the sidecar; low
    confidence marks review=true. None while AGENT_AUTOTAG_ENABLED is OFF, when
    no reader is available, or past the daily spend cap. Never raises.
    """
    if not config.autotag_enabled():
        return None
    from datetime import date
    day = day or date.today().isoformat()
    if not _spend_allowed(day):
        return None
    reader = reader or _default_reader()
    if reader is None:
        return None
    try:
        with open(creative_path, "rb") as fh:
            raw = reader(fh.read())
        body = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        confidence = float(body.get("confidence", 0))
        updates = {
            "tags": [str(t).lower() for t in (body.get("tags") or [])][:5],
            "people": bool(body.get("people", True)),  # unsure defaults to True
            "description": str(body.get("description", ""))[:200],
            "tag_confidence": confidence,
        }
        if confidence < REVIEW_CONFIDENCE:
            updates["review"] = True
        return write_sidecar(creative_path, updates)
    except Exception as e:
        print(f"[dam] autotag failed for {os.path.basename(creative_path)}: "
              f"{type(e).__name__}: {e}")
        return None
