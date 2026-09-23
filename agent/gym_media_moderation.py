"""
gym_media_moderation.py — bounded moderation EVIDENCE producer for gym Drive media.

Downloads ONE pending gym-media photo, verifies its bytes against the indexed
Drive content hash, asks a vision provider for a strict safety/people verdict,
and records the parsed verdict as moderation evidence on the media_asset row.

This module is NOT a reviewer and NOT an approver:
  * review_status is NEVER written — it stays 'pending_review'. Operator review
    (agent/gym_media_review.py) remains the sole approval writer. Nothing here
    approves, posts, or stages anything.
  * Evidence is written ONLY to a row that is review_status='pending_review' AND
    moderation_status='pending', via the store's conditional
    update_moderation_asset (content-hash pinned); a concurrent sync or second
    moderation run loses the race instead of silently overwriting.

Fail-closed (returns ok=False with a reason, never raises, and never writes) on:
  * asset missing, or asset.gym_id != the caller's gym (cross-tenant)
  * no content_hash on the row (bytes can never be bound to evidence)
  * review_status != 'pending_review' or moderation_status != 'pending'
  * non-photo assets (videos carry motion/audio this prompt does not judge)
  * downloaded bytes whose recomputed MD5 differs from the row's content_hash
    (hash drift: the file changed since indexing — stale evidence is poison)
  * vision provider unarmed (no API key), non-JSON output, missing/extra-ambiguous
    keys, verdict 'unknown', or people_detected not a strict bool
  * vision provider raising (network/quota/model error)

Secrets are never logged: reasons name the failure class, never the key, the
model response body beyond its parsed verdict, or the file bytes.

Store errors other than the conditional-write conflict (MediaStoreError 409)
are unexpected: they propagate to the caller so a broken store is loud, not a
silent skip. The 409 conflict (asset changed mid-run) is expected and returns
ok=False.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config, gym_media_index as _idx
from .media_source_store import MediaStoreError

_MODERATION_PROMPT = (
    "You are a safety and privacy classifier for a gym's marketing photo library. "
    "Look at this one image and answer TWO questions:\n"
    "1. verdict: is this image safe to publish as a gym's marketing photo? Answer "
    "\"unsafe\" if it contains nudity or sexual content, graphic violence or gore, "
    "blood or visible injury, hateful symbols or gestures, illegal drugs, or a "
    "visible minor's face. Answer \"clean\" only when none of those apply. Answer "
    "\"unknown\" when you cannot tell or the image is unreadable.\n"
    "2. people_detected: are one or more recognizable human faces or bodies of "
    "adults visible? true, false, or null when you cannot tell.\n"
    "Reply with ONLY a JSON object, no prose, no markdown fence, with exactly these "
    "keys: {\"verdict\": \"clean\"|\"unsafe\"|\"unknown\", "
    "\"people_detected\": true|false|null}.")

_VERDICTS = ("clean", "unsafe", "unknown")


def _fail(asset_id, gym_id, reason):
    return {"ok": False, "asset_id": asset_id, "gym_id": gym_id, "reason": reason}


def default_vision(model=None):
    """The Gemini vision moderation callable (image_bytes -> raw text), or None
    when unarmed (no API key). Lazy google.genai import, same config surface as
    agent/ocr_check: key from os.environ[config.NANO_API_KEY_ENV], model from
    config.OCR_MODEL. The raw text is parsed strictly by the caller."""
    key = os.environ.get(config.NANO_API_KEY_ENV)
    if not key:
        return None
    model = model or config.OCR_MODEL
    from google import genai  # lazy — tests never touch network
    from google.genai import types as gtypes
    client = genai.Client(api_key=key)

    def _scan(image_bytes, mime_type="image/jpeg"):
        resp = client.models.generate_content(
            model=model,
            contents=[gtypes.Part.from_bytes(data=image_bytes,
                                             mime_type=mime_type),
                      _MODERATION_PROMPT])
        return getattr(resp, "text", "") or ""

    return _scan


def parse_verdict(raw):
    """Strictly parse the provider's JSON-only reply into (verdict, people), or
    None on any deviation: non-JSON, markdown-fenced JSON, missing keys, unknown
    extra keys, a verdict outside clean/unsafe/unknown, or people_detected that
    is neither a bool nor None. Ambiguity is never coerced into a verdict."""
    text = str(raw or "").strip()
    if text.startswith("```"):
        return None                      # fenced output is not JSON-only
    def unique_keys(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise ValueError("duplicate provider key")
            data[key] = value
        return data

    try:
        data = json.loads(text, object_pairs_hook=unique_keys)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or set(data) != {"verdict", "people_detected"}:
        return None
    verdict = data.get("verdict")
    people = data.get("people_detected")
    if verdict not in _VERDICTS:
        return None
    if people is not None and not isinstance(people, bool):
        return None
    return verdict, people


def _md5_hex(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_evidence(provider, verdict, asset, people_detected, observed_at):
    """The moderation_json payload, shaped to EXACTLY what
    gym_media_selector._clean_moderation_evidence validates, plus the provider
    string: verdict, provider, content_hash, asset_id, gym_id, people_detected
    (identity-equal to the row's people_detected), observed_at (ISO with tz)."""
    return {
        "provider": provider,
        "verdict": verdict,
        "content_hash": asset["content_hash"],
        "asset_id": asset["id"],
        "gym_id": asset["gym_id"],
        "people_detected": people_detected,
        "observed_at": observed_at,
    }


def _outcome_fields(verdict, people, provider, asset, observed_at):
    """Map a parsed verdict to the media_asset field update. review_status is
    never among them. people_detected is recorded ONLY as a strict bool (a null
    provider answer records None — consent then stays pending and unusable)."""
    evidence = build_evidence(provider, verdict, asset, people, observed_at)
    fields = {"moderation_json": evidence,
              "people_detected": people if isinstance(people, bool) else None}
    if verdict == "clean" and people is False:
        fields.update(moderation_status="clean", consent_status="not_required")
    elif verdict == "clean":
        # People present (or unreadable): clean content, but a release is still
        # required — consent stays pending for operator review to grant.
        fields.update(moderation_status="clean", consent_status="pending")
    else:
        # 'unsafe' or 'unknown' both quarantine the asset pending a human look;
        # neither can ever clear it.
        fields.update(moderation_status="flagged", consent_status="pending")
    return fields


def moderate_asset(gym_id, asset_id, *, store, drive, vision=None, now_iso=None):
    """Produce moderation evidence for ONE pending asset and conditionally write
    it. Returns {ok, asset_id, gym_id, moderation_status?, people_detected?,
    reason?}; never raises for an expected degrade (see module docstring).

    vision: injectable callable image_bytes -> raw provider text (default:
    default_vision(), None -> fail closed). now_iso: injectable clock for tests.
    drive: a DriveClient-shaped object with download(file_id, dest) -> Path."""
    gym_id = str(gym_id or "").strip()
    asset_id = str(asset_id or "").strip()
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()

    asset = store.get_asset(asset_id)
    if not asset:
        return _fail(asset_id, gym_id, "asset not found")
    if str(asset.get("gym_id") or "") != gym_id:
        return _fail(asset_id, gym_id, "asset belongs to another gym")
    content_hash = str(asset.get("content_hash") or "").strip()
    if not content_hash:
        return _fail(asset_id, gym_id, "no content hash — bytes cannot be bound")
    if asset.get("review_status") != "pending_review":
        return _fail(asset_id, gym_id,
                     f"review_status is {asset.get('review_status')!r}, not pending_review")
    if asset.get("moderation_status") not in ("pending",):
        return _fail(asset_id, gym_id,
                     f"already moderated ({asset.get('moderation_status')!r})")
    if asset.get("kind") != _idx.KIND_PHOTO:
        return _fail(asset_id, gym_id,
                     f"kind {asset.get('kind')!r} is not a photo — skipped")

    if vision is None:
        vision = default_vision()
    if vision is None:
        return _fail(asset_id, gym_id,
                     f"no vision provider armed (missing {config.NANO_API_KEY_ENV})")

    tmp_dir = tempfile.mkdtemp(prefix="gymmod_")
    tmp_path = Path(tmp_dir) / "moderation.bin"
    try:
        drive.download(asset_id, tmp_path)
        digest = _md5_hex(tmp_path)
        if digest != content_hash:
            return _fail(asset_id, gym_id,
                         "hash drift: downloaded bytes no longer match the "
                         "indexed content_hash — evidence refused")
        raw = vision(tmp_path.read_bytes(),
                     str(asset.get("mime_type") or "image/jpeg"))
    except Exception as e:  # noqa: BLE001 - drive/vision failure is a degrade
        return _fail(asset_id, gym_id,
                     f"download/scan failed: {type(e).__name__}")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
            Path(tmp_dir).rmdir()
        except OSError:
            pass

    parsed = parse_verdict(raw)
    if parsed is None:
        return _fail(asset_id, gym_id,
                     "unparseable or ambiguous provider verdict — no evidence written")
    verdict, people = parsed
    if verdict == "unknown":
        # Explicit fail-closed verdict: recorded as flagged so a human looks.
        pass

    provider = f"gemini:{config.OCR_MODEL}"
    fields = _outcome_fields(verdict, people, provider, asset, now_iso)
    try:
        store.update_moderation_asset(
            gym_id, asset_id, fields, expected_content_hash=content_hash)
    except MediaStoreError as e:
        if getattr(e, "status", None) == 409:
            return _fail(asset_id, gym_id,
                         "asset changed during moderation (conflict) — no write")
        raise  # unexpected store failure: loud, not a silent skip
    return {"ok": True,
            "asset_id": asset_id,
            "gym_id": gym_id,
            "moderation_status": fields["moderation_status"],
            "people_detected": fields["people_detected"],
            "verdict": verdict,
            "evidence": fields["moderation_json"]}
