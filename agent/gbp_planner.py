"""
GBP planner lane (Phase 3). Plans a month of Google Business posts for one gym from
its OWN library + sources, in GBP copy style, and writes them as PENDING rows to
content_calendar via the existing Echo write path (insert_rows). Nothing publishes;
the owner still taps Approve in the portal.

Reuses the FB/IG machinery WITHOUT modifying it: client_content source rotation +
pick_image for the photo, gbp.crop_4x3 for the 1200x900 crop, and a GBP-specific LLM
caption (different system prompt: 80-char hook, city, no hashtags, no phone, CTA carries
the ask). Every caption clears gbp.caption_issues (A+) or the slot is skipped — A+ or
nothing, never a sub-par GBP post.

Offer/CTA source (discovered 2026-08-15): the live front-end offer NAME comes from
onboarding_intake.offers (jsonb array); the redeem/CTA URL from onboarding_intake
.ghl_link (the GHL funnel). There is NO CTA-type override column and NO coupon / offer-
window / terms source — CTA defaults to LEARN_MORE and the offer window is a planner
default (7-14 days, validator cap 30). See PROGRESS.md for the flagged gaps. Never
fabricate an offer: no offer name or no redeem URL -> the OFFER slot is skipped.
"""

import hashlib
import os
import tempfile
import uuid
from datetime import date, timedelta
from pathlib import Path

from . import client_content, client_sources, config, gbp, gym_media_selector, media_host, rotation
from .content_categories import filter_platform_copy
from .drafter import _call_llm_caption, _output_claims_cleared

# §5.1 cadence per connected location per month
CADENCE = {"STANDARD": 8, "OFFER": 1, "EVENT_MAX": 2, "PHOTO": 4}
OFFER_WINDOW_DAYS = 10          # planner default within the 7-14 band (validator cap 30)

GBP_SYSTEM = (
    "You are a local-SEO copywriter for a boutique gym. Write ONE Google Business "
    "Profile post caption for people searching 'gym near me' on Google Search and Maps "
    "(strangers, not followers).\n"
    "HARD RULES:\n"
    "- The first 80 characters carry the whole message: lead with the outcome AND the "
    "city. Google truncates there in Search.\n"
    "- 150 to 300 characters total.\n"
    "- Name the city or neighborhood once, naturally.\n"
    "- NO hashtags. NO phone numbers (a CALL button handles that). NO emojis spam.\n"
    "- No em dashes, en dashes, or hyphens used as dashes.\n"
    "- Draw ONLY from the brand voice doc and the fact provided. Invent nothing: no "
    "stats, prices, offers, or claims not in the sources.\n"
    "- Do NOT write a CTA or a link in the body; the CTA button is separate.\n"
    "- Output ONLY the caption text. No labels, no headers, no quotes."
)


def generate_gbp_caption(fact_text, voice, city):
    """A GBP-style caption grounded in one approved fact + the gym's voice doc, city
    named. Returns a caption that PASSES gbp.caption_issues(city), or None when the LLM
    is unavailable / the output cannot be made A+ (caller skips the slot: A+ or nothing).
    Figure-fabrication gated exactly like the FB/IG SB7 path."""
    from .drafter import _strip_llm_scaffold

    def _attempt(user_prompt):
        try:
            cap = _strip_llm_scaffold(_call_llm_caption(GBP_SYSTEM, user_prompt) or "")
            return filter_platform_copy(cap).strip()
        except Exception as exc:  # noqa: BLE001
            print(f"[gbp-planner] caption LLM failed: {type(exc).__name__}")
            return None

    voice_raw = getattr(voice, 'raw', '') or ''
    base_user = (f"BRAND VOICE DOC:\n{voice_raw}\n\n"
                 f"CITY: {city}\n\nTODAY'S FACT (the only source of specifics):\n{fact_text}\n\n"
                 "Write the GBP caption now.")
    cap = _attempt(base_user)
    if cap and gbp.caption_issues(cap, city=city):
        # City not named — retry once with an explicit reminder
        retry_user = (base_user +
                      f"\n\nIMPORTANT: Your previous attempt did not name the city. "
                      f"The caption MUST include '{city}' to rank on Google Maps. "
                      "Start the caption with the city or include it in the first sentence.")
        cap = _attempt(retry_user)

    if not cap:
        return None
    if not _output_claims_cleared(cap, voice, fact_text):
        print("[gbp-planner] caption carried an unapproved figure; skipping slot")
        return None
    if gbp.caption_issues(cap, city=city):
        print(f"[gbp-planner] caption not A+ ({gbp.caption_issues(cap, city=city)[0]}); "
              "skipping slot")
        return None
    return cap


def resolve_offer(offers_json, ghl_link):
    """(offer_name, offer_dict) from the gym record, or (None, None) to SKIP the OFFER
    slot. offer_dict is the content_calendar.gbp_offer payload (redeemOnlineUrl only —
    coupon/terms have no source and are omitted, never invented). Requires BOTH a real
    offer name AND a redeem URL; either missing -> skip (never a dead offer)."""
    def _name_of(item):
        # a jsonb offer element may be a plain string or an object; pull a real name
        # field from an object, never stringify the dict into the caption.
        if isinstance(item, dict):
            return str(item.get("name") or item.get("title") or item.get("label")
                       or "").strip()
        return str(item or "").strip()

    name = ""
    if isinstance(offers_json, list) and offers_json:
        name = _name_of(offers_json[0])
    elif isinstance(offers_json, str):
        name = offers_json.strip()
    url = (ghl_link or "").strip()
    if not name or not url:
        return None, None
    return name, {"redeemOnlineUrl": url}


def _offer_window(start):
    """gbp_event.schedule offer window: OFFER_WINDOW_DAYS from `start`."""
    end = start + timedelta(days=OFFER_WINDOW_DAYS)
    return {"schedule": {"startDate": start.isoformat(), "endDate": end.isoformat()}}


def _cropped_image(account_key, image, day_key):
    """Crop the picked library photo to 1200x900 at PLANNING time, host it, return
    (hosted url, local crop path) — the exact pixels the owner approves + that publish.
    (None, None) on failure."""
    try:
        cache = os.path.join(rotation._cache_dir(None) if hasattr(rotation, "_cache_dir")
                             else "/tmp", "gbp_crops")
    except Exception:
        cache = "/tmp/gbp_crops"
    os.makedirs(cache, exist_ok=True)
    out = os.path.join(cache, f"{account_key}_{os.path.basename(image.path)}_gbp.jpg")
    # CROP CACHE (2026-09-02): re-cropping a photo whose 1200x900 output is already on
    # disk and newer than its source produces byte-identical pixels, so skip the work.
    # This matters now that the mirror crops one photo per FEED POST on every build
    # (~268 fleet-wide) rather than a handful per planned month. A source edited after
    # its crop still re-crops, because the mtime comparison fails.
    try:
        fresh = (os.path.isfile(out) and os.path.getsize(out) > 0
                 and os.path.getmtime(out) >= os.path.getmtime(image.path))
    except OSError:
        fresh = False
    if not fresh:
        try:
            gbp.crop_4x3(image.path, out)
        except Exception as exc:  # noqa: BLE001
            print(f"[gbp-planner] crop failed for {image.path}: {type(exc).__name__}")
            return None, None
    if config.hosting_enabled():
        hosted = media_host.host_media(out, account_key)
        if hosted:
            return hosted, out
    return None, None


def _cropped_image_url(account_key, image, day_key):
    """Hosted 1200x900 crop URL only (legacy callers). See _cropped_image."""
    return _cropped_image(account_key, image, day_key)[0]


# ---- global visual lineage (2026-10-03, GBP provenance package) ------------------
# Blake's global media guard: no reused photos, Drive photos first, and EXACT
# source/delivered lineage on every staged visual row. The GBP planner crops and
# re-hosts every photo, so the hosted crop URL alone is NOT lineage: the staged row
# must name the trusted exact RAW source URL, and a transformed write must carry
# render evidence bound to the actual source/delivered bytes. When the global
# prepared writer is enabled (AGENT_VISUAL_GLOBAL_WRITER_PREP), a GBP image that
# cannot establish BOTH is HELD (slot skipped, never staged) rather than written
# with a cropped URL masquerading as its own source. Default-off behavior is
# byte-for-byte unchanged: no extra hosting, no new row keys.
def _global_writer_enabled():
    """True when the global prepared writer guards calendar writes. Unknown state
    (import/flag read failure) fails CLOSED for transformed GBP writes."""
    try:
        from . import visual_writer_prepare
        return bool(visual_writer_prepare.enabled())
    except Exception:  # noqa: BLE001
        return True


