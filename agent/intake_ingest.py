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
  4. dedupe the converted bytes by SHA-256 plus perceptual hash against
     everything already accepted,
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
import io
import json
import os

from . import config, ops_alerts
from .accounts import get_account

MANIFEST = "manifest.json"


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
    name changes, so no conversion ever loses the source file."""
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
def _load_manifest(r2, client):
    try:
        raw = r2.get_bytes(f"intake/{client}/{MANIFEST}")
        manifest = json.loads(raw.decode("utf-8"))
    except Exception:
        manifest = {"processed": [], "sha256": [], "phash": []}
    # additive key for raw-bytes dedupe; old manifests gain it on first touch
    manifest.setdefault("sha256_raw", [])
    return manifest


def _save_manifest(r2, client, manifest):
    r2.put_bytes(f"intake/{client}/{MANIFEST}",
                 json.dumps(manifest).encode("utf-8"),
                 content_type="application/json")


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


def _land_intake_form(client, payload, r2, key, manifest):
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
                registered = bool(_accounts.register_gym(
                    client, name=proposal["gym_name"],
                    ig_handle=proposal.get("ig_handle", ""),
                    fb_page=proposal.get("fb_page", "")))
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
    r2.put_bytes(f"intake/{client}/forms/{os.path.basename(key)}",
                 json.dumps(payload).encode("utf-8"),
                 content_type="application/json")
    r2.delete(key)
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


def _process_client(client, r2, poster, converter, phash, moderator):
    stats = {"accepted": 0, "duplicates": 0, "flagged": 0, "deadlettered": 0,
             "skipped": 0, "intake_forms": 0, "needs_caption": 0, "low_res": 0}
    manifest = _load_manifest(r2, client)
    prefix = f"intake/{client}/incoming/"
    keys = sorted(r2.list_keys(prefix))
    sidecars = {k: None for k in keys if k.endswith("_upload.json")}
    media_keys = [k for k in keys if not k.endswith(".json")]
    form_keys = [k for k in keys if k.endswith("_intake.json")]

    # Intake FORM submissions first: they are tiny and carry the sources the
    # media may pair with. A malformed payload dead-letters; never crashes.
    for key in form_keys:
        if key in manifest["processed"]:
            stats["skipped"] += 1
            continue
        try:
            payload = json.loads(r2.get_bytes(key).decode("utf-8"))
            _land_intake_form(client, payload, r2, key, manifest)
            stats["intake_forms"] += 1
        except Exception as e:
            stats["deadlettered"] += 1
            try:
                r2.put_bytes(f"intake/{client}/deadletter/{os.path.basename(key)}",
                             r2.get_bytes(key))
                r2.delete(key)
            except Exception as dl_err:
                print(f"[intake] form dead-letter failed for {client}/"
                      f"{os.path.basename(key)}: {type(dl_err).__name__}")
            manifest["processed"].append(key)
            ops_alerts.alert(f"intake form dead-lettered {client}/"
                             f"{os.path.basename(key)}: {type(e).__name__}: {e}")

    # note/sidecar lookup: a media file's sidecar shares its timestamp prefix.
    # Returns (found, payload): found=True means a sidecar key existed (even if
    # the payload was malformed); found=False means no sidecar at all.
    def _sidecar_for(media_key):
        stamp = os.path.basename(media_key).split("_", 1)[0]
        for sk in sidecars:
            if os.path.basename(sk).startswith(stamp):
                try:
                    return True, json.loads(r2.get_bytes(sk).decode("utf-8")) or {}
                except Exception:
                    return True, {}
        return False, {}

    def _note_for(media_key):
        _, payload = _sidecar_for(media_key)
        return payload.get("note", "")

    lib_dir = _library_dir_for(client)
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
                r2.put_bytes(f"intake/{client}/deadletter/{name}", b"")
                r2.delete(key)
                manifest["processed"].append(key)
                ops_alerts.alert(f"intake ingest quarantined {client}/{name}: "
                                 "zero-byte upload (empty file, nothing filed)")
                continue

            # RAW dedupe FIRST: the same file uploaded twice lands once, no
            # matter what the converter does with it.
            raw_sha = hashlib.sha256(raw).hexdigest()
            if raw_sha in manifest["sha256_raw"]:
                stats["duplicates"] += 1
                r2.delete(key)
                manifest["processed"].append(key)
                continue

            data, name = converter(raw, name)

            sha = hashlib.sha256(data).hexdigest()
            ph = phash(data, name)
            if sha in manifest["sha256"] or (ph is not None and ph in manifest["phash"]):
                stats["duplicates"] += 1
                manifest["sha256_raw"].append(raw_sha)   # remember the raw form too
                r2.delete(key)
                manifest["processed"].append(key)
                continue

            ok, reason = moderator(data, name)
            if not ok:
                r2.put_bytes(f"intake/{client}/review/{name}", data)
                r2.delete(key)
                manifest["processed"].append(key)
                stats["flagged"] += 1
                if poster is not None:
                    poster.post_notice(f"Intake: {client} file {name} sent to review "
                                       f"({reason}); nothing filed to the library.")
                # A moderation reject can be a FALSE POSITIVE that silently buries a
                # legit gym photo in review/. Raise an ops alert so a human can eyeball
                # it and release it, rather than the photo just vanishing (audit #4).
                ops_alerts.alert(
                    f"intake moderation sent {client}/{name} to review ({reason}); "
                    "verify — a false positive strands a legit photo in review/")
                continue

            # ORIGINALS KEPT: a conversion (name changed: HEIC->JPG, MOV->MP4)
            # archives the untouched source bytes to intake/<client>/originals/
            # BEFORE the incoming object is deleted. No conversion loses a file.
            if name != os.path.basename(key):
                r2.put_bytes(f"intake/{client}/originals/{os.path.basename(key)}",
                             raw)

            # THUMBNAIL: generated after conversion, before library filing.
            # A failed thumbnail logs a warning and never blocks ingest.
            thumb_result = _make_thumbnail(data, name)
            if thumb_result is not None:
                thumb_bytes, thumb_name = thumb_result
                try:
                    r2.put_bytes(f"intake/{client}/thumbs/{thumb_name}",
                                 thumb_bytes, content_type="image/jpeg")
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
                            poster.post_notice(
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
                pending_sidecar = {
                    "status": "needs_caption",
                    "original_key": key,
                    **low_res_flag,
                }
                r2.put_bytes(f"intake/{client}/pending_caption/{name}", data)
                r2.put_bytes(
                    f"intake/{client}/pending_caption/{os.path.splitext(name)[0]}.json",
                    json.dumps(pending_sidecar).encode("utf-8"),
                    content_type="application/json",
                )
                manifest["processed"].append(key)
                manifest["sha256"].append(sha)
                manifest["sha256_raw"].append(raw_sha)
                if ph is not None:
                    manifest["phash"].append(ph)
                r2.delete(key)
                if poster is not None:
                    poster.post_notice(
                        f"Got your photo! Send a quick caption and we will get "
                        f"it into your content lineup.")
                continue

            os.makedirs(lib_dir, exist_ok=True)
            with open(os.path.join(lib_dir, name), "wb") as fh:
                fh.write(data)
            note = caption_text
            if note:
                stem = os.path.splitext(name)[0]
                with open(os.path.join(lib_dir, f"{stem}.txt"), "w", encoding="utf-8") as fh:
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

            manifest["processed"].append(key)
            manifest["sha256"].append(sha)
            manifest["sha256_raw"].append(raw_sha)
            if ph is not None:
                manifest["phash"].append(ph)
            r2.delete(key)
            stats["accepted"] += 1
            newly_filed.append((os.path.join(lib_dir, name), note))
            # DAM auto-tag on the freshly filed asset (AGENT_AUTOTAG_ENABLED,
            # OFF by default; errors are contained inside autotag)
            try:
                from . import dam
                dam.autotag(os.path.join(lib_dir, name))
            except Exception:
                pass
        except Exception as e:
            stats["deadlettered"] += 1
            try:
                # quarantine from the bytes already in memory when we have them
                # (a corrupt object can be unreadable a second time); re-fetch
                # only if the original get itself was what failed.
                r2.put_bytes(f"intake/{client}/deadletter/{os.path.basename(key)}",
                             raw if raw is not None else r2.get_bytes(key))
                r2.delete(key)
            except Exception as dl_err:
                # even dead-lettering must never crash the loop, but a failed
                # dead-letter is LOUD, and the key is still marked processed
                # below so the same bad file is never re-picked forever.
                print(f"[intake] dead-letter itself failed for {client}/"
                      f"{os.path.basename(key)}: {type(dl_err).__name__}")
            manifest["processed"].append(key)
            ops_alerts.alert(f"intake ingest dead-lettered {client}/{os.path.basename(key)}: "
                             f"{type(e).__name__}: {e}")

    # WHOLE-BATCH DEAD-LETTER ESCALATION (audit #3): when a pass tried several media
    # files and EVERY one dead-lettered (nothing accepted, nothing merely deduped),
    # that is not a bad file — it is a systemic decoder/converter failure (e.g.
    # pillow-heif or ffmpeg missing from the image so every HEIC/HEVC upload fails).
    # The per-file alerts alone read as noise; this fires ONE loud, unmistakable ops
    # alert so the batch failure is visible and actionable, not buried.
    if stats["deadlettered"] >= 3 and stats["accepted"] == 0 \
            and stats["duplicates"] == 0:
        ops_alerts.alert(
            f"intake BATCH FAILURE for {client}: {stats['deadlettered']} media file(s) "
            "dead-lettered this pass and NONE were filed. This is usually a missing "
            "decoder/converter in the deployed image (pillow-heif for HEIC, ffmpeg for "
            "HEVC/MOV). Check the worker image before more uploads are lost.",
            force=True)

    _save_manifest(r2, client, manifest)

    # DRAFT-ON-UPLOAD (AGENT_DRAFT_ON_UPLOAD, OFF by default): draft one approval
    # card per newly filed asset the instant ingest finishes, so a gym's upload
    # lands in the queue immediately instead of waiting for the daily draw. The
    # trigger reuses the daily draft+surface path (every gate intact) and is fully
    # self-guarding: flag OFF or no new assets -> no-op; a draft failure never
    # breaks ingest (this whole block is contained).
    if config.draft_on_upload_enabled() and newly_filed:
        try:
            from . import runner
            drafts = runner.draft_for_new_upload(client, newly_filed, poster=poster)
            stats["drafted_on_upload"] = len(drafts)
        except Exception as e:
            print(f"[intake] draft-on-upload failed for {client}: "
                  f"{type(e).__name__}: {e}")
            ops_alerts.alert(f"draft-on-upload trigger errored for {client}: "
                             f"{type(e).__name__}: {e}. Media is filed; the daily "
                             "draw will still pick it up.")

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
