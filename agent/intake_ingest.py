"""
Texted-link intake: the processing half, INSIDE the existing listener loop (the
one process that has both /data and R2).

Per pass, for each client with objects under intake/<client>/incoming/:
  1. quarantine zero-byte uploads to deadletter/ with a specific ops alert,
  2. dedupe the RAW bytes by SHA-256 (the same file uploaded twice lands once,
     no matter what the converter does with it),
  3. convert HEIC to JPG (EXIF orientation normalized) and MOV to MP4 (ffmpeg
     stream-copy remux when ffmpeg is available, unchanged pass-through when
     not); every conversion archives the ORIGINAL to intake/<client>/originals/
     before the incoming object is deleted, so no conversion loses a file; a
     conversion failure dead-letters the file, it never crashes the loop,
  4. dedupe the converted bytes by SHA-256 against everything already
     accepted (an exact converted-byte duplicate drops the incoming copy only
     AFTER its raw source is archived to originals/, so no source is ever
     lost); a perceptual-hash near-duplicate is NEVER deleted — it is held
     under intake/<client>/hold/ with its source bytes preserved for a human
     decision (a pHash is similarity, never identity),
  5. run the moderation hook (a stub interface today: moderate(data, name) ->
     (ok, reason); anything flagged moves to intake/<client>/review/ and posts one
     Slack notice line),
  6. file accepted media into the client's content library prefix with the
     client's sentence saved as the caption note file the drafter already reads.

Idempotent via a processed manifest stored in R2 (intake/<client>/manifest.json);
a re-run of an already-processed batch is a no-op. Any per-file failure goes to
intake/<client>/deadletter/ with ONE ops alert and processing continues.

Same flag as the upload page: AGENT_INTAKE_ENABLED, default OFF (dormant).
"""

import hashlib
from contextvars import ContextVar
import io
import json
import os
from pathlib import Path

from . import config, local_inventory_mutation as _mutation, ops_alerts, visual_fingerprint
from .accounts import get_account

MANIFEST = "manifest.json"
_active_mutation = ContextVar("intake_inventory_mutation", default=None)


def _after_disposition(callback):
    active = _active_mutation.get()
    if active is None:
        return callback()
    active["followups"].append(callback)


def _ops_alert(*args, **kwargs):
    return _after_disposition(lambda: ops_alerts.alert(*args, **kwargs))


def _post_notice(poster, text):
    return _after_disposition(lambda: poster.post_notice(text))


# ---- inventory mutation receipt guard ------------------------------------------
# When armed, one receipt fences the entire client media disposition. It loads
# the manifest under the canonical gym flock, writes/verifies retained media and
# provenance, saves/verifies the manifest, and only then consumes incoming copies.
# Any uncertain effect holds the entire receipt for reconciliation. The default
# OFF path retains legacy handling, with source retention corrected for same-name
# conversions. Form landing needs a connection-aware adapter and explicitly holds
# in armed mode. Notifications and DAM/draft follow-ups run after COMPLETE.
def _mutation_config(client, lib_dir):
    if not _mutation.enabled():
        return None
    root = Path(lib_dir).absolute()
    root.mkdir(parents=True, exist_ok=True)
    return _mutation.configured(client, root)


def _guarded(client, kind, request_payload, apply, lib_dir):
    """Run apply(conn) under the mutation receipt protocol when armed, else directly."""
    active = _active_mutation.get()
    if active is not None:
        if active["cfg"].gym_id != client:
            raise _mutation.MutationHold("intake_mutation_binding_invalid")
        # The whole client disposition already owns the canonical lock. Nested
        # effects belong to that receipt and must never reacquire its flock.
        try:
            return apply(active["conn"])
        except Exception:
            # A failed effect may already have changed durable state. Never let
            # the per-file bad-media handler reclassify it as safe to consume.
            raise _mutation.MutationHold("intake_disposition_pending") from None
    cfg = _mutation_config(client, lib_dir)
    if cfg is None:
        return apply(None)
    authority = _mutation.MutationAuthority.from_environment()
    def _apply(conn):
        token = _active_mutation.set({"cfg": cfg, "conn": conn, "followups": []})
        try:
            return apply(conn)
        finally:
            _active_mutation.reset(token)
    try:
        return _mutation.run(cfg, authority, kind, request_payload, _apply)
    finally:
        authority.close()


# ---- default media transforms (lazy imports; injectable for tests) -------------
def _remux_mov(data, name, runner=None, which=None):
    """MOV -> MP4 container remux via ffmpeg (stream copy: lossless, cheap).
    Returns (bytes, new_name) or None when ffmpeg is unavailable or the remux
    fails — the caller then passes the MOV through unchanged (IG accepts MOV;
    a playable original always beats a failed conversion)."""
    import shutil
    import subprocess
    import tempfile
    which = which or shutil.which
    runner = runner or subprocess.run
    if which("ffmpeg") is None:
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, name)
            dst = os.path.join(td, os.path.splitext(name)[0] + ".mp4")
            with open(src, "wb") as fh:
                fh.write(data)
            runner(["ffmpeg", "-y", "-i", src, "-c", "copy", dst],
                   check=True, capture_output=True, timeout=120)
            with open(dst, "rb") as fh:
                return fh.read(), os.path.basename(dst)
    except Exception:
        return None


def _decode_image(data, name):
    """Image.open + FULL decode, salvaging nearly-complete files. A JPEG missing
    its final few bytes (interrupted mobile-Safari/multipart upload) makes PIL
    raise OSError 'image file is truncated (N bytes not processed)' even though
    the picture is 99% intact and fully usable — and a client photo is precious,
    so we retry ONCE with LOAD_TRUNCATED_IMAGES instead of dead-lettering. The
    flag is Pillow PROCESS-GLOBAL, so it is set/restored tightly around the one
    retry decode, never left on. Truly undecodable bytes (garbage, no parseable
    header) still raise on BOTH attempts, so they still dead-letter upstream.
    The caller re-encodes the salvaged image to a clean JPEG, so everything
    downstream (phash, thumbnail, library, publish) gets a valid file."""
    import re
    from PIL import Image, ImageFile  # lazy
    try:
        img = Image.open(io.BytesIO(data))
        img.load()   # force the full decode HERE, where we can catch truncation
        return img
    except OSError as e:
        # only the specific truncated-tail failure earns a salvage retry;
        # anything else (unidentifiable garbage included) dead-letters as before
        if "truncated" not in str(e):
            raise
        truncated_err = e
    prev = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = prev
    m = re.search(r"\((\d+) bytes not processed\)", str(truncated_err))
    unprocessed = m.group(1) if m else "unknown"
    print(f"[intake-ingest] salvaged truncated image {name} "
          f"({unprocessed} bytes unprocessed)")
    return img