def _render_evidence_dict(source_url, delivered_url, source_path, delivered_path, *,
                          tenant="", source_asset_id=""):
    """Render evidence bound to the ACTUAL local source/delivered byte objects, in the
    exact contract visual_writer_prepare._prepare_source_rendition verifies (it re-reads
    both URLs and rejects any fingerprint/length mismatch). None when bytes unreadable —
    never an invented attestation."""
    try:
        source = Path(source_path).read_bytes()
        delivered = Path(delivered_path).read_bytes()
    except (OSError, TypeError, ValueError):
        return None
    if not source or not delivered:
        return None
    # Carry a replayable candidate through the existing evidence side channel.
    # It remains unverified and cannot replace owner registry/manifest receipts.
    try:
        from .gym_media_builder import still_materialization_observation
        observation = still_materialization_observation(
            source, delivered, delivered_url, tenant=tenant,
            source_asset_id=source_asset_id, source_url=source_url,
            image_name="gbp_crop_4x3")
    except Exception:  # unsupported input/cache drift/readback -> hold, never attest
        return None
    source_hash = hashlib.md5(source).hexdigest()
    delivered_hash = hashlib.md5(delivered).hexdigest()
    return {
        "operation": "render",
        "materialization_observation": observation,
        "source_exact_url": source_url,
        "delivered_exact_url": delivered_url,
        "source_fingerprint": "md5:" + source_hash,
        "delivered_fingerprint": "md5:" + delivered_hash,
        "source_byte_length": len(source),
        "delivered_byte_length": len(delivered),
        # The identifier is persisted with the owner receipts. It includes the
        # exact local transform's two observed hashes and a per-render UUID, so
        # it is both bound to those bytes and unique when a crop is repeated.
        "evidence_ref": f"gbp_planner:render:{source_hash}:{delivered_hash}:{uuid.uuid4()}",
        "observed_by": "gbp_planner",
        "rendered_by": "gbp_planner",
    }


def _url_bytes_match(url, path):
    """True only when the exact bytes served at url equal the local file's bytes.
    False/None on any read failure — a hosted URL we cannot byte-verify never
    attests a source."""
    try:
        local = Path(path).read_bytes()
    except (OSError, TypeError, ValueError):
        return False
    try:
        import requests
        with requests.get(url, timeout=(5, 30), allow_redirects=False,
                          stream=True) as response:
            if response.status_code != 200:
                return False
            chunks, total = [], 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                chunks.append(chunk)
                total += len(chunk)
                if total > 128 * 1024 * 1024:
                    return False
            return b"".join(chunks) == local
    except Exception:  # noqa: BLE001
        return False


def _transformed_gbp_image(account_key, image, day_key, *, source_url=None,
                           source_asset_id=""):
    """Crop+host one GBP photo WITH exact source lineage.

    Returns {"url", ...} plus, when the global prepared writer is enabled,
    "source_media_url" (trusted exact RAW source URL, never the cropped URL) and
    "render_evidence" (byte-bound, verified below and re-verified by the writer).
    Returns None — HOLD, the slot is skipped — when the guard is on and either
    cannot be established. With the guard off this is exactly the historical
    crop+host (no extra hosting, no new keys)."""
    url, out_path = _cropped_image(account_key, image, day_key)
    if not url:
        return None
    if source_url:
        # An externally produced source object (e.g. a cached Drive rendition) is
        # only trusted when its served bytes equal the exact local source bytes we
        # cropped from. Mismatch/unreadable -> hold, never attest stale bytes.
        if not _url_bytes_match(source_url, image.path):
            return None
    else:
        if not config.hosting_enabled():
            return None
        source_url = media_host.host_media(str(image.path), account_key)
        if not source_url:
            return None
    evidence = _render_evidence_dict(source_url, url, image.path, out_path,
                                     tenant=account_key, source_asset_id=source_asset_id)
    if evidence is None:
        return None
    return {"url": url, "source_media_url": source_url, "render_evidence": evidence}


def _drive_photo_candidate(account_key, day_key, used_ids):
    """Materialize one unused approved Drive photo without consuming it yet.

    The selector owns tenant isolation, review eligibility, and global once-used
    enforcement. The caller stamps usage only after its row is durably inserted.
    """
    if str(account_key or "").startswith("lasso"):
        return None
    if not (config.gym_drive_stage_enabled()
            and config.gym_drive_connect_active_for(account_key)):
        return None
    try:
        from . import gym_media_index, gym_media_selector
        from .integrations.drive_client import DriveClient
        media_store = gym_media_index.default_store()
        drive = DriveClient()
        if not media_store.available() or not drive.available():
            return {"hold": True}
        base = gym_media_selector.base_gym_key(account_key)
        list_sources = getattr(media_store, "list_sources", None)
        if not callable(list_sources):
            return {"hold": True}
        sources = list_sources(base) or []
        ready = [s for s in sources
                 if str(s.get("kind") or "") == "gym_drive"
                 and s.get("active") is not False
                 and not s.get("revoked_externally")
                 and str(s.get("sync_status") or "").lower() == "ready"
                 and s.get("sync_finished_at")]
        if not ready:
            return {"hold": True}
        assets = media_store.list_assets(base)
        # CROSS-GYM SOURCE GUARD: the snapshot must expose the authoritative
        # same-gym active source rows (already read above) or the selector fails
        # closed on unproven evidence and no Drive photo can ever be picked.
        class Snapshot:
            def available(self):
                return True
            def list_assets(self, _base):
                return assets
            def list_sources(self, _base, include_inactive=False):
                return sources
        asset = gym_media_selector.pick_media(
            base, kind_preference="photo", store=Snapshot(),
            exclude_ids=tuple(used_ids))
        if asset is None:
            return None
        with tempfile.TemporaryDirectory(prefix="gbp_drive_") as work:
            raw = Path(work) / os.path.basename(
                asset.get("title") or f"{asset['id']}.jpg")
            drive.download(asset["id"], raw)
            source = raw
            source_url = None
            if gym_media_index.needs_rendition(asset):
                rendition_url, _ = gym_media_index.ensure_rendition(
                    asset, raw, store=media_store)
                if not rendition_url:
                    return None
                source = Path(work) / f"{raw.stem}.jpg"
                gym_media_index.heic_to_jpeg(raw, source)
                # The exact RAW source of the GBP crop is the hosted rendition
                # object (the Drive original is never touched); it must still
                # byte-verify against the local converted bytes before it may
                # be named as the row's source.
                source_url = rendition_url

            class DrivePhoto:
                path = str(source)
                media_type = "image"

            if _global_writer_enabled():
                prov = _transformed_gbp_image(account_key, DrivePhoto(), day_key,
                                              source_url=source_url,
                                              source_asset_id=str(asset["id"]))
            else:
                url = _cropped_image_url(account_key, DrivePhoto(), day_key)
                prov = {"url": url} if url else None
        if not prov:
            return None
        used_ids.add(str(asset["id"]))
        pick = {"url": prov["url"], "kind": "drive", "asset": asset,
                "base": base, "store": media_store, "day_key": day_key}
        if prov.get("source_media_url"):
            pick["source_media_url"] = prov["source_media_url"]
            pick["render_evidence"] = prov["render_evidence"]
        return pick
    except Exception as exc:  # noqa: BLE001 - GBP falls through to local media
        print(f"[gbp-planner] Drive photo failed for {account_key} on {day_key}: "
              f"{type(exc).__name__}")
        return {"hold": True}


def _drive_claim_id(base, asset):
    from . import gym_media_selector
    return gym_media_selector.drive_content_claim_id(base, asset)


def _claim_drive_pick(pick, account_key):
    """Atomically reserve one Drive asset across concurrent GBP planners."""
    from . import gym_media_selector
    try:
        claim_id = gym_media_selector.claim_drive_content(
            pick["base"], pick["asset"], pick["store"])
    except Exception:
        return False
    if claim_id is None:
        return False
    claim_account = f"{pick['base']}_gbp"
    pick["claim_id"] = claim_id
    pick["claim_account"] = claim_account
    return True


def _release_drive_claim(pick):
    claim_id = (pick or {}).get("claim_id")
    if not claim_id:
        return True
    from . import db
    db.socialapi_claim_release(claim_id, pick["claim_account"])
    pick.pop("claim_id", None)
    pick.pop("claim_account", None)
    return True


