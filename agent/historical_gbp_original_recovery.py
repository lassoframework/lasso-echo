"""historical_gbp_original_recovery.py — bounded, fail-closed original-source
authority for a small number of HISTORICAL held GBP rows whose raw original is
no longer tenant-local (source_media_url / source_media_asset_id /
drive_file_id all NULL). See docs/HISTORICAL_GBP_ORIGINAL_RECOVERY.md.

This is NOT photo swapping and NOT a signed historical owner manifest. The
only calendar write ever performed is target.source_media_url NULL -> the
operator-pinned trusted raw URL, inside ONE service-role SECURITY DEFINER RPC
(migrations/DRAFT_historical_gbp_original_recovery.sql) that freezes exact
before/after snapshots into an immutable receipt. The hold, approval, status,
caption, date and delivered media bytes are preserved untouched.

Gates (ALL required, all default closed):
  * AGENT_HISTORICAL_GBP_ORIGINAL_RECOVERY truthy (default OFF)
  * gym in AGENT_HISTORICAL_GBP_ORIGINAL_RECOVERY_GYMS (CSV, default EMPTY)
  * an operator-seeded DB binding for (gym_id, row_id) whose pinned
    source_origin equals the operator-seeded portal_action_receipt_config
    'public_origin' (DB enforces; unset origin fails closed)

Verification (every observe/apply/receipt read):
  * target is an exact eligible held GBP row and its full live snapshot equals
    the binding's expected_before (DB re-checks under lock at apply);
  * the same-tenant historical row's full snapshot equals the pinned
    historical snapshot and references the pinned raw URL;
  * the pinned raw URL has the anchored trusted-origin tenant
    content-addressed shape (https only, exact origin, /echo/<slug>/ or
    /echo/<slug>_ig/ + 16-hex segment + name, no query/fragment/userinfo,
    no redirects, no traversal, no mixed tenant/host);
  * bounded downloaded raw bytes match the pinned source SHA256 and the URL's
    SHA1-16 content-address segment;
  * the delivered bytes match the pinned delivered SHA256 and their URL's
    SHA1-16 segment, and an INDEPENDENT in-memory GBP crop_4x3 re-render
    (ImageOps.fit RGB 1200x900 LANCZOS JPEG q90) of the raw bytes equals the
    delivered bytes EXACTLY.

Nothing here publishes, hosts, copies files, or manufactures an old manifest;
the receipt is a fresh historical reconstruction receipt.
"""

import hashlib
import io
import json
import os
import re
from urllib.parse import urlsplit
import uuid

from . import visual_writer_prepare as vp