def _convert_default(data, name):
    """(new_bytes, new_name): HEIC/HEIF -> JPG (orientation normalized);
    MOV -> MP4 (ffmpeg remux when available, else unchanged); MP4 passes
    through. The ORIGINAL bytes are archived by the pipeline whenever the
    bytes or name change, so no conversion ever loses the source file."""
    lower = name.lower()
    if lower.endswith(".mp4"):
        return data, name
    if lower.endswith(".mov"):
        remuxed = _remux_mov(data, name)
        return remuxed if remuxed is not None else (data, name)
    from PIL import ImageOps  # lazy
    if lower.endswith((".heic", ".heif")):
        import pillow_heif  # lazy
        pillow_heif.register_heif_opener()
    img = _decode_image(data, name)
    img = ImageOps.exif_transpose(img)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=92)
    stem = os.path.splitext(name)[0]
    return out.getvalue(), f"{stem}.jpg"


def _phash_default(data, name):
    """8x8 average hash for near-duplicate detection; None for video/unreadable."""
    if name.lower().endswith((".mp4", ".mov")):
        return None
    try:
        from PIL import Image  # lazy
        img = Image.open(io.BytesIO(data)).convert("L").resize((8, 8))
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels)
        return "".join("1" if p > avg else "0" for p in pixels)
    except Exception:
        return None


_MODERATION_PROMPT = (
    "Review this image for content moderation. Reply ONLY with a JSON object, no "
    'other text: {"safe": true or false, "reason": "" (empty when safe, else one '
    'short phrase such as "nudity", "violence", "explicit_text", or "other"), '
    '"confidence": 0.0 to 1.0}')


def _gemini_moderate(data):
    """Gemini Vision moderation call. Returns (safe: bool, reason: str) or raises."""
    import json as _json
    from google import genai
    from google.genai import types as gtypes
    key = os.environ.get(config.NANO_API_KEY_ENV, "")
    if not key:
        return True, ""
    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model=config.OCR_MODEL,
        contents=[gtypes.Part.from_bytes(data=data, mime_type="image/jpeg"),
                  _MODERATION_PROMPT])
    raw = getattr(resp, "text", "") or ""
    body = _json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    return bool(body.get("safe", True)), str(body.get("reason", ""))


def _moderate_default(data, name):
    """Passes while AGENT_CONTENT_MODERATION_ENABLED is OFF. When ON, calls
    Gemini Vision; fails open on any error so uploads never stall permanently."""
    if not config.content_moderation_enabled():
        return True, ""
    if os.path.splitext(name)[1].lower() in (".mp4", ".mov", ".avi"):
        return True, ""  # video moderation out of scope for the current pass
    try:
        return _gemini_moderate(data)
    except Exception:
        return True, ""


def _make_thumbnail(data, name, max_px=400):
    """Returns (thumb_bytes, thumb_name) or None if Pillow is unavailable or the
    image format is not supported. Resizes to max_px on the longest side, converts
    to JPEG, strips EXIF. Never raises."""
    try:
        from PIL import Image, ImageOps  # lazy
        stem = os.path.splitext(name)[0]
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        w, h = img.size
        if w == 0 or h == 0:
            return None
        scale = max_px / max(w, h)
        if scale < 1.0:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                             Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue(), f"{stem}_thumb.jpg"
    except Exception:
        return None


# ---- manifest -------------------------------------------------------------------
def _object_absent(exc, key):
    """Only an explicit missing-object response establishes absence."""
    if isinstance(exc, KeyError):
        return exc.args == (key,)  # the requested key in an in-memory store
    if isinstance(exc, FileNotFoundError):
        return True  # local object store
    response = getattr(exc, "response", None)
    return (isinstance(response, dict)
            and response.get("Error", {}).get("Code") in
            ("NoSuchKey", "NotFound", "404"))


def _load_manifest(r2, client):
    manifest_key = f"intake/{client}/{MANIFEST}"
    try:
        raw = r2.get_bytes(manifest_key)
        manifest = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        if _active_mutation.get() is not None and not _object_absent(exc, manifest_key):
            raise _mutation.MutationHold("intake_manifest_unavailable") from None
        manifest = {"processed": [], "sha256": [], "phash": []}
    if _active_mutation.get() is not None:
        if (not isinstance(manifest, dict)
                or any(not isinstance(manifest.get(k, []), list)
                       or any(not isinstance(v, str) for v in manifest.get(k, []))
                       for k in ("processed", "sha256", "phash", "sha256_raw",
                                 "source_fingerprints"))
                or any(not isinstance(manifest.get(k, {}), dict)
                       for k in ("source_fingerprint_aliases", "asset_provenance"))):
            raise _mutation.MutationHold("intake_manifest_invalid")
        for k in ("processed", "sha256", "phash"):
            manifest.setdefault(k, [])
        if (any(not isinstance(k, str) or not isinstance(v, list)
                or any(not isinstance(alias, str) for alias in v)
                for k, v in manifest.get("source_fingerprint_aliases", {}).items())
                or any(not isinstance(k, str) or not isinstance(v, dict)
                       for k, v in manifest.get("asset_provenance", {}).items())):
            raise _mutation.MutationHold("intake_manifest_invalid")
        try:
            _mutation.canonical_json(manifest)
        except (ValueError, TypeError):
            raise _mutation.MutationHold("intake_manifest_invalid") from None
    # additive key for raw-bytes dedupe; old manifests gain it on first touch
    manifest.setdefault("sha256_raw", [])
    # Strong source identities are authoritative. Drive-compatible MD5 values
    # remain explicitly typed aliases; never silently promote them to identity.
    manifest.setdefault("source_fingerprints", [])
    manifest.setdefault("source_fingerprint_aliases", {})
    # One durable binding per incoming object. This maps the source identity to
    # its converted representation and current filename/key; asset_id remains
    # nullable until a later indexer assigns one.
    manifest.setdefault("asset_provenance", {})
    return manifest


def _save_manifest(r2, client, manifest):
    r2.put_bytes(f"intake/{client}/{MANIFEST}",
                 json.dumps(manifest).encode("utf-8"),
                 content_type="application/json")


def _record_source_identity(manifest, fingerprint, aliases):
    """Record one authoritative source identity and its explicit aliases."""
    if fingerprint not in manifest["source_fingerprints"]:
        manifest["source_fingerprints"].append(fingerprint)
    normalized = []
    for alias in aliases or []:
        value = visual_fingerprint.normalize_any(alias)
        if value and value != fingerprint:
            normalized.append(value)
    if normalized:
        current = manifest["source_fingerprint_aliases"].setdefault(fingerprint, [])
        current.extend(a for a in normalized if a not in current)


def _record_asset_provenance(manifest, *, original_key, current_key, filename,
                             status, source_fingerprint, source_aliases,
                             converted_bytes, similarity_alias=None):
    """Persist the source-to-representation binding for one intake object."""
    _record_source_identity(manifest, source_fingerprint, source_aliases)
    converted = visual_fingerprint.derived_aliases(converted_bytes)
    record = {
        "status": status,
        "original_key": original_key,
        "current_key": current_key,
        "filename": filename,
        "asset_id": None,
        "source_fingerprint": source_fingerprint,
        "source_fingerprint_aliases": list(source_aliases or []),
        "converted_fingerprint": converted[0],
        "converted_fingerprint_aliases": converted[1:],
    }
    if similarity_alias:
        record["similarity_alias"] = similarity_alias
    manifest["asset_provenance"][original_key] = record
    return dict(record)