def _complete_drive_claim(pick):
    from . import db
    if pick.get("journal_entry"):
        # An armed retry may complete only its original exact claim; never
        # overwrite a completed claim or a foreign in-flight asset binding.
        asset_id = str(pick["asset"]["id"])
        with db.connect() as conn:
            conn.execute(
                "UPDATE socialapi_claims SET status='done',post_id=? "
                "WHERE draft_id=? AND account_key=? AND status='in_flight' "
                "AND (post_id IS NULL OR post_id='' OR post_id=?)",
                (asset_id, pick["claim_id"], pick["claim_account"], asset_id))
            conn.commit()
    else:
        db.socialapi_claim_done(
            pick["claim_id"], pick["claim_account"], str(pick["asset"]["id"]))

# ---- armed remote Drive use caller (AGENT_REMOTE_DRIVE_USE_CAS_ENABLED) -----
# Default OFF. When armed, every Drive row carries a durable source-bound use
# identity in the local gbp_drive_use_journal BEFORE the calendar POST: exact
# gym/logical id, frozen authoritative zero-use asset + source snapshots, epoch,
# date, the proposed row, and one stable use UUID. Write intent is persisted for
# every Drive row before the batch send; any journal hold fails closed before
# the POST. Only exact landed rows are consumed, through the remote atomic CAS
# with the SAME UUID and full snapshots, and a Drive claim completes only after
# an authoritative receipt is verified. When the flag is off every path below
# is inert and the legacy stamp flow is byte-for-byte unchanged.


def _remote_drive_cas_enabled():
    """True only when the remote Drive use CAS is explicitly armed. A flag read
    failure holds when the environment explicitly arms the lane."""
    try:
        from . import remote_drive_use
        return bool(remote_drive_use.enabled())
    except Exception:  # noqa: BLE001
        if os.environ.get("AGENT_REMOTE_DRIVE_USE_CAS_ENABLED", "").strip().lower() in ("true", "1", "yes", "on"):
            raise RuntimeError("remote Drive use flag unavailable while armed") from None
        return False


def _drive_use_epoch_id(base):
    """The deployment's explicit mutation epoch for this gym (the same binding
    remote_drive_use.apply enforces). Raises when unconfigured: the caller holds."""
    from . import local_inventory_mutation as lim
    cfg = lim.configured(base, Path(config.LIBRARY_PATH) / base)
    return cfg.epoch_id


def _authoritative_drive_snapshots(pick):
    """Exact authoritative asset + source rows for one armed Drive pick, or None.

    The armed lane never trusts the picker-time dict: it re-reads the exact
    current rows from the media store and requires the eligible zero-use proof
    the remote CAS contract binds (same-gym, active gym_drive source, eligible,
    never used). Any missing/ambiguous snapshot is None — fail closed, the row
    is held before the POST, never guessed."""
    store = pick.get("store")
    base = pick.get("base")
    asset = pick.get("asset") or {}
    get_asset = getattr(store, "get_asset", None)
    get_source = getattr(store, "get_source", None)
    if not callable(get_asset) or not callable(get_source) or not base:
        return None
    try:
        asset_row = get_asset(str(asset.get("id") or ""))
        source_row = (get_source(str(asset_row.get("source_id") or ""))
                      if isinstance(asset_row, dict) else None)
    except Exception:  # noqa: BLE001 - unreadable snapshot is unavailable proof
        return None
    if not isinstance(asset_row, dict) or not isinstance(source_row, dict):
        return None
    if (asset_row.get("gym_id") != base or source_row.get("gym_id") != base
            or asset_row.get("used_count") != 0
            or asset_row.get("last_used_at") is not None
            or asset_row.get("eligible") is not True
            or asset_row.get("excluded_by_coach") is not False
            or source_row.get("kind") != "gym_drive"
            or source_row.get("active") is not True
            or not str(asset_row.get("content_hash") or "")):
        return None
    return asset_row, source_row


def _prepare_drive_use_journal(portal_gym_key, row, pick):
    """Freeze the durable source-bound use identity for one armed Drive row.

    Returns the journal entry (stable UUID; a same-identity retry resumes the
    existing entry) or None — HOLD, fail closed before the POST — when the
    claim, logical id, epoch, or authoritative snapshots are unavailable, or
    the journal itself holds (e.g. an identity conflict under the same logical
    post id)."""
    from . import gbp_drive_use_journal as journal
    from .portal_calendar_store import prepare_calendar_caption_payload
    # Freeze the exact deterministic caption/hold payload the store will write.
    # Mutate this proposed row so the subsequent batch insert carries it too.
    row.update(prepare_calendar_caption_payload(row))
    asset = pick.get("asset") or {}
    asset_id = str(asset.get("id") or "")
    if not asset_id or not pick.get("claim_id"):
        return None
    existing = row.get("source_media_asset_id")
    if existing is not None and str(existing) != asset_id:
        return None
    # The journal contract binds the asset id ON the proposed row, so the exact
    # landed readback must carry it too.
    row["source_media_asset_id"] = asset_id
    snapshots = _authoritative_drive_snapshots(pick)
    if snapshots is None:
        return None
    asset_row, source_row = snapshots
    try:
        epoch_id = _drive_use_epoch_id(pick["base"])
    except Exception:  # noqa: BLE001 - no explicit epoch: hold, never invent one
        return None
    try:
        logical = _ensure_logical_post_id(row)
    except ValueError:
        return None
    request = dict(
        gym_id=pick["base"], logical_post_id=logical,
        claim_id=str(pick["claim_id"]), post_date=row["post_date"],
        content_hash=str(asset_row.get("content_hash") or ""),
        asset_id=asset_id, source_id=str(source_row.get("id") or ""),
        calendar_row=dict(row), payload=dict(row),
        asset_before=asset_row, source_before=source_row, epoch_id=epoch_id)
    try:
        return journal.prepare(request)
    except journal.JournalHold:
        return None


def _recorded_remote_receipt(use_id):
    """The durably recorded authoritative remote receipt for one use UUID, read
    back from the remote_drive_use attempt ledger. None when missing, not yet
    confirmed, or unreadable — the caller holds rather than trusting counters."""
    import json as _json
    import sqlite3 as _sqlite3
    from . import db as _db
    try:
        conn = _sqlite3.connect(str(_db.db_path()), timeout=30)
        try:
            row = conn.execute(
                "SELECT state, receipt FROM remote_drive_use_attempt"
                " WHERE use_id=?", (str(use_id),)).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None
    if not row or row[0] != "confirmed" or not row[1]:
        return None
    try:
        receipt = _json.loads(row[1])
    except Exception:  # noqa: BLE001
        return None
    return receipt if isinstance(receipt, dict) else None