FLAG_ENV = "AGENT_HISTORICAL_GBP_ORIGINAL_RECOVERY"
GYMS_ENV = "AGENT_HISTORICAL_GBP_ORIGINAL_RECOVERY_GYMS"
RECIPE = "gbp-crop-4x3-jpeg90-v1"
HOLD_REASON = "cross_date_media_repeat_needs_new_visual"
_HEX16 = re.compile(r"[0-9a-f]{16}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_ORIGIN = re.compile(r"https://[a-z0-9.-]+(:[0-9]+)?\Z")
GBP_W, GBP_H = 1200, 900



class RecoveryError(ValueError):
    """Base refusal: recovery can never prove the original."""


class RecoveryUnavailable(RecoveryError):
    """Flag off, gym not allowlisted, or no configured binding/receipt:
    callers (media_guard) fall back to the ordinary refusal."""


class RecoveryVerificationError(RecoveryError):
    """Configured evidence disagreed (drift, forgery, cross-tenant, bytes)."""


class RecoveryStoreError(RecoveryError):
    """Transport/store failure with an UNKNOWN write outcome; reconcile by
    re-reading the exact persisted receipt, never by blind replay."""


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def enabled():
    return _truthy(os.environ.get(FLAG_ENV, "false"))


def allowed_gyms():
    raw = os.environ.get(GYMS_ENV, "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _gate(account_key):
    if not enabled():
        raise RecoveryUnavailable("historical GBP original recovery is disabled")
    if str(account_key or "") not in allowed_gyms():
        raise RecoveryUnavailable("gym is not allowlisted for historical GBP recovery")



def row_snapshot(row):
    """Every persisted field, without dropping current or future columns."""
    if not isinstance(row, dict):
        raise RecoveryVerificationError("row snapshot missing")
    return json.loads(json.dumps(row, allow_nan=False))


def request_fingerprint(account_key, row_id, binding):
    """Immutable request identity: gym + row + the operator-pinned binding
    fields the proof restates. Never derived from mutable row reads."""
    payload = {
        "gym_id": str(account_key),
        "row_id": str(row_id),
        "historical_row_id": str(binding.get("historical_row_id") or ""),
        "source_url": str(binding.get("source_url") or ""),
        "source_sha256": str(binding.get("source_sha256") or ""),
        "delivered_sha256": str(binding.get("delivered_sha256") or ""),
        "recipe": str(binding.get("recipe") or ""),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def proof_for(binding):
    """The proof payload: ONLY a restatement of the configured binding (the
    SQL apply requires exact equality, so a caller can never introduce a URL,
    hash or recipe the operator did not pin)."""
    return {
        "source_url": str(binding["source_url"]),
        "source_sha256": str(binding["source_sha256"]),
        "delivered_sha256": str(binding["delivered_sha256"]),
        "recipe": str(binding["recipe"]),
        "tenant_slug": str(binding["tenant_slug"]),
    }


def validate_hosted_url(url, origin, slug):
    """Anchored trusted-origin tenant content-addressed shape:
    <origin>/echo/<slug>/<sha1-16>/<name> or .../echo/<slug>_ig/.... https
    only, exact origin, no query/fragment/userinfo, no traversal, no
    cross-tenant or slug-prefix path. Returns the 16-hex segment or None."""
    if (not isinstance(url, str) or not isinstance(origin, str)
            or not isinstance(slug, str) or not slug):
        return None
    if not _ORIGIN.match(origin):
        return None
    if any(c.isspace() for c in url):
        return None
    try:
        parsed = urlsplit(url)
        base = urlsplit(origin)
    except ValueError:
        return None
    if (parsed.scheme != "https" or base.scheme != "https"
            or parsed.netloc.lower() != base.netloc.lower()
            or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        return None
    path = parsed.path
    for prefix in (f"/echo/{slug}/", f"/echo/{slug}_ig/"):
        if path.startswith(prefix):
            rest = path[len(prefix):]
            break
    else:
        return None
    parts = rest.split("/")
    if len(parts) != 2 or not _HEX16.match(parts[0]) or not parts[1]:
        return None
    lowered = url.lower()
    if ".." in url or "%2e" in lowered or "%2f" in lowered:
        return None
    return parts[0]


def _sha1_segment_ok(data, segment):
    return hashlib.sha1(data).hexdigest()[:16] == segment


def _default_reader(url):
    """Bounded byte fetch: exact URL, https, redirects REFUSED, size-capped.
    A redirect is never followed: the attested bytes must come from the exact
    pinned URL."""
    import requests
    with requests.get(url, timeout=(5, 30), allow_redirects=False,
                      stream=True) as response:
        if response.status_code != 200:
            raise RecoveryVerificationError(
                f"recovery fetch refused ({response.status_code})")
        chunks, total = [], 0
        for chunk in response.iter_content(chunk_size=vp._READ_CHUNK):
            total += len(chunk)
            if total > vp.MAX_VISUAL_BYTES:
                raise RecoveryVerificationError("recovery object too large")
            chunks.append(chunk)
        return b"".join(chunks)


def render_gbp_crop(raw):
    """Reproduce the deployed GBP crop recipe IN MEMORY: ImageOps.fit RGB
    1200x900 LANCZOS, JPEG quality 90 (gbp.crop_4x3, recipe
    'gbp-crop-4x3-jpeg90-v1')."""
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(raw)) as im:
        out = ImageOps.fit(im.convert("RGB"), (GBP_W, GBP_H), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _fetch_row(store, account_key, row_id):
    if not isinstance(row_id, str) or str(uuid.UUID(row_id)) != row_id:
        raise RecoveryVerificationError("exact canonical row UUID required")
    response = store._client().get(
        store._rest("content_calendar"),
        params={"select": "*", "id": f"eq.{row_id}",
                "gym_id": f"eq.{account_key}", "limit": "2"},
        headers=store._headers(), timeout=30)
    rows = response.json() if response.status_code < 400 else None
    if (not isinstance(rows, list) or len(rows) != 1
            or str(rows[0].get("id")) != str(row_id)
            or str(rows[0].get("gym_id")) != str(account_key)):
        raise RecoveryVerificationError("calendar row missing or cross-tenant")
    return rows[0]


def _rpc(store, name, args, timeout=60):
    try:
        response = store._client().post(
            store._rest(f"rpc/{name}"),
            headers=store._headers({"Content-Type": "application/json"}),
            json=args, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - unknown outcome; caller reconciles
        raise RecoveryStoreError(f"rpc {name} transport: {type(exc).__name__}")
    if response.status_code >= 400:
        code, message = "", ""
        try:
            err = response.json()
            if isinstance(err, dict):
                code, message = str(err.get("code") or ""), str(err.get("message") or "")
        except Exception:  # noqa: BLE001
            pass
        detail = f"SQLSTATE {code or 'unavailable'}"
        if code == "22023":
            raise RecoveryVerificationError(f"rpc {name} rejected proof: {detail}")
        if code == "23514":
            raise RecoveryVerificationError(f"rpc {name} refused: {detail}")
        raise RecoveryStoreError(f"rpc {name} failed ({response.status_code}): {detail}")
    try:
        return response.json()
    except Exception as exc:  # noqa: BLE001
        raise RecoveryStoreError(f"rpc {name} unparseable: {type(exc).__name__}")


def fetch_binding(store, account_key, row_id):
    """Read RPC: {"binding":..., "receipt":...} for the exact (gym, row).
    No binding -> RecoveryUnavailable (fail closed)."""
    data = _rpc(store, "historical_gbp_original_recovery_read",
                {"p_gym_id": str(account_key), "p_row_id": str(row_id)},
                timeout=30)
    if not isinstance(data, dict) or not isinstance(data.get("binding"), dict):
        raise RecoveryUnavailable("no configured recovery binding for this row")
    binding = data["binding"]
    if (str(binding.get("gym_id") or "") != str(account_key)
            or str(binding.get("row_id") or "") != str(row_id)):
        raise RecoveryVerificationError("recovery binding tenant/row mismatch")
    receipt = data.get("receipt")
    if receipt is not None and not isinstance(receipt, dict):
        raise RecoveryVerificationError("recovery receipt malformed")
    return binding, receipt


def _validate_binding_shape(binding):
    slug = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9_-]+", "-", str(binding.get("gym_id") or "").lower())).strip("-_")
    if not slug or binding.get("tenant_slug") != slug:
        raise RecoveryVerificationError("configured tenant slug mismatch")
    for field in ("tenant_slug", "source_origin", "source_url", "recipe"):
        if not binding.get(field):
            raise RecoveryVerificationError(f"recovery binding missing {field}")
    if binding["recipe"] != RECIPE:
        raise RecoveryVerificationError("recovery binding recipe mismatch")
    for field in ("source_sha256", "delivered_sha256"):
        if not _HEX64.match(str(binding.get(field) or "")):
            raise RecoveryVerificationError("recovery binding hash malformed")


def _require_validated_urls(binding, source_url, delivered_url):
    """Fail closed on untrusted tenant URLs BEFORE any byte reader runs."""
    for label, url in (("source", source_url), ("delivered", delivered_url)):
        if validate_hosted_url(url, binding["source_origin"],
                               binding["tenant_slug"]) is None:
            raise RecoveryVerificationError(
                f"recovery {label} URL is not a trusted tenant object")


def _verify_bytes(binding, raw, delivered, source_url, delivered_url):
    segment = validate_hosted_url(source_url, binding["source_origin"],
                                  binding["tenant_slug"])
    if segment is None:
        raise RecoveryVerificationError("recovery source URL is not a trusted tenant object")
    if not _sha1_segment_ok(raw, segment):
        raise RecoveryVerificationError("recovery raw content-address mismatch")
    if hashlib.sha256(raw).hexdigest() != binding["source_sha256"]:
        raise RecoveryVerificationError("recovery raw bytes mismatch")
    dsegment = validate_hosted_url(delivered_url, binding["source_origin"],
                                   binding["tenant_slug"])
    if dsegment is None or not _sha1_segment_ok(delivered, dsegment):
        raise RecoveryVerificationError("recovery delivered content-address mismatch")
    if hashlib.sha256(delivered).hexdigest() != binding["delivered_sha256"]:
        raise RecoveryVerificationError("recovery delivered bytes mismatch")
    if render_gbp_crop(raw) != delivered:
        raise RecoveryVerificationError("recovery GBP re-render mismatch")


def _target_eligible(row):
    return (
        str(row.get("account") or "") == "googlebusiness"
        and str(row.get("format") or "") in ("update", "photo")
        and str(row.get("status") or "") in ("pending", "coach_review")
        and str(row.get("variant_status") or "") == "active"
        and str(row.get("media_not_ready_reason") or "") == HOLD_REASON
        and all(key in row and row[key] is None for key in
                ("published_at", "late_post_id", "publish_claim_token", "approval_kind",
                 "approved_by", "approved_at", "approval_digest"))
        and not row.get("source_media_url")
        and not row.get("source_media_asset_id")
        and not row.get("drive_file_id")
    )


class Observation:
    """A fully verified, NOT YET APPLIED recovery. observe() never writes."""

    def __init__(self, binding, proof, fingerprint, before, receipt):
        self.binding = binding
        self.proof = proof
        self.fingerprint = fingerprint
        self.before = before
        self.receipt = receipt

    def as_dict(self):
        return {"proof": dict(self.proof), "request_fingerprint": self.fingerprint,
                "before_state": dict(self.before),
                "already_recovered": self.receipt is not None}


def observe(store, account_key, row, *, read_bytes=None):
    """READ-ONLY verification of one held GBP row against its operator
    binding. Downloads bounded bytes, re-renders the crop in memory and
    compares exact bytes. Returns an Observation; raises RecoveryError on any
    mismatch. Never writes."""
    _gate(account_key)
    if not _target_eligible(row):
        raise RecoveryUnavailable("row is not an eligible held GBP recovery target")
    binding, receipt = fetch_binding(store, account_key, row.get("id"))
    _validate_binding_shape(binding)
    if row_snapshot(row) != _jsonb_snapshot(binding.get("expected_before")):
        raise RecoveryVerificationError("target row drifted from the configured binding")
    historical = _fetch_row(store, account_key, binding["historical_row_id"])
    if _jsonb_snapshot(binding.get("historical_snapshot")) != row_snapshot(historical):
        raise RecoveryVerificationError("historical row drifted from the configured binding")
    if (historical.get("source_media_url") != binding["source_url"]
            and historical.get("image_url") != binding["source_url"]):
        raise RecoveryVerificationError("historical row does not reference the pinned original")
    reader = read_bytes or _default_reader
    _require_validated_urls(binding, binding["source_url"], row.get("image_url"))
    raw = vp._exact_bytes(binding["source_url"], reader, "recovery raw original")
    delivered = vp._exact_bytes(row.get("image_url"), reader, "recovery delivered")
    _verify_bytes(binding, raw, delivered, binding["source_url"], row.get("image_url"))
    fingerprint = request_fingerprint(account_key, row.get("id"), binding)
    return Observation(binding, proof_for(binding), fingerprint,
                       row_snapshot(row), receipt)


def _jsonb_snapshot(value):
    return row_snapshot(value)


def _check_receipt(receipt, account_key, row_id, request_id, fingerprint,
                   proof, binding):
    if (not isinstance(receipt, dict)
            or str(receipt.get("gym_id") or "") != str(account_key)
            or str(receipt.get("row_id") or "") != str(row_id)
            or str(receipt.get("request_id") or "") != str(request_id)
            or str(receipt.get("request_fingerprint") or "") != fingerprint
            or receipt.get("status") != "succeeded"):
        raise RecoveryVerificationError("recovery receipt identity mismatch")
    if receipt.get("proof") != proof:
        raise RecoveryVerificationError("recovery receipt proof mismatch")
    if receipt.get("binding") != binding or receipt.get("before_state") != binding.get("expected_before"):
        raise RecoveryVerificationError("recovery receipt binding mismatch")
    expected_after = dict(binding["expected_before"], source_media_url=binding["source_url"])
    actual_after = row_snapshot(receipt.get("after_state"))
    if {k:v for k,v in actual_after.items() if k != "updated_at"} != {k:v for k,v in expected_after.items() if k != "updated_at"}:
        raise RecoveryVerificationError("recovery receipt after snapshot mismatch")
    return receipt


def apply(store, account_key, row, request_id, *, read_bytes=None,
          observation=None):
    """APPLY one verified recovery through the single service-role RPC.
    Re-observes first (fresh proof, fresh snapshots). An UNKNOWN transport
    outcome is reconciled by re-reading the EXACT persisted receipt and
    comparing its immutable identity -- never by blind replay. Returns the
    persisted receipt dict."""
    _gate(account_key)
    fresh = _fetch_row(store, account_key, row.get("id"))
    if row_snapshot(fresh) != row_snapshot(row):
        raise RecoveryVerificationError("target changed before fresh observation")
    obs = observe(store, account_key, fresh, read_bytes=read_bytes)
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", request_id):
        raise RecoveryVerificationError("recovery request id missing")
    args = {"p_gym_id": str(account_key), "p_row_id": str(row.get("id")),
            "p_request_id": request_id,
            "p_request_fingerprint": obs.fingerprint,
            "p_proof": obs.proof}
    try:
        receipt = _rpc(store, "historical_gbp_original_recovery_apply", args)
    except RecoveryStoreError:
        # Unknown outcome: reconcile against the exact persisted receipt.
        binding, stored = fetch_binding(store, account_key, row.get("id"))
        if stored is None:
            raise
        receipt = stored
    _check_receipt(receipt, account_key, row.get("id"), request_id,
                   obs.fingerprint, obs.proof, obs.binding)
    current = _fetch_row(store, account_key, row["id"])
    if row_snapshot(current) != receipt["after_state"]:
        raise RecoveryStoreError("recovery committed outcome requires exact readback")
    return receipt


def reconcile(store, account_key, row_id, request_id, *, read_bytes=None):
    """Read only: certify the exact immutable request after response loss."""
    _gate(account_key)
    binding, receipt = fetch_binding(store, account_key, row_id)
    _check_receipt(receipt, account_key, row_id, request_id,
                   request_fingerprint(account_key, row_id, binding),
                   proof_for(binding), binding)
    current = _fetch_row(store, account_key, row_id)
    receipt_identity(store, account_key, current, read_bytes=read_bytes)
    return receipt


def receipt_identity(store, account_key, row, *, read_bytes=None):
    """media_guard original-source authority for an ALREADY-recovered row.

    Requires flag + allowlist + an immutable succeeded receipt whose frozen
    after snapshot equals the row's full CURRENT snapshot (including the
    recovered source_media_url) and whose binding/proof agree; then re-fetches
    the raw bytes and re-verifies SHA256, both content-address segments and
    the exact in-memory GBP re-render against the delivered bytes. Any drift,
    forgery, cross-tenant or unconfigured input refuses. Returns the ordinary
    original-identity shape {"sha256", "source_asset_id", "source_url"}."""
    _gate(account_key)
    binding, receipt = fetch_binding(store, account_key, row.get("id"))
    if receipt is None:
        raise RecoveryUnavailable("no persisted recovery receipt for this row")
    _validate_binding_shape(binding)
    historical = _fetch_row(store, account_key, binding["historical_row_id"])
    if row_snapshot(historical) != binding["historical_snapshot"]:
        raise RecoveryVerificationError("recovery historical snapshot drifted")
    proof = proof_for(binding)
    fingerprint = request_fingerprint(account_key, row.get("id"), binding)
    _check_receipt(receipt, account_key, row.get("id"),
                   receipt.get("request_id"), fingerprint, proof, binding)
    if row_snapshot(row) != _jsonb_snapshot(receipt.get("after_state")):
        raise RecoveryVerificationError("row drifted from the frozen recovery receipt")
    if row.get("source_media_url") != binding["source_url"]:
        raise RecoveryVerificationError("recovered source URL mismatch")
    reader = read_bytes or _default_reader
    _require_validated_urls(binding, binding["source_url"], row.get("image_url"))
    raw = vp._exact_bytes(binding["source_url"], reader, "recovery raw original")
    delivered = vp._exact_bytes(row.get("image_url"), reader, "recovery delivered")
    _verify_bytes(binding, raw, delivered, binding["source_url"], row.get("image_url"))
    return {"sha256": hashlib.sha256(raw).hexdigest(),
            "source_asset_id": None,
            "source_url": binding["source_url"]}


def main(argv=None):
    """Default: PLAN ONLY (no network). --observe: read-only verification.
    --apply: the separate explicit write step. Nothing runs automatically."""
    import argparse
    parser = argparse.ArgumentParser(prog="agent.historical_gbp_original_recovery")
    parser.add_argument("--gym", default="")
    parser.add_argument("--row-id", default="")
    parser.add_argument("--observe", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument("--request-id", default="")
    args = parser.parse_args(argv)
    if not enabled():
        print("historical GBP original recovery: DISABLED "
              f"({FLAG_ENV} unset/false); no action")
        return 0
    gyms = sorted(allowed_gyms())
    if not args.observe and not args.apply and not args.reconcile:
        print(f"plan only: enabled, allowlisted gyms={gyms or '[]'}; "
              "pass --observe (read-only) or --apply --request-id <id> "
              "(explicit operator step). No network, no writes by default.")
        return 0
    if sum((args.observe, args.apply, args.reconcile)) > 1:
        parser.error("choose one explicit observe or apply step")
    if not args.gym or not args.row_id or ((args.apply or args.reconcile) and not args.request_id):
        parser.error("exact gym/row and apply request ID required")
    from .portal_calendar_store import SupabaseCalendarStore
    store = SupabaseCalendarStore()
    row = _fetch_row(store, args.gym, args.row_id)
    if args.observe:
        result = observe(store, args.gym, row).as_dict()
    else:
        receipt = (reconcile(store, args.gym, args.row_id, args.request_id)
                   if args.reconcile else apply(store, args.gym, row, args.request_id))
        result = {"status": receipt["status"], "gym_id": args.gym,
                  "row_id": args.row_id, "request_id": args.request_id}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