def _library_dir_for(client):
    """The client's content library prefix: the account's own library when
    configured (multi-client), else a per-client folder under the global library."""
    acct = get_account(client)
    if acct is not None and getattr(acct, "library_prefix", ""):
        return acct.library_prefix
    return os.path.join(config.LIBRARY_PATH, client)


def _clients_with_incoming(r2):
    clients = set()
    for key in r2.list_keys("intake/"):
        parts = key.split("/")
        if len(parts) >= 4 and parts[2] == "incoming" and parts[3]:
            clients.add(parts[1])
    return sorted(clients)


def process_all(r2=None, poster=None, converter=None, phash=None, moderator=None):
    """
    One ingest pass over every client. Returns {client: {"accepted": n, ...}} or
    None while the flag is OFF. Never raises for a single bad file.
    """
    if not config.intake_enabled():
        return None
    r2 = r2 or _default_r2()
    if r2 is None:
        return {}
    converter = converter or _convert_default
    phash = phash or _phash_default
    moderator = moderator or _moderate_default

    results = {}
    for client in _clients_with_incoming(r2):
        # PER-CLIENT ISOLATION: one gym's R2 list/read failure (or any unhandled
        # error inside its pass) must NEVER abort ingest for every other gym. A gym
        # that blows up is recorded as an error result + a loud ops alert, and the
        # loop moves on to the next gym.
        try:
            results[client] = _process_client(
                client, r2, poster, converter, phash, moderator)
        except Exception as e:  # noqa: BLE001 - one gym never sinks the whole pass
            results[client] = {"error": f"{type(e).__name__}: {e}"}
            ops_alerts.alert(
                f"intake ingest ABORTED for {client} (other gyms unaffected): "
                f"{type(e).__name__}: {e}")
    return results


# Intake FORM sections that become PENDING sources, mapped to their client
# source category. Everything else in the payload (voice, audience, media notes,
# gym basics) is BIBLE material, kept in the archived form for draft-bible.
_FORM_SOURCE_SECTIONS = (
    ("offers", "offer", "intake form"),
    ("pricing_rule", "offer", "intake form pricing rule, exact wording"),
    ("services", "service", "intake form"),
    ("proof", "testimonial", "intake form"),
    ("about", "about", "intake form"),
)


def sections_from_flat(answers):
    """The flat texted-link answers dict, reshaped into the 7-SECTION structure
    normalize_portal_intake reads, so the flat lane can draft a brand bible too.

    WHY: write_brand_docs needs section-shaped input, and the flat lane has none, so a
    gym arriving through the texted-link door got NO voice doc at all and the drafter
    then had nothing to ground captions in. Swift River CrossFit, 2026-08-31: 31
    approved sources and no bible, because its payload was flat. Blake's ruling that
    day: Echo drafts the voice doc and the brain from the gym's own intake, and does
    not hand a human a TODO list.

    Pure mapping, one field to one field. It NEVER invents a value: a field the gym did
    not answer stays absent, so the bible is built only from the gym's own words.
    """
    from . import intake_web  # lazy: avoids an import cycle with the web module
    a = {k: (str(answers.get(k) or "")).strip() for k in intake_web.FORM_FIELDS}

    def _sec(**kw):
        return {k: v for k, v in kw.items() if v}

    out = {
        "gym": _sec(name=a.get("gym_name"), city=a.get("city"),
                    website=a.get("website"), about=a.get("about")),
        "voice": _sec(vibe=a.get("voice")),
        "offers": _sec(front_door_offer=a.get("offers"), services=a.get("services"),
                       exact_pricing_wording=a.get("pricing_rule")),
        "audience": _sec(ideal_member=a.get("audience")),
        "proof": _sec(verifiable_numbers=a.get("proof")),
        "media": _sec(notes=a.get("media_notes")),
        "approver": _sec(name=a.get("approver_name"),
                         contact=a.get("approver_contact")),
    }
    return {k: v for k, v in out.items() if v}


#: The nested sections normalize_portal_intake reads. A payload is bible-drafting material
#: only when at least one of these is a dict; the flat texted-link answers dict has none.
_SECTION_KEYS = ("gym", "voice", "offers", "audience", "proof", "media", "approver")


def _is_section_shaped(payload):
    """True when `payload` is the portal's nested 7 section body, the ONLY shape
    social_intake_reader.map_answers can parse. Guards against feeding it the flat
    answers dict, which raises rather than degrading."""
    if not isinstance(payload, dict) or not payload:
        return False
    return any(isinstance(payload.get(k), dict) for k in _SECTION_KEYS)