def _settle_armed_drive_landing(portal_gym_key, row, pick, persisted_row, log, *,
                              forward_binding_reader=None, manifest_evidence=None):
    """Consume one exact landed armed Drive row to a verified receipt.

    Sequence: confirm_landed (exact same logical row + asset + payload) ->
    begin_consumption -> remote atomic Drive use with the SAME journal UUID and
    the frozen full snapshots -> authoritative receipt readback ->
    confirm_receipt -> complete the Drive claim. Any hold leaves the journal
    entry unsettled and the claim in-flight (non-reofferable); nothing is
    released on an unknown outcome. Resumable: a retry of the same logical post
    replays the recorded receipt with the same UUID and never double-consumes.
    Returns True only when the receipt is durably confirmed and the claim done.
    """
    from . import gbp_drive_use_journal as journal
    entry = pick.get("journal_entry")
    if not entry:
        log(f"{portal_gym_key}: armed Drive row landed without a journal entry; "
            "holding claim")
        return False
    use_id = entry["use_id"]
    try:
        current = journal.get(use_id)
    except journal.JournalHold:
        return False
    if current is None:
        return False
    # GBP staged-journal binding (2026-10-09, flag AGENT_GBP_STAGED_JOURNAL,
    # default OFF): with the binding armed, a staged forward-reservation
    # CANDIDATE row is never a landed placement and never consumes remote
    # Drive use. Settlement of ANY remote-consuming state requires an exact
    # ACTIVE member readback PLUS exactly one durable batch binding whose
    # tenant, exact frozen member row and terminal `finalized` proof all
    # match. A missing, conflicting, cross-tenant or non-finalized binding
    # holds with ZERO remote calls; an ambiguous flag holds before any remote
    # mutation. The legacy explicit-OFF path below is byte-identical.
    from .portal_calendar_store import gbp_staged_journal_flag
    staged_flag = gbp_staged_journal_flag()
    if staged_flag is None:
        log(f"{portal_gym_key}: staged journal flag is ambiguous; holding "
            f"remote use for {use_id} before any remote mutation")
        return False
    state = current["state"]
    if staged_flag and state in ("write_intent", "unknown_result",
                                 "confirmed_landed", "consumption_pending"):
        if not isinstance(persisted_row, dict):
            # Armed recovery with no exact active persisted row (readback
            # error or missing) makes ZERO remote calls.
            log(f"{portal_gym_key}: armed settlement lacks an exact persisted "
                f"row readback; holding remote use for {use_id}")
            return False
        if (persisted_row.get("variant_status") != "active"
                or persisted_row.get("media_not_ready_reason")
                == "forward_reservation_staged"):
            log(f"{portal_gym_key}: staged candidate is not a landed placement; "
                f"holding remote use for {use_id}")
            return False
        row_id = persisted_row.get("id")
        try:
            binding_reader = forward_binding_reader or journal.forward_stage_for_member
            bound = (binding_reader(str(row_id))
                     if row_id else None)
        except journal.JournalHold as hold:
            log(f"{portal_gym_key}: member batch binding hold for {use_id}: "
                f"{hold}")
            return False
        if bound is None:
            log(f"{portal_gym_key}: no durable batch binding for the landed "
                f"member row; holding remote use for {use_id}")
            return False
        if bound["state"] != "finalized":
            log(f"{portal_gym_key}: batch {bound['batch_id']} not finalized; "
                f"holding remote use for {use_id}")
            return False
        if not journal.forward_finalized_proof_valid(bound):
            log(f"{portal_gym_key}: batch {bound['batch_id']} lacks exact "
                f"terminal proof; holding remote use for {use_id}")
            return False
        if not journal.forward_stage_tenant_matches(current, bound):
            log(f"{portal_gym_key}: cross-tenant batch binding "
                f"({bound['tenant_id']}); holding remote use for {use_id}")
            return False
        manifest_digest = None
        if persisted_row.get("render_manifest_digest") is not None:
            if not journal.forward_manifest_evidence_matches(
                    current, persisted_row, bound, manifest_evidence):
                log(f"{portal_gym_key}: trusted manifest/reservation proof unavailable; "
                    f"holding remote use for {use_id}")
                return False
            manifest_digest = manifest_evidence["snapshot"]["render_manifest_digest"]
        member_row = journal.forward_stage_member_row(bound, str(row_id))
        # Finalization restores active/unheld. Trusted staged preparation may
        # fill the manifest digest verified above. Every other frozen member
        # field must match, with only explicit safe server defaults.
        expected_member = dict(member_row) if isinstance(member_row, dict) else None
        if expected_member is not None:
            if (expected_member.get("variant_status") not in (None, "candidate")
                    or expected_member.get("media_not_ready_reason") is not None):
                expected_member = None
            else:
                expected_member["variant_status"] = "active"
                expected_member["media_not_ready_reason"] = None
        proof = current.get("landed_proof")
        if (expected_member is None
                or str(member_row.get("logical_post_id") or "")
                != current["logical_post_id"]
                or not journal._landed_row_matches(persisted_row, expected_member,
                                                   manifest_digest=manifest_digest)
                or not journal.forward_landed_entry_matches(current, persisted_row, bound,
                                                            manifest_digest=manifest_digest)
                or (state in ("confirmed_landed", "consumption_pending")
                    and (not isinstance(proof, dict)
                         or proof.get("use_id") != use_id
                         or proof.get("logical_post_id") != current["logical_post_id"]
                         or not isinstance(proof.get("calendar_row"), dict)
                         or proof.get("asset_id") != current["asset_id"]
                         or proof.get("content_hash") != current["content_hash"]
                         or not journal._landed_row_matches(
                             persisted_row, proof.get("calendar_row") or {})))):
            log(f"{portal_gym_key}: landed row is not the exact frozen member "
                f"of batch {bound['batch_id']}; holding remote use for "
                f"{use_id}")
            return False
    if state in ("write_intent", "unknown_result"):
        if persisted_row is None:
            # An unseen row is never settled from counters or absence.
            return False
        evidence = {"calendar_row": persisted_row,
                    "asset_id": str((pick.get("asset") or {}).get("id") or ""),
                    "content_hash": str(current.get("content_hash") or "")}
        if manifest_evidence is not None:
            evidence["forward_manifest_evidence"] = manifest_evidence
        try:
            journal.confirm_landed(use_id, evidence)
            journal.begin_consumption(use_id)
        except journal.JournalHold as hold:
            log(f"{portal_gym_key}: armed Drive landing hold for {use_id}: {hold}")
            return False
        state = "consumption_pending"
    elif state == "confirmed_landed":
        try:
            journal.begin_consumption(use_id)
        except journal.JournalHold:
            return False
        state = "consumption_pending"
    if state == "consumption_pending":
        try:
            from . import gym_media_selector
            gym_media_selector.stamp_use(
                pick["asset"], pick["base"], pick["day_key"], store=pick["store"],
                use_id=use_id, asset_row=current["asset_before"],
                source_row=current["source_before"])
        except Exception as exc:  # noqa: BLE001 - unknown remote outcome: hold
            log(f"{portal_gym_key}: armed remote Drive use held for {use_id}: "
                f"{type(exc).__name__}")
            return False
        receipt = _recorded_remote_receipt(use_id)
        if receipt is None:
            log(f"{portal_gym_key}: no authoritative receipt recorded for {use_id}")
            return False
        try:
            journal.confirm_receipt(use_id, receipt)
        except journal.JournalHold as hold:
            log(f"{portal_gym_key}: armed Drive receipt hold for {use_id}: {hold}")
            return False
    elif state == "claim_done":
        try:
            journal.confirm_claim_done(use_id)
            return True
        except journal.JournalHold:
            return False
    elif state != "receipt_confirmed":
        # prepared/abandoned here means the caller violated the send protocol.
        return False
    try:
        # Never create or overwrite a mismatched claim during crash recovery.
        from . import db
        with db.connect() as conn:
            claim = conn.execute(
                "SELECT status,post_id FROM socialapi_claims WHERE draft_id=? AND account_key=?",
                (current["claim_id"], current["gym_id"] + "_gbp")).fetchone()
        if claim is None or (claim[0] == "done" and claim[1] != current["asset_id"]):
            return False
        if claim[0] not in ("in_flight", "done"):
            return False
        _complete_drive_claim(pick)
        journal.confirm_claim_done(use_id)
    except Exception as exc:  # noqa: BLE001 - claim remains non-reofferable
        log(f"{portal_gym_key}: armed Drive claim completion failed for {use_id}: "
            f"{type(exc).__name__}")
        return False
    return True


def _forward_recovery_member_matches(entry, persisted, bound, *, provisional=False,
                                     manifest_evidence=None):
    """Match frozen placement; provisional discovery never authorizes CAS.

    Preparation may add a digest after staging. Discovery checks its shape
    while preserving every other field. Strict matching requires the exact
    trusted snapshot and current active reservation proof for that digest.
    """
    from . import gbp_drive_use_journal as journal
    if (not isinstance(bound, dict) or not journal.forward_stage_tenant_matches(entry, bound)
            or persisted.get("gym_id") != entry["gym_id"]
            or persisted.get("variant_status") != "active"
            or persisted.get("media_not_ready_reason") is not None):
        return False
    member = journal.forward_stage_member_row(bound, str(persisted.get("id") or ""))
    if (not isinstance(member, dict)
            or member.get("logical_post_id") != entry["logical_post_id"]
            or member.get("variant_status") not in (None, "candidate")
            or member.get("media_not_ready_reason") is not None):
        return False
    expected = dict(member, variant_status="active", media_not_ready_reason=None)
    manifest_digest = persisted.get("render_manifest_digest")
    if manifest_digest is not None and not provisional:
        if not journal.forward_manifest_evidence_matches(entry, persisted, bound, manifest_evidence):
            return False
    return (journal._landed_row_matches(persisted, expected, manifest_digest=manifest_digest)
            and journal.forward_landed_entry_matches(entry, persisted, bound,
                manifest_digest=manifest_digest, provisional=provisional))


def _forward_recovery_manifest_evidence(store, entry, persisted, bound):
    """Read exact manifest and reservation authority via existing PG RPCs.

    No new DSN, mutable registry read, row-derived source hash or caller
    manifest is used. The frozen observation binds the source SHA request;
    PG independently validates it against the current active reservation.
    """
    from . import gbp_drive_use_journal as journal
    from .portal_calendar_store import _SNAPSHOT_RPC
    if persisted.get("render_manifest_digest") is None:
        return None
    if not journal.forward_finalized_proof_valid(bound):
        raise ValueError("terminal batch proof required before manifest read")
    sha = journal.forward_stage_source_sha256(bound, str(persisted.get("id") or ""))
    if sha is None:
        raise ValueError("frozen source SHA unavailable")
    authority = getattr(store, "_s", store)
    snapshot = authority._reservation_rpc(_SNAPSHOT_RPC,
        {"p_calendar_row_id": persisted["id"]}, timeout=30)
    proof = authority.forward_reservation_proof(persisted["id"], sha)
    evidence = {"snapshot": snapshot, "reservation_proof": proof}
    if not journal.forward_manifest_evidence_matches(entry, persisted, bound, evidence):
        raise ValueError("trusted manifest/reservation binding mismatch")
    return evidence