def _land_intake_form(client, payload, r2, key, manifest, lib_dir=None):
    """Route one submitted intake form through the client-sources path: fact
    sections land as PENDING sources (never auto approved, deduped so a second
    submission adds nothing twice); the approver + gym basics are held as an
    account proposal (kv + audit, applied by a human only); the full payload is
    archived to intake/<client>/forms/ for the bible draft."""
    from . import client_sources, db
    answers = payload.get("answers") or {}

    bundle, existing = {}, {(s.category, s.text)
                            for s in client_sources.all_sources(client)}
    for field, category, citation in _FORM_SOURCE_SECTIONS:
        for line in (answers.get(field) or "").splitlines():
            fact = line.strip().lstrip("-*").strip()
            if fact and (category, fact) not in existing:
                existing.add((category, fact))
                bundle.setdefault(category, []).append((fact, citation))
    created = client_sources.submit_intake(
        client, bundle, status=client_sources.intake_status()) \
        if bundle else []

    # HELD proposal, never applied live: overwrites in place, so a re-submission
    # UPDATES the pending proposal rather than stacking a second one.
    proposal = {k: (answers.get(k) or "").strip()
                for k in ("gym_name", "city", "website", "ig_handle", "fb_page",
                          "google_business", "approver_name", "approver_contact")}
    registered = False
    if any(proposal.values()):
        db.kv_set(f"account_proposal_{client}", json.dumps(
            {**proposal, "timestamp": payload.get("timestamp", "")}))
        db.audit("account_proposal", client,
                 "intake form proposal held (gym basics + approver)", client)
        # APPLY IT, do not just hold it. Holding meant a gym that had done everything
        # asked of it sat in NEITHER lane until someone hand-applied the proposal, and
        # the alert below told a human to go do that. Swift River CrossFit, 2026-08-31:
        # 31 sources landed and auto-approved, the proposal carried its real name, IG
        # handle and Facebook page, and it still could not draft because nobody had
        # applied it. Everything needed is right here, so use it. The kv proposal is
        # still written as the record of what the gym actually said.
        # Behind AGENT_ONBOARDING_AUTOREGISTER (default OFF), the same flag that lets
        # the readiness watch register a portal-known gym: this is that capability
        # reached through the intake-form door, which the portal roster never sees.
        # Registration creates an INACTIVE Account record only: no tokens, no
        # connection, no approval, no publish. A blank gym name registers nothing,
        # because a fabricated name becomes the account label.
        if config.onboarding_autoregister_enabled() and proposal.get("gym_name"):
            try:
                from . import accounts as _accounts
                # Best-effort gym_id so register_gym's write-path dedup guard has a
                # signal on this door too (see accounts.register_gym docstring / the
                # Sunnyside-Swift River split-key class). A miss registers with no
                # gym_id, exactly like today -- never blocks the landing.
                gid = None
                try:
                    from .portal_calendar_store import SupabaseCalendarStore
                    _store = SupabaseCalendarStore()
                    if _store.available():
                        gid = _store.resolve_gym_uuid(client)
                except Exception:  # noqa: BLE001
                    gid = None
                # own_submission: this form landed under the gym's OWN signed upload
                # token, so `client` IS the submitting gym's key by construction
                # (echo_clients / D73 ruling: an owner's own intake may register the
                # gym before its Echo marker is readable; a fleet sweep may not).
                registered = bool(_accounts.register_gym(
                    client, name=proposal["gym_name"],
                    ig_handle=proposal.get("ig_handle", ""),
                    fb_page=proposal.get("fb_page", ""),
                    gym_id=gid, own_submission=True, door="intake_ingest"))
            except Exception as exc:  # noqa: BLE001 - never fail the intake landing
                ops_alerts.alert(
                    f"{client}: intake landed but auto-register failed "
                    f"({type(exc).__name__}). It is in neither lane until registered.")

    # WRITE THE BRAND BIBLE. This is the lane a healthy intake takes, and it used to only
    # archive the payload "for the bible draft" while nothing ever drafted it: the one
    # automatic writer (onboard_from_social) runs from the unrouted sweeper, whose lister
    # filters echo_forwarded=false. A successful forward sets that true, so a gym got a
    # bible exactly when its delivery FAILED, and every gym that onboarded cleanly had no
    # voice doc — the drafter then produced captions with no avatar, no pillars, no CTAs.
    #
    # Needs the raw 7-SECTION body the portal forwarded (payload["portal"]) — map_answers
    # delegates to normalize_portal_intake, which reads body["gym"], body["voice"] and so on.
    # The texted-link lane (handle_intake_form) archives a FLAT answers dict with no "portal"
    # key, and feeding that in does NOT degrade gracefully: normalize_portal_intake calls
    # .get() on what are plain strings and raises, and even if it did not, every section would
    # come back empty and _write_doc would lay down an unclobberable bible for "the gym" keyed
    # "client" — worse than no bible, because a hollow one looks like a satisfied precondition
    # and blocks the real one forever. So: RESHAPE the flat answers into the sections the
    # mapper accepts (sections_from_flat, a pure one-to-one field mapping that invents
    # nothing) rather than either feeding it a shape it cannot read or leaving the gym
    # with no voice doc at all. Blake's ruling 2026-08-31, after Swift River CrossFit
    # landed 31 approved sources and still had no bible because its payload was flat:
    # Echo drafts the voice doc from the gym's OWN intake; it does not hand a human a
    # TODO list. Still no fabrication: a field the gym did not answer stays absent, and
    # _write_doc never clobbers a bible that already exists.
    sections = payload.get("portal")
    if not _is_section_shaped(sections):
        flat = sections_from_flat(payload.get("answers") or {})
        if _is_section_shaped(flat):
            sections = flat
    if _is_section_shaped(sections):
        try:
            from .social_intake_reader import write_brand_docs
            wrote = write_brand_docs(client, sections)
            if wrote["wrote"]:
                db.audit("brand_bible", client,
                         f"drafted from intake ({wrote['bible_path']})", client)
            # SEED THE BRAIN TOO. It used to start empty and fill only as humans edited
            # the gym's posts, so the first weeks of captions ignored the voice
            # preferences the gym had just written down. Style rules only (vibe, banned
            # words, content goal, hashtags); offers, pricing and proof are facts and
            # stay in client_sources behind the fabrication gate.
            try:
                from . import tenant_brain as _brain
                seeded = _brain.seed_from_intake(client, sections)
                if seeded:
                    db.audit("brain_seed", client,
                             f"{seeded} style rule(s) seeded from intake", client)
            except Exception as exc:  # noqa: BLE001 - the bible still stands
                print(f"[intake-ingest] {client}: brain seed skipped "
                      f"({type(exc).__name__})")
        except Exception as exc:  # noqa: BLE001 - never lose a landed intake over the bible
            ops_alerts.alert(
                f"intake landed for {client} but the brand bible could NOT be written "
                f"({type(exc).__name__}). The gym has sources but no voice doc, so its "
                "captions will have no avatar, pillars or CTAs until this is fixed.")
    else:
        # Not an alert: the sources and proposal DID land, and this lane never produced a
        # bible before either. Loud enough to find, quiet enough not to cry wolf on every
        # texted-link submission.
        print(f"[intake-ingest] {client}: no 7 section payload on this intake, so no brand "
              f"bible was drafted (source lane: {payload.get('source') or 'form'}). "
              "Draft it with `python -m agent draft-bible --from-form`.")

    # archive the FULL payload (voice/audience/media notes included) for the
    # bible draft, then consume the incoming object
    def _archive_form(conn):
        r2.put_bytes(f"intake/{client}/forms/{os.path.basename(key)}",
                     json.dumps(payload).encode("utf-8"),
                     content_type="application/json")
        r2.delete(key)
        return {"archived": os.path.basename(key)}
    if lib_dir is not None:
        _guarded(client, "intake_admit",
                 {"asset": key, "lane": "form", "status": "form_archived"},
                 _archive_form, lib_dir)
    else:
        _archive_form(None)
    manifest["processed"].append(key)
    # TELL THE TRUTH. This line said "pending source(s) to review (approve before they
    # can draft)" no matter what actually happened, so with AGENT_INTAKE_AUTO_APPROVE
    # armed it reported 31 ALREADY-APPROVED sources as needing review and sent a human
    # to do work that was already done. An alert that is wrong in the safe direction
    # still costs exactly as much attention as a real one.
    landed = client_sources.intake_status()
    if landed == "approved":
        state = f"{len(created)} source(s) approved and ready to draft from"
    else:
        state = f"{len(created)} pending source(s) to review before they can draft"
    ops_alerts.alert(
        f"intake form received for {client}: {state}; "
        + ("the gym is registered and in the build lane. "
           if registered else "account proposal held. ")
        + f"Run `python -m agent preflight --account {client}` to see what it still "
          "needs.")
    return len(created)