def _recover_armed_drive_uses(portal_gym_key, account_gen_key, store, log):
    """Recover frozen uses before selection. Never resend a calendar POST."""
    from . import gbp_drive_use_journal as journal, gym_media_index
    base = gym_media_selector.base_gym_key(account_gen_key)
    entries = [e for e in journal.unsettled() if e["gym_id"] == base]
    if not entries:
        return None
    media_store = gym_media_index.default_store()
    held = []
    for entry in entries:
        row = entry["calendar_row"]
        pick = dict(kind="drive", base=base, asset=entry["asset_before"],
                    store=media_store, day_key=entry["post_date"],
                    claim_id=entry["claim_id"], claim_account=f"{base}_gbp",
                    journal_entry=entry)
        persisted = None
        readback_states = {"write_intent", "unknown_result"}
        from .portal_calendar_store import gbp_staged_journal_flag
        if gbp_staged_journal_flag():
            # Armed: recovery of landed/consumption states must also re-read
            # the exact active persisted row before any remote consumption.
            readback_states |= {"confirmed_landed", "consumption_pending"}
        if entry["state"] in readback_states:
            readback = _readback_inserted_rows(store, portal_gym_key, [row])
            matches = [r for r in (readback or [])
                       if r.get("logical_post_id") == entry["logical_post_id"]]
            if len(matches) == 1:
                persisted = matches[0]
        if not _settle_armed_drive_landing(portal_gym_key, row, pick, persisted, log):
            held.append(entry["use_id"])
    return dict(ok=not held, planned=0, recovered=len(entries)-len(held),
                operational_hold=bool(held), use_ids=held,
                reason="remote Drive use recovery hold" if held else "remote Drive uses recovered")


def _release_local_reservation(binding):
    """Release one exact local reservation using its reserve-time binding.

    The guarded release re-proves the reservation account, rotation key,
    canonical path and SHA-256 bytes before deleting. A hold or any failure is
    an unknown outcome: the reservation is retained, never deleted by tuple
    guesswork. Returns True only when the exact row was released."""
    reservation_id, account_key, key, path, content_hash = binding
    if not reservation_id:
        return False
    try:
        return bool(rotation.release_served(
            reservation_id, account_key=account_key, key=key, path=path,
            content_hash=content_hash))
    except Exception as exc:  # noqa: BLE001 - unknown outcome: retain
        print(f"[gbp-planner] local reservation {reservation_id} release held "
              f"({type(exc).__name__}); reservation retained")
        return False


def _calendar_row_key(row):
    return (str((row or {}).get("post_date") or ""),
            str((row or {}).get("format") or ""),
            str((row or {}).get("image_url") or ""),
            str((row or {}).get("account") or ""))


def _readback_inserted_rows(store, portal_gym_key, proposed, *, max_rows=None):
    """Return exact landed rows, [] for proven-none, or None when unreadable.

    Supabase may commit an insert and lose the HTTP response. A normal month
    listing is not absence proof: it is active-row filtered and has no exact
    result count. Use either an explicitly authoritative keyed reader (tests or
    another store implementation) or one count=exact query per proposed row.
    Missing counts, malformed responses, and read failures are uncertainty.
    """
    authoritative = getattr(store, "authoritative_rows_for_keys", None)
    if callable(authoritative):
        try:
            rows = authoritative(portal_gym_key, proposed)
        except Exception:  # noqa: BLE001
            return None
        if rows is None:
            return None
        if max_rows is not None:
            from itertools import islice
            rows = list(islice(iter(rows), max_rows + 1))
            return rows if len(rows) <= max_rows else None
        return list(rows)

    base = getattr(store, "_s", store)
    client_fn = getattr(base, "_client", None)
    rest_fn = getattr(base, "_rest", None)
    headers_fn = getattr(base, "_headers", None)
    if not all(callable(fn) for fn in (client_fn, rest_fn, headers_fn)):
        return None
    try:
        visible = []
        for row in proposed:
            response = client_fn().get(
                rest_fn("content_calendar"),
                params={
                    "gym_id": f"eq.{portal_gym_key}",
                    "account": f"eq.{row.get('account') or ''}",
                    "post_date": f"eq.{row.get('post_date') or ''}",
                    "format": f"eq.{row.get('format') or ''}",
                    "image_url": f"eq.{row.get('image_url') or ''}",
                    "select": "*",
                    **({"limit": str(max_rows)} if max_rows is not None else {}),
                },
                headers=headers_fn({"Prefer": "count=exact"}), timeout=30)
            if response.status_code >= 400:
                return None
            content_range = str(response.headers.get("content-range") or "")
            try:
                total = int(content_range.rsplit("/", 1)[1])
            except (IndexError, ValueError):
                return None
            found = response.json() or []
            if max_rows is not None and (total > max_rows or len(found) > max_rows):
                return None
            if total == 0:
                if found:
                    return None
                continue
            matches = [item for item in found
                       if _calendar_row_key(item) == _calendar_row_key(row)]
            if not matches:
                return None
            visible.extend(matches)
    except Exception:  # noqa: BLE001 - uncertainty is a first-class result
        return None
    return visible


def _alert_drive_claim_hold(portal_gym_key, picks, reason):
    """Emit an explicit operational hold while atomic claims prevent re-offer."""
    ids = [str(p.get("asset", {}).get("id") or "") for p in picks]
    ids = [asset_id for asset_id in ids if asset_id]
    if not ids:
        return
    try:
        from . import ops_alerts
        ops_alerts.alert(
            f"GBP media claim HOLD for {portal_gym_key}: {', '.join(ids)}; {reason}. "
            "Claims remain in-flight so these assets cannot be offered again; "
            "reconcile the calendar row and media usage receipt before release.")
    except Exception as exc:  # noqa: BLE001 - the claim itself remains the safety belt
        print(f"[gbp-planner] claim hold alert failed for {portal_gym_key}: "
              f"{type(exc).__name__}")


def _row(portal_gym_key, account_gen_key, day_key, caption, image_url, *,
         topic_type, pillar, cta_type=gbp.DEFAULT_CTA, cta_url="",
         event=None, offer=None, gbp_location_id=None, fmt="update",
         status="pending", source_media_url=None, source_media_asset_id=None):
    """One content_calendar GBP row dict (no id; DB mints it). account is the literal
    'googlebusiness'; gym_id is the portal_gym_key canonical join. New rows always use
    'pending' for the owner's normal approval flow, regardless of a legacy status value."""
    row = {
        "gym_id": portal_gym_key,
        "account": gbp.PLATFORM,             # 'googlebusiness'
        "post_date": day_key,
        "pillar": pillar,
        "format": fmt,                        # update | event | offer | photo
        "caption": caption,
        "image_url": image_url,
        "status": "pending",
        "gbp_topic_type": topic_type,
    }
    if topic_type != "OFFER":
        row["gbp_cta_type"] = cta_type
        row["gbp_cta_url"] = cta_url
    if event is not None:
        row["gbp_event"] = event
    if offer is not None:
        row["gbp_offer"] = offer
    if gbp_location_id:
        row["gbp_location_id"] = gbp_location_id
    if source_media_url:
        # Exact raw source lineage for the global prepared writer. Never the
        # cropped/delivered URL: a transform names its true raw source, a
        # same-object row names the delivered object itself.
        row["source_media_url"] = source_media_url
    if source_media_asset_id:
        # The Drive/media-library asset the raw source came from (future global
        # one-use provenance). Propagated, never minted: a caller passes only an
        # id it already carries on the mirrored feed draft.
        row["source_media_asset_id"] = str(source_media_asset_id)
    # GBP posts are singleton logical objects. Identity does not derive from
    # date, image, or any IG/FB/Story grouping.
    if config.logical_post_id_enabled():
        _ensure_logical_post_id(row)
    return row


def _ensure_logical_post_id(row):
    """Assign one stable UUID to this GBP row object, preserving valid retries.

    Invalid pre-existing identity is a hard error: replacing it could turn a
    retry into a second logical post. Callers must not persist an unkeyed row.
    """
    existing = row.get("logical_post_id")
    if existing is not None:
        try:
            uuid.UUID(str(existing))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("invalid logical_post_id") from exc
        return str(existing)
    assigned = str(uuid.uuid4())
    # Validate the generated value before allowing this row to reach storage.
    uuid.UUID(assigned)
    row["logical_post_id"] = assigned
    return assigned