class _DispositionR2:
    """Read back remote effects; consume incoming sources after manifest commit.

    R2 cannot roll back with SQLite. An uncertain remote write or delete raises
    inside the batch receipt, leaving it pending for explicit reconciliation.
    """
    def __init__(self, r2):
        self.r2 = r2
        self.deletions = {}
        self.sources = {}
        self.retained = {}

    def __getattr__(self, name):
        return getattr(self.r2, name)

    def get_bytes(self, key):
        data = self.r2.get_bytes(key)
        if "/incoming/" in key:
            source_hash = hashlib.sha256(data).hexdigest()
            if key in self.sources and self.sources[key] != source_hash:
                raise _mutation.MutationHold("intake_source_changed")
            self.sources[key] = source_hash
        return data

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        try:
            if "/originals/" in key:
                try:
                    existing = self.r2.get_bytes(key)
                except Exception as exc:
                    if not _object_absent(exc, key):
                        raise _mutation.MutationHold("intake_original_unavailable")
                else:
                    if existing != data:
                        raise _mutation.MutationHold("intake_original_conflict")
                    self.retained[key] = hashlib.sha256(data).hexdigest()
                    return  # immutable, already verified at these exact bytes
            self.r2.put_bytes(key, data, content_type=content_type)
            if self.r2.get_bytes(key) != data:
                raise _mutation.MutationHold("intake_remote_write_unverified")
            self.retained[key] = hashlib.sha256(data).hexdigest()
        except Exception:
            raise _mutation.MutationHold("intake_remote_write_unverified") from None

    def delete(self, key):
        if key not in self.deletions:
            self.get_bytes(key)  # bind deletion to the bytes originally read
            self.deletions[key] = self.sources[key]

    def consume_sources(self):
        # Verify the retained bytes and lineage as one disposition immediately
        # before releasing any source; an earlier PUT receipt alone is weaker.
        for key, retained_hash in self.retained.items():
            try:
                if hashlib.sha256(self.r2.get_bytes(key)).hexdigest() != retained_hash:
                    raise _mutation.MutationHold("intake_retained_bytes_changed")
            except Exception:
                raise _mutation.MutationHold("intake_retained_bytes_unverified") from None
        for key, source_hash in self.deletions.items():
            try:
                if hashlib.sha256(self.r2.get_bytes(key)).hexdigest() != source_hash:
                    raise _mutation.MutationHold("intake_source_changed")
                self.r2.delete(key)
                try:
                    self.r2.get_bytes(key)
                except Exception as exc:
                    if _object_absent(exc, key):
                        continue
                raise _mutation.MutationHold("intake_remote_delete_unverified")
            except Exception:
                raise _mutation.MutationHold("intake_remote_delete_unverified") from None


def _process_client(client, r2, poster, converter, phash, moderator):
    if not _mutation.enabled():
        return _process_client_body(client, r2, poster, converter, phash, moderator)
    lib_dir = _library_dir_for(client)
    followups = []
    held_subpath = []
    def _disposition(conn):
        if any(key.endswith("_intake.json") for key in
               r2.list_keys(f"intake/{client}/incoming/")):
            # Form landing writes through separate DB helpers rather than conn.
            # It cannot participate in this SQLite transaction safely yet.
            held_subpath.append("intake_form_transaction_adapter_required")
            raise _mutation.MutationHold("intake_form_transaction_adapter_required")
        _active_mutation.get()["followups"] = followups
        guarded_r2 = _DispositionR2(r2)
        stats = _process_client_body(client, guarded_r2, poster, converter, phash,
                                     moderator)
        # The body's authoritative manifest read and write are both under this
        # lock. No incoming copy is consumed before its durable disposition.
        guarded_r2.consume_sources()
        return {"stats": stats, "consumed_sources": guarded_r2.deletions,
                "retained_objects": guarded_r2.retained,
                "manifest_sha256": guarded_r2.retained[f"intake/{client}/{MANIFEST}"]}
    try:
        result = _guarded(client, "intake_batch",
                          {"client": client, "lane": "incoming"},
                          _disposition, lib_dir)
    except _mutation.MutationHold:
        if held_subpath:
            raise _mutation.MutationHold(held_subpath[0]) from None
        raise
    stats = result["stats"]
    for callback in followups:
        try:
            callback()
        except Exception as exc:
            print(f"[intake] post-disposition follow-up failed for {client}: "
                  f"{type(exc).__name__}")
    return stats


def _process_client_body(client, r2, poster, converter, phash, moderator):
    stats = {"accepted": 0, "duplicates": 0, "held": 0, "flagged": 0, "deadlettered": 0,
             "skipped": 0, "intake_forms": 0, "needs_caption": 0, "low_res": 0}
    manifest = _load_manifest(r2, client)
    prefix = f"intake/{client}/incoming/"
    keys = sorted(r2.list_keys(prefix))
    sidecars = {k: None for k in keys if k.endswith("_upload.json")}
    media_keys = [k for k in keys if not k.endswith(".json")]
    form_keys = [k for k in keys if k.endswith("_intake.json")]

    # Intake FORM submissions first: they are tiny and carry the sources the
    # media may pair with. A malformed payload dead-letters; never crashes.
    lib_dir = _library_dir_for(client)
    for key in form_keys:
        if key in manifest["processed"]:
            stats["skipped"] += 1
            continue
        try:
            payload = json.loads(r2.get_bytes(key).decode("utf-8"))
            _land_intake_form(client, payload, r2, key, manifest, lib_dir)
            stats["intake_forms"] += 1
        except _mutation.MutationHold:
            raise   # a pending receipt mutation is never dead-lettered or marked processed
        except Exception as e:
            stats["deadlettered"] += 1
            try:
                _guarded(client, "intake_quarantine",
                         {"asset": os.path.basename(key), "lane": "form",
                          "reason": "malformed_form"},
                         lambda conn: (r2.put_bytes(
                             f"intake/{client}/deadletter/{os.path.basename(key)}",
                             r2.get_bytes(key)),
                             r2.delete(key),
                             {"quarantined": os.path.basename(key)})[-1],
                         lib_dir)
            except _mutation.MutationHold:
                raise
            except Exception as dl_err:
                print(f"[intake] form dead-letter failed for {client}/"
                      f"{os.path.basename(key)}: {type(dl_err).__name__}")
            manifest["processed"].append(key)
            _ops_alert(f"intake form dead-lettered {client}/"
                             f"{os.path.basename(key)}: {type(e).__name__}: {e}")

    # note/sidecar lookup: a media file's sidecar shares its timestamp prefix.
    # Returns (found, payload): found=True means a sidecar key existed (even if
    # the payload was malformed); found=False means no sidecar at all.
    def _sidecar_for(media_key):
        stamp = os.path.basename(media_key).split("_", 1)[0]
        for sk in sidecars:
            if os.path.basename(sk).startswith(stamp):
                try:
                    payload = json.loads(r2.get_bytes(sk).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("invalid upload sidecar")
                    if (_active_mutation.get() is not None and
                            not isinstance(payload.get("note", ""), str)):
                        raise ValueError("invalid upload caption")
                    return True, payload
                except Exception:
                    if _active_mutation.get() is not None:
                        raise _mutation.MutationHold("intake_sidecar_unavailable") from None
                    return True, {}
        return False, {}

    # Draft-on-upload (AGENT_DRAFT_ON_UPLOAD): assets filed THIS pass, so we can
    # draft one approval card per new upload the instant ingest finishes.
    newly_filed = []
    for key in media_keys:
        if key in manifest["processed"]:
            stats["skipped"] += 1
            continue
        name = os.path.basename(key)
        raw = None   # kept for dead-letter-from-memory + the originals archive
        try:
            raw = r2.get_bytes(key)

            # ZERO-BYTE GUARD: an empty upload can never be media. Quarantine to
            # the dead-letter prefix with a specific alert; never crash, never
            # hand empty bytes to a converter.
            if not raw:
                stats["deadlettered"] += 1
                _guarded(client, "intake_quarantine",
                         {"asset": name, "reason": "zero_byte"},
                         lambda conn: (r2.put_bytes(
                             f"intake/{client}/deadletter/{name}", b""),
                             r2.delete(key),
                             {"quarantined": name})[-1],
                         lib_dir)
                manifest["processed"].append(key)
                _ops_alert(f"intake ingest quarantined {client}/{name}: "
                                 "zero-byte upload (empty file, nothing filed)")
                continue

            # Source SHA-256 is authoritative. Drive MD5 is retained only as an
            # explicitly namespaced alias. Both are computed before conversion.
            src_aliases = visual_fingerprint.source_aliases(raw)
            src_fp = src_aliases[0]

            # RAW dedupe FIRST: the same file uploaded twice lands once, no
            # matter what the converter does with it. The surviving first copy
            # already holds these exact bytes, so deleting the re-upload loses
            # no source.
            raw_sha = hashlib.sha256(raw).hexdigest()
            if raw_sha in manifest["sha256_raw"]:
                stats["duplicates"] += 1
                if _active_mutation.get() is not None:
                    # An older manifest can name a source whose first JPEG was
                    # re-encoded without retaining its raw bytes. Keep this raw
                    # re-upload rather than relying on that historical claim.
                    archive_key = f"intake/{client}/originals/{os.path.basename(key)}"
                    provenance = _record_asset_provenance(
                        manifest, original_key=key, current_key=archive_key,
                        filename=os.path.basename(key), status="duplicate_raw",
                        source_fingerprint=src_fp, source_aliases=src_aliases[1:],
                        converted_bytes=raw)
                    r2.put_bytes(archive_key, raw)
                    r2.put_bytes(f"{archive_key}.provenance.json",
                                 json.dumps(provenance).encode("utf-8"),
                                 content_type="application/json")
                _guarded(client, "intake_delete",
                         {"asset": key, "reason": "raw_duplicate",
                          "sha256": raw_sha},
                         lambda conn: (r2.delete(key), {"deleted": key})[-1],
                         lib_dir)
                manifest["processed"].append(key)
                continue

            data, name = converter(raw, name)

            sha = hashlib.sha256(data).hexdigest()
            ph = phash(data, name)
            if sha in manifest["sha256"]:
                # EXACT converted-byte duplicate: the converted bytes are already
                # filed, but the RAW source of THIS upload may differ (a HEIC
                # original) — archive it BEFORE the incoming object is deleted.
                # No dedupe path ever destroys the only copy of a source.
                stats["duplicates"] += 1
                manifest["sha256_raw"].append(raw_sha)   # remember the raw form too
                archive_key = f"intake/{client}/originals/{os.path.basename(key)}"
                provenance = _record_asset_provenance(
                    manifest, original_key=key, current_key=archive_key,
                    filename=os.path.basename(key), status="duplicate_converted",
                    source_fingerprint=src_fp, source_aliases=src_aliases[1:],
                    converted_bytes=data)
                def _archive_dup(conn, archive_key=archive_key,
                                 provenance=provenance):
                    r2.put_bytes(archive_key, raw)
                    r2.put_bytes(
                        f"{archive_key}.provenance.json",
                        json.dumps(provenance).encode("utf-8"),
                        content_type="application/json")
                    r2.delete(key)
                    return {"archived": archive_key}
                _guarded(client, "intake_admit",
                         {"asset": key, "archive_key": archive_key,
                          "status": "duplicate_converted", "sha256": sha},
                         _archive_dup, lib_dir)
                manifest["processed"].append(key)
                continue
            if ph is not None and ph in manifest["phash"]:
                # pHash COLLISION — QUARANTINE/HOLD, NEVER DELETE. A perceptual
                # hash is similarity, not identity: two DIFFERENT photos can
                # collide, and the old code silently destroyed the client's
                # source on a collision. Hold the RAW source bytes under
                # intake/<client>/hold/ with an honest sidecar and one ops
                # alert; a human decides whether it is a true duplicate.
                stats["held"] += 1
                manifest["sha256_raw"].append(raw_sha)
                hold_key = f"intake/{client}/hold/{os.path.basename(key)}"
                provenance = _record_asset_provenance(
                    manifest, original_key=key, current_key=hold_key,
                    filename=os.path.basename(key), status="held_near_duplicate",
                    source_fingerprint=src_fp, source_aliases=src_aliases[1:],
                    converted_bytes=data, similarity_alias=f"perceptual:{ph}")
                hold_sidecar_key = (
                    f"intake/{client}/hold/"
                    f"{os.path.splitext(os.path.basename(key))[0]}.json")

                def _hold(conn, hold_key=hold_key, provenance=provenance,
                          hold_sidecar_key=hold_sidecar_key):
                    r2.put_bytes(hold_key, raw)
                    r2.put_bytes(
                        hold_sidecar_key,
                        json.dumps({
                            **provenance,
                            "phash": ph,
                            "note": "perceptual-hash collision with an accepted "
                                    "asset; source preserved, awaiting human "
                                    "keep/drop decision",
                        }).encode("utf-8"),
                        content_type="application/json")
                    r2.delete(key)   # only AFTER the hold copy + sidecar landed
                    return {"held": hold_key}
                _guarded(client, "intake_quarantine",
                         {"asset": key, "hold_key": hold_key,
                          "status": "held_near_duplicate",
                          "similarity_alias": f"perceptual:{ph}"},
                         _hold, lib_dir)
                manifest["processed"].append(key)
                _ops_alert(
                    f"intake ingest HELD {client}/{os.path.basename(key)}: "
                    "near-duplicate of an already-accepted asset (pHash "
                    "collision). Source preserved under hold/ — confirm "
                    "keep/drop; nothing was deleted.")
                continue

            ok, reason = moderator(data, name)
            if not ok:
                review_key = f"intake/{client}/review/{name}"
                provenance = _record_asset_provenance(
                    manifest, original_key=key, current_key=review_key,
                    filename=name, status="review",
                    source_fingerprint=src_fp, source_aliases=src_aliases[1:],
                    converted_bytes=data)
                provenance["review_reason"] = reason
                review_sidecar_key = (
                    f"intake/{client}/review/{os.path.splitext(name)[0]}.json")

                def _review(conn, review_key=review_key, provenance=provenance,
                            review_sidecar_key=review_sidecar_key):
                    r2.put_bytes(review_key, data)
                    r2.put_bytes(
                        review_sidecar_key,
                        json.dumps(provenance).encode("utf-8"),
                        content_type="application/json")
                    if raw != data or name != os.path.basename(key):
                        r2.put_bytes(
                            f"intake/{client}/originals/{os.path.basename(key)}",
                            raw)
                    r2.delete(key)
                    return {"quarantined": review_key}
                _guarded(client, "intake_quarantine",
                         {"asset": key, "review_key": review_key,
                          "status": "review", "reason": reason},
                         _review, lib_dir)
                manifest["processed"].append(key)
                stats["flagged"] += 1
                if poster is not None:
                    _post_notice(poster, f"Intake: {client} file {name} sent to review "
                                       f"({reason}); nothing filed to the library.")
                # A moderation reject can be a FALSE POSITIVE that silently buries a
                # legit gym photo in review/. Raise an ops alert so a human can eyeball
                # it and release it, rather than the photo just vanishing (audit #4).
                _ops_alert(
                    f"intake moderation sent {client}/{name} to review ({reason}); "
                    "verify — a false positive strands a legit photo in review/")
                continue

            # ORIGINALS KEPT: a conversion can change bytes without a rename.
            # archives the untouched source bytes to intake/<client>/originals/
            # BEFORE the incoming object is deleted. No conversion loses a file.
            # The original is only ever copied, never mutated in place.
            if raw != data or name != os.path.basename(key):
                originals_key = f"intake/{client}/originals/{os.path.basename(key)}"
                _guarded(client, "intake_admit",
                         {"asset": key, "archive_key": originals_key,
                          "status": "original_archived",
                          "source_fingerprint": src_fp},
                         lambda conn: (r2.put_bytes(originals_key, raw),
                                       {"archived": originals_key})[-1],
                         lib_dir)

            # THUMBNAIL: generated after conversion, before library filing.
            # A failed thumbnail logs a warning and never blocks ingest; a
            # receipt-protocol hold, however, is never swallowed.
            thumb_result = _make_thumbnail(data, name)
            if thumb_result is not None:
                thumb_bytes, thumb_name = thumb_result
                try:
                    _guarded(client, "intake_thumbnail",
                             {"asset": name, "thumb": thumb_name},
                             lambda conn: (r2.put_bytes(
                                 f"intake/{client}/thumbs/{thumb_name}",
                                 thumb_bytes, content_type="image/jpeg"),
                                 {"thumb": thumb_name})[-1],
                             lib_dir)
                except _mutation.MutationHold:
                    raise
                except Exception as thumb_err:
                    print(f"[intake] thumbnail store failed for {client}/{name}: "
                          f"{type(thumb_err).__name__}")
            else:
                if not name.lower().endswith((".mp4", ".mov")):
                    print(f"[intake] thumbnail skipped for {client}/{name} "
                          "(Pillow unavailable or unsupported format)")

            # LOW-RES FLAG: images whose width AND height are both below 800px are
            # accepted without blocking but tagged and the poster is notified.
            sidecar_found, sidecar_data = _sidecar_for(key)
            low_res_flag = {}
            if not name.lower().endswith((".mp4", ".mov")):
                try:
                    from PIL import Image  # lazy
                    img_check = Image.open(io.BytesIO(data))
                    w_check, h_check = img_check.size
                    if w_check < 800 and h_check < 800:
                        low_res_flag = {"low_res": True,
                                        "resolution": f"{w_check}x{h_check}"}
                        stats["low_res"] += 1
                        if poster is not None:
                            _post_notice(poster,
                                f"Heads up: the photo {name} for {client} is "
                                f"low resolution ({w_check}x{h_check}). It has "
                                "been filed but a higher resolution version will "
                                "look better in your content lineup.")
                except Exception:
                    pass

            # MISSING-CAPTION GATE: an upload whose sidecar exists but has no
            # caption is staged to pending_caption/ rather than the live library,
            # and status is set to needs_caption. The draft is BLOCKED until a
            # caption arrives. Nothing is invented. Never fabricate.
            # When NO sidecar exists at all, we skip this gate (no upload context
            # means there is nothing to check, and we preserve the old behavior).
            caption_text = (sidecar_data.get("note") or "").strip()
            if sidecar_found and not caption_text:
                stats["needs_caption"] += 1
                pending_key = f"intake/{client}/pending_caption/{name}"
                pending_sidecar = _record_asset_provenance(
                    manifest, original_key=key, current_key=pending_key,
                    filename=name, status="needs_caption",
                    source_fingerprint=src_fp, source_aliases=src_aliases[1:],
                    converted_bytes=data)
                pending_sidecar.update({
                    **low_res_flag,
                })
                pending_sidecar_key = (
                    f"intake/{client}/pending_caption/"
                    f"{os.path.splitext(name)[0]}.json")

                def _stage_pending(conn, pending_key=pending_key,
                                   pending_sidecar=pending_sidecar,
                                   pending_sidecar_key=pending_sidecar_key):
                    r2.put_bytes(pending_key, data)
                    r2.put_bytes(
                        pending_sidecar_key,
                        json.dumps(pending_sidecar).encode("utf-8"),
                        content_type="application/json",
                    )
                    r2.delete(key)
                    return {"staged": pending_key}
                _guarded(client, "intake_quarantine",
                         {"asset": key, "pending_key": pending_key,
                          "status": "needs_caption"},
                         _stage_pending, lib_dir)
                manifest["processed"].append(key)
                manifest["sha256"].append(sha)
                manifest["sha256_raw"].append(raw_sha)
                if ph is not None:
                    manifest["phash"].append(ph)
                if poster is not None:
                    _post_notice(poster,
                        f"Got your photo! Send a quick caption and we will get "
                        f"it into your content lineup.")
                continue

            library_path = os.path.join(lib_dir, name)
            provenance = _record_asset_provenance(
                manifest, original_key=key, current_key=library_path,
                filename=name, status="accepted",
                source_fingerprint=src_fp, source_aliases=src_aliases[1:],
                converted_bytes=data)
            provenance.update(low_res_flag)
            note = caption_text

            def _file_into_library(conn, note=note, provenance=provenance,
                                   library_path=library_path):
                os.makedirs(lib_dir, exist_ok=True)
                active = _active_mutation.get()
                if active is not None:
                    cfg = active["cfg"]
                    media_path = cfg.asset_path(Path(library_path))
                    stem = os.path.splitext(name)[0]
                    sidecar_path = cfg.asset_path(Path(lib_dir) / f"{stem}.json")
                    caption_path = (cfg.asset_path(Path(lib_dir) / f"{stem}.txt")
                                    if note else None)
                    try:
                        existing = (json.loads(sidecar_path.read_text(encoding="utf-8"))
                                    if sidecar_path.exists() else {})
                        if not isinstance(existing, dict):
                            raise ValueError("invalid provenance sidecar")
                    except (OSError, ValueError):
                        raise _mutation.MutationHold("intake_provenance_unavailable") from None
                    existing.update(provenance)
                    _mutation.atomic_write_bytes(media_path, data)
                    if caption_path is not None:
                        _mutation.atomic_write_bytes(caption_path, note.strip().encode("utf-8"))
                    _mutation.atomic_write_json(sidecar_path, existing)
                    r2.delete(key)
                    return {"filed": str(media_path)}
                with open(library_path, "wb") as fh:
                    fh.write(data)
                if note:
                    stem = os.path.splitext(name)[0]
                    with open(os.path.join(lib_dir, f"{stem}.txt"),
                              "w", encoding="utf-8") as fh:
                        fh.write(note.strip())
                if low_res_flag:
                    stem = os.path.splitext(name)[0]
                    try:
                        existing_sidecar_path = os.path.join(lib_dir, f"{stem}.json")
                        if os.path.exists(existing_sidecar_path):
                            with open(existing_sidecar_path, encoding="utf-8") as _fh:
                                filed_sidecar = json.load(_fh)
                        else:
                            filed_sidecar = {}
                        filed_sidecar.update(low_res_flag)
                        with open(existing_sidecar_path, "w", encoding="utf-8") as _fh:
                            json.dump(filed_sidecar, _fh)
                    except Exception:
                        pass
                provenance_path = os.path.join(
                    lib_dir, f"{os.path.splitext(name)[0]}.json")
                try:
                    existing_provenance = {}
                    if os.path.exists(provenance_path):
                        with open(provenance_path, encoding="utf-8") as _fh:
                            existing_provenance = json.load(_fh) or {}
                    existing_provenance.update(provenance)
                    with open(provenance_path, "w", encoding="utf-8") as _fh:
                        json.dump(existing_provenance, _fh)
                except (OSError, ValueError):
                    pass
                r2.delete(key)
                return {"filed": library_path}
            _guarded(client, "intake_admit",
                     {"asset": key, "library_path": library_path,
                      "status": "accepted", "sha256": sha,
                      "source_fingerprint": src_fp},
                     _file_into_library, lib_dir)

            manifest["processed"].append(key)
            manifest["sha256"].append(sha)
            manifest["sha256_raw"].append(raw_sha)
            if ph is not None:
                manifest["phash"].append(ph)
            stats["accepted"] += 1
            newly_filed.append((library_path, note))
            # DAM auto-tag on the freshly filed asset (AGENT_AUTOTAG_ENABLED,
            # OFF by default; errors are contained inside autotag)
            try:
                from . import dam
                _after_disposition(lambda path=os.path.join(lib_dir, name): dam.autotag(path))
            except Exception:
                pass
        except _mutation.MutationHold:
            # A fenced mutation is PENDING, never failed-safe: do not dead-letter
            # the source, do not mark the key processed, do not save the manifest
            # over it. The pass aborts so the durable journal can be reconciled.
            raise
        except Exception as e:
            stats["deadlettered"] += 1
            try:
                # quarantine from the bytes already in memory when we have them
                # (a corrupt object can be unreadable a second time); re-fetch
                # only if the original get itself was what failed.
                _guarded(client, "intake_quarantine",
                         {"asset": os.path.basename(key), "lane": "media",
                          "reason": f"{type(e).__name__}"},
                         lambda conn: (r2.put_bytes(
                             f"intake/{client}/deadletter/{os.path.basename(key)}",
                             raw if raw is not None else r2.get_bytes(key)),
                             r2.delete(key),
                             {"quarantined": os.path.basename(key)})[-1],
                         lib_dir)
            except _mutation.MutationHold:
                raise
            except Exception as dl_err:
                # even dead-lettering must never crash the loop, but a failed
                # dead-letter is LOUD, and the key is still marked processed
                # below so the same bad file is never re-picked forever.
                print(f"[intake] dead-letter itself failed for {client}/"
                      f"{os.path.basename(key)}: {type(dl_err).__name__}")
            manifest["processed"].append(key)
            _ops_alert(f"intake ingest dead-lettered {client}/{os.path.basename(key)}: "
                             f"{type(e).__name__}: {e}")

    # WHOLE-BATCH DEAD-LETTER ESCALATION (audit #3): when a pass tried several media
    # files and EVERY one dead-lettered (nothing accepted, nothing merely deduped),
    # that is not a bad file — it is a systemic decoder/converter failure (e.g.
    # pillow-heif or ffmpeg missing from the image so every HEIC/HEVC upload fails).
    # The per-file alerts alone read as noise; this fires ONE loud, unmistakable ops
    # alert so the batch failure is visible and actionable, not buried.
    if stats["deadlettered"] >= 3 and stats["accepted"] == 0 \
            and stats["duplicates"] == 0:
        _ops_alert(
            f"intake BATCH FAILURE for {client}: {stats['deadlettered']} media file(s) "
            "dead-lettered this pass and NONE were filed. This is usually a missing "
            "decoder/converter in the deployed image (pillow-heif for HEIC, ffmpeg for "
            "HEVC/MOV). Check the worker image before more uploads are lost.",
            force=True)

    manifest_bytes = json.dumps(manifest).encode("utf-8")
    _guarded(client, "intake_manifest",
             {"client": client,
              "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
              "processed": len(manifest["processed"])},
             lambda conn: (_save_manifest(r2, client, manifest),
                           {"processed": len(manifest["processed"])})[-1],
             lib_dir)

    # DRAFT-ON-UPLOAD (AGENT_DRAFT_ON_UPLOAD, OFF by default): draft one approval
    # card per newly filed asset the instant ingest finishes, so a gym's upload
    # lands in the queue immediately instead of waiting for the daily draw. The
    # trigger reuses the daily draft+surface path (every gate intact) and is fully
    # self-guarding: flag OFF or no new assets -> no-op; a draft failure never
    # breaks ingest (this whole block is contained).
    if config.draft_on_upload_enabled() and newly_filed:
        def _draft_uploaded():
            try:
                from . import runner
                drafts = runner.draft_for_new_upload(client, newly_filed, poster=poster)
                stats["drafted_on_upload"] = len(drafts)
            except Exception as e:
                print(f"[intake] draft-on-upload failed for {client}: "
                      f"{type(e).__name__}: {e}")
                _ops_alert(f"draft-on-upload trigger errored for {client}: "
                           f"{type(e).__name__}: {e}. Media is filed; the daily "
                           "draw will still pick it up.")
        _after_disposition(_draft_uploaded)

    return stats


class _R2:
    """List/get/put/delete R2 wrapper (listener side). Credentials lazy, never logged."""

    def __init__(self, s3, bucket):
        self._s3 = s3
        self._bucket = bucket

    def list_keys(self, prefix):
        keys, token = [], None
        while True:
            kw = {"Bucket": self._bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kw)
            keys.extend(o["Key"] for o in resp.get("Contents", []))
            token = resp.get("NextContinuationToken")
            if not token:
                return keys

    def get_bytes(self, key):
        return self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data,
                            ContentType=content_type)

    def delete(self, key):
        self._s3.delete_object(Bucket=self._bucket, Key=key)


def _default_r2():
    key_id = os.environ.get(config.S3_ACCESS_KEY_ID_ENV)
    secret = os.environ.get(config.S3_SECRET_ACCESS_KEY_ENV)
    if not key_id or not secret or not config.S3_BUCKET:
        return None
    import boto3  # lazy
    s3 = boto3.client("s3", endpoint_url=config.S3_ENDPOINT or None,
                      region_name=config.S3_REGION or None,
                      aws_access_key_id=key_id, aws_secret_access_key=secret)
    return _R2(s3, config.S3_BUCKET)