# ---- B9: an empty GBP month must name what is MISSING ---------------------------
# ENG, 2026-09: the month sweep failed for four gyms with the single line "nothing
# planned (no A+ captions or media)". That sentence is true and useless -- it names
# two possible causes and tells the gym which of them applies for neither. The planner
# already knows: every skipped slot has exactly one reason. This turns the ledger into
# one sentence a gym owner can act on today.
#
# GBP KEEPS ITS OWN BAR. The A+ caption gate is NOT relaxed and no threshold is lowered
# here: a slot that cannot clear the gate is still skipped, and nothing is ever
# fabricated to fill it. The only thing that changes is what we SAY about the empty
# month. "Give GBP its own threshold" was the alternative; naming the gap is the honest
# half of that choice, and it is the half that does not put weaker copy in front of
# Google.
_SKIP_LANGUAGE = (
    ("no_media", "add photos (connect the gym's Drive folder or upload in the portal)"),
    ("no_fact", "add approved content sources (the intake form's about / offers / "
                "events sections are empty)"),
    ("caption_rejected", "the drafted captions did not clear the quality gate"),
    ("no_event_schedule", "the events on file have no schedule, so they cannot be posted"),
)


def empty_month_reason(skips, counts=None):
    """One sentence naming the LARGEST real cause of an empty GBP month, in the gym's
    own language. Falls back to the historical wording only when the ledger is empty
    (a month with no slots attempted at all), so no caller loses its reason string."""
    skips = dict(skips or {})
    ranked = [(n, name, text) for name, text in _SKIP_LANGUAGE
              for n in (int(skips.get(name) or 0),) if n > 0]
    if not ranked:
        return "nothing planned (no A+ captions or media)"
    ranked.sort(key=lambda r: (-r[0], r[1]))
    top_n, _top_name, top_text = ranked[0]
    total = sum(n for n, _nm, _t in ranked)
    rest = ""
    if len(ranked) > 1:
        rest = " Also skipped for: " + ", ".join(
            f"{t} ({n})" for n, _nm, t in ranked[1:]) + "."
    return (f"nothing planned: {total} Google Business slot(s) were skipped. "
            f"The biggest gap is {top_n} slot(s) where {top_text}." + rest)


def plan_gbp_month(portal_gym_key, account_gen_key, *, voice, library_path, city,
                   store, start=None, days=30, offer=None, events=(),
                   gbp_location_id=None, cta_url="", caption_fn=None, image_fn=None,
                   facts=None, offer_confirmed=False, initial_status="pending",
                   logger=None):
    """Plan one GBP month for a gym and WRITE it as PENDING rows (Echo write path).

    Cadence (§5.1): 8 STANDARD + 1 OFFER (if a real offer resolves) + up to 2 EVENT
    (only real events passed in) + 4 photo drops. Every STANDARD/EVENT caption must
    clear the A+ gate or its slot is SKIPPED (A+ or nothing). One distinct photo per
    post (no reuse), cropped to 1200x900 at plan time. Returns
    {ok, planned, standard, offer, event, photo, skipped}.

    offer: (name, offer_dict) from resolve_offer, or None. events: list of
    {title, schedule, fact} dicts (real, from the gym record). caption_fn/image_fn are
    injectable for tests; production uses generate_gbp_caption + client_content.pick_image
    + crop-and-host.

    facts: OPTIONAL list of (pillar, fact_text) tuples — a bespoke, already-approved fact
    source (e.g. LASSO's own lasso_now.md copy bank) for a tenant whose content does NOT
    live in the client_sources pipeline. When given, the STANDARD loop draws its facts
    from this list (cycled) instead of client_sources, and satisfies the presence guard.
    It is REAL approved material only; every A+ / figure / no-dash gate still runs on the
    generated caption, so no gate is weakened and nothing is fabricated.

    GATE 1 offer_confirmed: the OFFER slot is planned ONLY when this is True AND a real
    offer resolves. A gym whose live offer is not confirmed gets NO OFFER post (a wrong
    offer to Google is a failure we cannot eat). Local updates / events / photo drops are
    unaffected. The legacy initial_status argument is accepted for call compatibility,
    but every new row is pending for the gym's own approval; coach review is retired."""
    log = logger or (lambda m: print(f"[gbp-planner] {m}"))
    initial_status = "pending"
    try:
        armed = _remote_drive_cas_enabled()
        if armed:
            recovery = _recover_armed_drive_uses(portal_gym_key, account_gen_key, store, log)
            if recovery is not None:
                return recovery
    except Exception:
        return {"ok": False, "planned": 0, "operational_hold": True,
                "reason": "remote Drive use recovery or flag unavailable"}
    start = start or date.today()
    caption_fn = caption_fn or (lambda fact: generate_gbp_caption(fact, voice, city))
    facts = list(facts or [])
    present = client_sources.categories_present(account_gen_key)
    if not present and not facts and not offer and not events:
        return {"ok": False, "reason": "no approved sources / facts / offer / events",
                "planned": 0}

    used = set()
    used_drive_ids = set()
    used_drive_hashes = set()
    global_local_hold = [False]
    rows = []
    media_claims = []
    counts = {"standard": 0, "offer": 0, "event": 0, "photo": 0, "skipped": 0}
    # B9 (ENG, month sweep): a bare "nothing planned (no A+ captions or media)"
    # told four gyms nothing they could act on. Every skipped slot now records WHY,
    # so an empty month names the ONE thing the gym has to fix. Counting only; no
    # slot is filled differently and nothing is fabricated to fill one.
    skips = {"no_fact": 0, "no_media": 0, "caption_rejected": 0,
             "no_event_schedule": 0}

    # GBP uses its own rotation namespace (_gbp suffix) so that IG/FB calendar
    # placement history doesn't block photos from appearing on Google Business.
    # _vision_base recognises the _gbp suffix, so vision scoring still applies.
    _gbp_rotation_key = account_gen_key.replace("_ig", "_gbp").replace("_fb", "_gbp")

    def _image_pick(day_key, pillar=None):
        if image_fn is not None:
            url = image_fn(day_key, used)
            if not url:
                return None
            pick = {"url": url, "kind": "injected", "day_key": day_key}
            if _global_writer_enabled():
                # An injected URL is delivered exactly as given — no transform —
                # so the raw source IS the delivered object (same-object row).
                pick["source_media_url"] = url
            return pick
        drive_pick = _drive_photo_candidate(
            account_gen_key, day_key, used_drive_ids)
        if drive_pick and drive_pick.get("hold"):
            return None
        if drive_pick:
            if drive_pick.get("kind") == "drive":
                from . import gym_media_selector as _media_selector
                digest = _media_selector._byte_hash(drive_pick["asset"])
                if not digest or digest in used_drive_hashes:
                    return None
                try:
                    aliases = drive_pick["store"].list_assets(drive_pick["base"])
                    used_drive_ids.update(
                        str(a["id"]) for a in aliases
                        if _media_selector._byte_hash(a) == digest
                        and str(a.get("gym_id")) == drive_pick["base"])
                except Exception:
                    return None
                used_drive_hashes.add(digest)
            return drive_pick
        # §4: pass the slot pillar so vision content-scores the pick (no-op for non-vision).
        try:
            img = client_content.pick_image(_gbp_rotation_key, day_key, library_path,
                                            exclude_keys=used, pillar=pillar,
                                            prefer_photos=True)
        except client_content.LocalPhotoGlobalLedgerUnavailable:
            global_local_hold[0] = True
            log(f"{day_key}: held GBP local-photo pick; global byte history is unreadable")
            return None
        if img is None:
            return None
        if getattr(img, "media_type", "") == "video":
            return None
        key = os.path.basename(img.path)
        if _global_writer_enabled():
            prov = _transformed_gbp_image(account_gen_key, img, day_key)
        else:
            url = _cropped_image_url(account_gen_key, img, day_key)
            prov = {"url": url} if url else None
        if prov:
            used.add(key)
            from . import dam
            pick = {"url": prov["url"], "kind": "local", "day_key": day_key,
                    "rotation_key": dam.rotation_key(img.path), "pillar": pillar,
                    "path": img.path}
            if prov.get("source_media_url"):
                pick["source_media_url"] = prov["source_media_url"]
                pick["render_evidence"] = prov["render_evidence"]
            return pick
        return None

    def _accept(row, pick):
        rows.append(row)
        media_claims.append((row, pick))

    # ---- 8 STANDARD (2/week ~ every 3-4 days) ----
    day = start
    fact_i = 0
    while counts["standard"] < CADENCE["STANDARD"] and (day - start).days < days:
        if facts:
            pillar, fact = facts[fact_i % len(facts)]
            fact_i += 1
        else:
            cat = client_content.category_for_day(account_gen_key, day.isoformat(),
                                                  present) if present else None
            src = client_content._source_for_day(account_gen_key, day.isoformat(), cat,
                                                  present) if cat else None
            fact = getattr(src, "text", "") if src else ""
            pillar = cat or "update"
        pick = _image_pick(day.isoformat(), pillar=pillar) if fact else None
        img_url = pick["url"] if pick else None
        cap = caption_fn(fact) if (fact and img_url) else None
        if cap and img_url:
            _accept(_row(portal_gym_key, account_gen_key, day.isoformat(), cap,
                         img_url, topic_type="STANDARD", pillar=pillar,
                         cta_type=gbp.DEFAULT_CTA, cta_url=cta_url,
                         gbp_location_id=gbp_location_id, fmt="update",
                         status=initial_status,
                         source_media_url=pick.get("source_media_url")), pick)
            counts["standard"] += 1
        else:
            counts["skipped"] += 1
            if not fact:
                skips["no_fact"] += 1
            elif not img_url:
                skips["no_media"] += 1
            else:
                skips["caption_rejected"] += 1
        day += timedelta(days=3)

    # ---- 1 OFFER (GATE 1: only when a real offer + redeem url resolved AND confirmed) ----
    if offer and offer[0] and offer[1] and offer_confirmed:
        oname, odict = offer
        od = day.isoformat()
        pick = _image_pick(od, pillar="offer")
        img_url = pick["url"] if pick else None
        cap = caption_fn(f"Our current offer: {oname}") if img_url else None
        if cap and img_url:
            _accept(_row(portal_gym_key, account_gen_key, od, cap, img_url,
                         topic_type="OFFER", pillar="offer", offer=odict,
                         event=_offer_window(day), gbp_location_id=gbp_location_id,
                         fmt="offer", status=initial_status,
                         source_media_url=pick.get("source_media_url")), pick)
            counts["offer"] += 1
        else:
            counts["skipped"] += 1
            if not img_url:
                skips["no_media"] += 1
            else:
                skips["caption_rejected"] += 1
        day += timedelta(days=2)
    elif offer and offer[0] and offer[1] and not offer_confirmed:
        log(f"{portal_gym_key}: offer '{offer[0]}' resolved but NOT confirmed -> OFFER "
            "slot skipped (GATE 1: offer-only-when-confirmed)")

    # ---- 0-2 EVENT (real events only) ----
    for ev in list(events)[:CADENCE["EVENT_MAX"]]:
        ed = day.isoformat()
        pick = _image_pick(ed, pillar="event")
        img_url = pick["url"] if pick else None
        cap = caption_fn(ev.get("fact") or ev.get("title") or "") if img_url else None
        if cap and img_url and ev.get("schedule"):
            _accept(_row(portal_gym_key, account_gen_key, ed, cap, img_url,
                         topic_type="EVENT", pillar="event", cta_type=gbp.DEFAULT_CTA,
                         cta_url=cta_url,
                         event={"title": ev.get("title") or "", "schedule": ev["schedule"]},
                         gbp_location_id=gbp_location_id, fmt="event",
                         status=initial_status,
                         source_media_url=pick.get("source_media_url")), pick)
            counts["event"] += 1
        else:
            counts["skipped"] += 1
            if not img_url:
                skips["no_media"] += 1
            elif not ev.get("schedule"):
                skips["no_event_schedule"] += 1
            else:
                skips["caption_rejected"] += 1
        day += timedelta(days=2)

    # ---- 4 PHOTO drops (gallery uploads; image only, no caption gate) ----
    pday = start + timedelta(days=1)
    while counts["photo"] < CADENCE["PHOTO"] and (pday - start).days < days:
        pick = _image_pick(pday.isoformat(), pillar="photo")
        img_url = pick["url"] if pick else None
        if img_url:
            _accept(_row(portal_gym_key, account_gen_key, pday.isoformat(),
                         "", img_url, topic_type="STANDARD", pillar="photo",
                         gbp_location_id=gbp_location_id, fmt="photo",
                         status=initial_status,
                         source_media_url=pick.get("source_media_url")), pick)
            counts["photo"] += 1
        pday += timedelta(days=7)

    if global_local_hold[0]:
        # Do not partially write a GBP plan after a local photo tier became
        # unprovable. A later rerun with a readable global ledger is the only
        # safe way to decide those slots.
        return {"ok": False, "reason": "global local-photo history unreadable",
                "planned": 0, "skips": dict(skips), **counts}

    if not rows:
        return {"ok": False, "reason": empty_month_reason(skips, counts),
                "planned": 0, "skips": dict(skips), **counts}

    if not hasattr(store, "insert_rows"):
        # never report a phantom success: a store that cannot persist means 0 rows landed
        return {"ok": False, "reason": "store cannot persist rows (no insert_rows)",
                "planned": 0, **counts}
    # Claim Drive assets atomically across concurrent planners. This is a
    # reservation only: usage is still stamped after the row insert succeeds.
    drive_claims = []
    for _row_obj, pick in media_claims:
        if pick.get("kind") != "drive":
            continue
        if not _claim_drive_pick(pick, _gbp_rotation_key):
            for prior in drive_claims:
                _release_drive_claim(prior)
            return {"ok": False, "reason": "Drive photo already claimed",
                    "planned": 0, "skips": dict(skips), **counts}
        drive_claims.append(pick)

    # Reserve local photos before the batch write, then release the exact
    # reservation for every row that does not land. A successful run keeps the
    # durable record so every later platform/planner run sees the photo consumed.
    # Each reservation carries its reserve-time binding (account, rotation key,
    # canonical path, SHA-256 content hash) so the guarded release can re-prove
    # it is deleting the exact unlanded row and nothing else.
    local_reservations = {}
    stamp_failures = []
    claim_receipt_failures = []
    insert_readback_recovered = False
    insert_readback_complete = False
    for row, pick in media_claims:
        if pick.get("kind") != "local":
            continue
        path = str(pick.get("path") or "")
        canonical = os.path.abspath(path) if path else ""
        try:
            digest = rotation.local_content_hash(canonical) if canonical else ""
        except OSError:
            digest = ""
        try:
            rid = rotation.reserve_local_photo_once(
                _gbp_rotation_key, pick.get("rotation_key"),
                pick.get("pillar") or "photo", pick["day_key"],
                path=canonical or path)
        except Exception:  # noqa: BLE001 - a reserve hold is a failed reservation
            rid = None
        if rid is None:
            # No calendar row exists yet: every prior reservation is a proven
            # never-landed selection and releases with its exact binding.
            for binding in local_reservations.values():
                _release_local_reservation(binding)
            for prior in drive_claims:
                _release_drive_claim(prior)
            return {"ok": False, "reason": "local photo reservation failed",
                    "planned": 0, "skips": dict(skips), **counts}
        local_reservations[id(row)] = (rid, _gbp_rotation_key,
                                       pick.get("rotation_key"),
                                       canonical or path, digest)
    if armed:
        # Persist durable source-bound identity + write intent for EVERY Drive row
        # before the batch POST. Any journal hold fails closed: nothing is sent.
        from . import gbp_drive_use_journal as _journal
        drive_pairs = [(r, p) for r, p in media_claims if p.get("kind") == "drive"]
        journal_ok = True
        for row, pick in drive_pairs:
            entry = _prepare_drive_use_journal(portal_gym_key, row, pick)
            if entry is None:
                journal_ok = False
                break
            pick["journal_entry"] = entry
        if journal_ok:
            for row, pick in drive_pairs:
                entry = pick["journal_entry"]
                try:
                    if entry["state"] == "prepared":
                        pick["journal_entry"] = _journal.record_write_intent(
                            entry["use_id"])
                    elif entry["state"] not in ("write_intent", "unknown_result"):
                        # A landed/settled entry must never be re-sent.
                        journal_ok = False
                        break
                except _journal.JournalHold:
                    journal_ok = False
                    break
        if not journal_ok:
            # Preserve every claim and entry when any intent may be ambiguous.
            # Only a fully prepared batch can be abandoned and released safely.
            ambiguous = any(p.get("journal_entry", {}).get("state") != "prepared"
                            for _, p in drive_pairs if p.get("journal_entry"))
            safe_release = not ambiguous
            if safe_release:
                for row, pick in drive_pairs:
                    entry = pick.get("journal_entry")
                    if entry:
                        try:
                            _journal.abandon(entry["use_id"])
                        except _journal.JournalHold:
                            safe_release = False
                if safe_release:
                    for prior in drive_claims:
                        _release_drive_claim(prior)
            for binding in local_reservations.values():
                _release_local_reservation(binding)
            return {"ok": False,
                    "reason": "remote Drive use journal hold before send",
                    "planned": 0, "skips": dict(skips), **counts}
    try:
        if _global_writer_enabled():
            # Render evidence is NOT a row column; it binds source/delivered bytes at
            # the prepared-writer boundary via insert_rows(render_evidence_by_url=...).
            # gbp_store.insert_rows is a pure passthrough without the kwarg, so call
            # the underlying calendar store directly (same object _readback uses).
            evidence_by_url = {
                p["url"]: p["render_evidence"]
                for _row_obj, p in media_claims if p.get("render_evidence")}
            target = getattr(store, "_s", store)
            inserted = target.insert_rows(
                portal_gym_key, rows, render_evidence_by_url=evidence_by_url) or []
        else:
            inserted = store.insert_rows(portal_gym_key, rows) or []
    except Exception as exc:
        readback = _readback_inserted_rows(store, portal_gym_key, rows)
        if armed:
            # The POST outcome is missing/ambiguous: every armed Drive entry goes
            # unknown_result; an exact zero readback is recorded but NEVER settles.
            from . import gbp_drive_use_journal as _journal
            for _row_obj, pick in media_claims:
                if pick.get("kind") != "drive":
                    continue
                entry = pick.get("journal_entry")
                if not entry:
                    continue
                try:
                    current = _journal.get(entry["use_id"])
                    if current and current["state"] == "write_intent":
                        _journal.mark_unknown(entry["use_id"])
                        if readback == []:
                            _journal.record_zero_readback(entry["use_id"])
                except _journal.JournalHold:
                    pass
        if readback is None:
            # The remote may have committed. Releasing would make landed media
            # immediately reofferable, so retain all claims/reservations.
            _alert_drive_claim_hold(
                portal_gym_key, drive_claims,
                f"insert raised {type(exc).__name__} and authoritative readback failed")
            raise
        if not readback:
            # Even an exact zero can race a late POST commit. The exception
            # leaves the write outcome unknown, so every claim stays held.
            _alert_drive_claim_hold(
                portal_gym_key, drive_claims,
                f"insert raised {type(exc).__name__}; immediate readback was empty")
            raise
        # The response was lost after at least one row committed. Stamp the
        # visible rows, but keep claims for every unseen row: a late commit can
        # still follow this readback.
        inserted = readback
        insert_readback_recovered = True
        insert_readback_complete = (
            {_calendar_row_key(r) for r in inserted}
            == {_calendar_row_key(r) for r in rows})
    inserted_keys = {_calendar_row_key(r) for r in inserted}
    inserted_by_key = ({_calendar_row_key(r): r for r in inserted}
                       if armed else {})
    armed_unseen = []
    for row, pick in media_claims:
        landed = _calendar_row_key(row) in inserted_keys
        if pick.get("kind") == "local" and not landed and not insert_readback_recovered:
            _release_local_reservation(
                local_reservations.get(id(row), (None, None, None, None, None)))
        if pick.get("kind") == "drive" and not landed and not insert_readback_recovered:
            if armed:
                # An unseen armed row is never released: a late commit can still
                # follow, so it stays held and non-reofferable in unknown_result.
                from . import gbp_drive_use_journal as _journal
                entry = pick.get("journal_entry")
                if entry:
                    try:
                        current = _journal.get(entry["use_id"])
                        if current and current["state"] == "write_intent":
                            _journal.mark_unknown(entry["use_id"])
                    except _journal.JournalHold:
                        pass
                armed_unseen.append(str(pick.get("asset", {}).get("id") or ""))
            else:
                _release_drive_claim(pick)
        if pick.get("kind") == "drive" and landed and armed:
            # Exact landed rows only: durable landed proof, remote atomic use
            # with the SAME UUID + frozen snapshots, verified receipt, then the
            # claim completes. Any hold keeps the claim in-flight.
            if not _settle_armed_drive_landing(
                    portal_gym_key, row, pick,
                    inserted_by_key.get(_calendar_row_key(row)), log):
                stamp_failures.append(str(pick["asset"].get("id") or ""))
        if pick.get("kind") == "drive" and landed and not armed:
            # A transient usage write gets one bounded retry. Never stamp a
            # caption-rejected or unpersisted candidate. stamp_use spans the
            # media store and its kv receipt, so inspect the asset after an
            # exception before retrying to avoid a double increment when only
            # the receipt write failed.
            original_count = int(pick["asset"].get("used_count") or 0)
            stamped = False
            last_exc = None
            for attempt in range(2):
                try:
                    from . import gym_media_selector
                    gym_media_selector.stamp_use(
                        pick["asset"], pick["base"], pick["day_key"],
                        store=pick["store"])
                    stamped = True
                    break
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    try:
                        fresh = next(
                            (a for a in pick["store"].list_assets(pick["base"])
                             if str(a.get("id")) == str(pick["asset"].get("id"))),
                            None)
                        if fresh and int(fresh.get("used_count") or 0) > original_count:
                            stamped = True
                            break
                    except Exception:  # noqa: BLE001 - safe bounded retry below
                        pass
            if not stamped:
                log(f"{portal_gym_key}: durable Drive usage stamp failed for "
                    f"{pick['asset'].get('id')} after insert: "
                    f"{type(last_exc).__name__}: {last_exc}")
                stamp_failures.append(str(pick["asset"].get("id") or ""))
            else:
                try:
                    _complete_drive_claim(pick)
                except Exception as exc:  # noqa: BLE001
                    # The atomic claim remains in-flight, so the asset is still
                    # non-reofferable even if its completion receipt fails.
                    log(f"{portal_gym_key}: Drive claim completion failed for "
                        f"{pick['asset'].get('id')}: {type(exc).__name__}")
                    claim_receipt_failures.append(
                        str(pick["asset"].get("id") or ""))
    if not inserted:
        return {"ok": False, "reason": "store inserted zero rows",
                "planned": 0, "skips": dict(skips), **counts}
    if stamp_failures:
        _alert_drive_claim_hold(
            portal_gym_key,
            [pick for row, pick in media_claims
             if pick.get("kind") == "drive"
             and str(pick.get("asset", {}).get("id") or "") in stamp_failures],
            "calendar row is durable but the media usage receipt could not be stamped")
        return {"ok": False,
                "reason": "durable rows inserted but Drive usage stamp failed: "
                          + ", ".join(stamp_failures),
                "planned": len(inserted), "operational_hold": True,
                "claim_ids": [p.get("claim_id") for p in drive_claims
                              if p.get("claim_id")],
                "skips": dict(skips), **counts}
    if claim_receipt_failures:
        _alert_drive_claim_hold(
            portal_gym_key,
            [pick for row, pick in media_claims
             if pick.get("kind") == "drive"
             and str(pick.get("asset", {}).get("id") or "")
             in claim_receipt_failures],
            "media usage was stamped but the completed-claim receipt failed")
        return {"ok": False,
                "reason": "Drive usage stamped but claim receipt remains in-flight: "
                          + ", ".join(claim_receipt_failures),
                "planned": len(inserted), "operational_hold": True,
                "claim_ids": [p.get("claim_id") for p in drive_claims
                              if p.get("claim_id")],
                "skips": dict(skips), **counts}
    if armed_unseen:
        _alert_drive_claim_hold(
            portal_gym_key,
            [pick for row, pick in media_claims
             if pick.get("kind") == "drive"
             and str(pick.get("asset", {}).get("id") or "") in armed_unseen],
            "armed Drive row unseen after send; held pending authoritative readback")
        return {"ok": False,
                "reason": "armed Drive rows unseen after send; held and "
                          "non-reofferable: " + ", ".join(armed_unseen),
                "planned": len(inserted), "operational_hold": True,
                "skips": dict(skips), **counts}
    if insert_readback_recovered and not insert_readback_complete:
        _alert_drive_claim_hold(
            portal_gym_key,
            [pick for row, pick in media_claims
             if pick.get("kind") == "drive"
             and _calendar_row_key(row) not in inserted_keys],
            "insert response failed; unseen rows may commit after readback")
        return {"ok": False,
                "reason": "insert response failed; partial durable rows reconciled by readback",
                "planned": len(inserted), "operational_hold": True,
                "skips": dict(skips), **counts}
    log(f"{portal_gym_key}: planned {len(rows)} GBP rows "
        f"(std {counts['standard']}, offer {counts['offer']}, event {counts['event']}, "
        f"photo {counts['photo']}, skipped {counts['skipped']})")
    return {"ok": True, "planned": len(inserted) or len(rows),
            "skips": dict(skips), **counts}
