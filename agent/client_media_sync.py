"""
client_media_sync.py: the missing link that makes Echo START working on a CLIENT
gym the moment that gym UPLOADS its media.

The upload page (agent/intake_web.py) is a SEPARATE web process that touches R2 ONLY:
a gym's photos/videos land in R2 under intake/<base>/incoming/<stamp>_<name> with a
sidecar <stamp>_upload.json (per-file captions + a batch note). The listener side has
never pulled those media into the gym's content library, so the client month builder
(client_month_run.build_client_month) always saw an empty library and WAITED forever.

This module closes that loop, listener-side:

  sync_uploads(base_key)      list the gym's uploaded media in R2, download the NEW
                              ones into content_library/<base>/, and write a .json
                              sidecar per file carrying its PUBLIC url (so the draft
                              is a portal-ready real-photo card) and the gym's own
                              caption for that photo. Idempotent: a file already in
                              the library is never re-downloaded.

  scan_and_generate(...)      for each ONBOARDED client gym (has approved client
                              sources), sync its uploads, then, IF it now has media
                              AND approved sources AND NO calendar rows yet, build
                              its DRAFT month from its REAL photos via
                              build_client_month. A gym with no media is left
                              awaiting (no-op). A gym that already has a calendar is
                              never regenerated.

HARD RULES honored:
  * Behind AGENT_CLIENT_MEDIA_SYNC (config.client_media_sync_enabled(), default OFF).
    Flag off -> both entry points return {ok:False} and touch nothing.
  * NO FABRICATION: the calendar is built ONLY from the gym's real uploaded media
    paired with its OWN approved sources (build_client_month enforces this and the
    banned-word guard). This module renders nothing and invents nothing.
  * Client calendars are DRAFTS (paused). NOTHING here publishes: there is no
    meta_publisher / autopublish call anywhere in this path. The calendar
    autopublisher is gym 'lasso' only and lives elsewhere.
  * Secrets are never logged. The R2 client reads credentials by env-var NAME (lazy),
    passes them only to boto3, and is injectable so every test runs fully offline.

THREE KEYS (do not conflate): the tenant BASE ("gritx") is echo_social_intake
.client_key AND content_calendar.gym_id AND the R2 prefix AND the library dir; the
_ig account ("gritx_ig") is the generation/source key; the _fb account is the mirror.
"""

import json
import os

from . import config

# Media extensions we sync (mirror client_month_run._MEDIA_EXTS: the same set that
# counts as a gym having uploaded usable creative).
_MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"}

# The R2 upload layout (see intake_web.handle_upload): media + two sidecar kinds.
_UPLOAD_SIDECAR_SUFFIX = "_upload.json"
_INTAKE_SIDECAR_SUFFIX = "_intake.json"


def _is_media_key(key):
    """True when an R2 key is an uploaded MEDIA object (not a sidecar, by extension)."""
    if not key:
        return False
    if key.endswith(_UPLOAD_SIDECAR_SUFFIX) or key.endswith(_INTAKE_SIDECAR_SUFFIX):
        return False
    return os.path.splitext(key)[1].lower() in _MEDIA_EXTS


def _library_dir(base_key, out_dir=None):
    """The gym's content library directory (content_library/<base_key>), or an
    explicit out_dir. This is where build_client_month reads the gym's real photos."""
    if out_dir:
        return out_dir
    return os.path.join("content_library", base_key)


def _public_url_for_key(key):
    """The public url for an R2 object key, from AGENT_S3_PUBLIC_BASE_URL. Empty when
    no public base is configured (the sidecar then carries no url; the draft path
    treats that as needs-media and the day is simply skipped, never fabricated)."""
    base = (config.S3_PUBLIC_BASE_URL or "").strip()
    if not base:
        return ""
    from urllib.parse import quote
    return f"{base.rstrip('/')}/{quote(key, safe='/')}"


def _read_captions(r2, prefix, log):
    """Merge every _upload.json sidecar under the gym's incoming prefix into one
    {stored_basename: caption} map. A flaky/malformed sidecar is skipped (never
    raises out). The upload handler keys captions by the STORED basename, which is
    exactly the R2 object basename, so it lines up with the downloaded file name."""
    captions = {}
    try:
        keys = list(r2.list_keys(prefix) or [])
    except Exception as exc:  # noqa: BLE001
        log(f"caption read: list failed: {type(exc).__name__}")
        return captions
    for key in keys:
        if not key.endswith(_UPLOAD_SIDECAR_SUFFIX):
            continue
        try:
            raw = r2.get_bytes(key)
            if not raw:
                continue
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue  # a malformed sidecar never blocks a real media download
        caps = data.get("captions") if isinstance(data, dict) else None
        if isinstance(caps, dict):
            for name, cap in caps.items():
                if name and cap:
                    captions[str(name)] = str(cap)
    return captions


def sync_uploads(base_key, *, r2=None, out_dir=None, logger=None):
    """List the gym's uploaded media in R2 and download the NEW files into the gym's
    content library, writing a .json sidecar (public_url + the gym's caption) per file.

    base_key   the tenant base ("gritx"): its R2 prefix is intake/<base>/incoming/ and
               its library dir is content_library/<base>.
    r2         an injected R2 client with list_keys(prefix)/get_bytes(key). Defaults to
               the real boto3-backed client (built lazily from env; None when creds are
               absent, in which case this no-ops). Never logs credentials.
    out_dir    override the library dir (tests). Defaults to content_library/<base>.

    Returns {"synced": n_new, "skipped": n_already_present}. IDEMPOTENT: a media file
    whose target name already exists in the library is skipped, never re-downloaded.
    Only image/video extensions are pulled; sidecars and other objects are ignored.
    Never raises out: a per-file error is logged and the rest continue."""
    log = logger or (lambda m: print(f"[client-media-sync] {m}"))
    base_key = (base_key or "").strip()
    if not base_key:
        return {"synced": 0, "skipped": 0}

    r2 = r2 if r2 is not None else _default_r2()
    if r2 is None:
        log(f"{base_key}: R2 not configured; nothing synced")
        return {"synced": 0, "skipped": 0}

    prefix = f"intake/{base_key}/incoming/"
    try:
        keys = list(r2.list_keys(prefix) or [])
    except Exception as exc:  # noqa: BLE001
        log(f"{base_key}: R2 list failed: {type(exc).__name__}")
        return {"synced": 0, "skipped": 0}

    media_keys = [k for k in keys if _is_media_key(k)]
    if not media_keys:
        return {"synced": 0, "skipped": 0}

    lib_dir = _library_dir(base_key, out_dir)
    os.makedirs(lib_dir, exist_ok=True)
    captions = _read_captions(r2, prefix, log)

    synced = 0
    skipped = 0
    for key in sorted(media_keys):
        name = os.path.basename(key)
        target = os.path.join(lib_dir, name)
        # IDEMPOTENT: already in the library -> never re-download.
        if os.path.exists(target):
            skipped += 1
            continue
        try:
            data = r2.get_bytes(key)
        except Exception as exc:  # noqa: BLE001
            log(f"{base_key}: download failed for one object: {type(exc).__name__}")
            continue
        if not data:
            continue
        try:
            with open(target, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            log(f"{base_key}: write failed for one object: {type(exc).__name__}")
            continue
        _write_sidecar(lib_dir, name, key, captions.get(name, ""), log)
        synced += 1

    if synced or skipped:
        log(f"{base_key}: synced {synced} new media, skipped {skipped} already present")
    return {"synced": synced, "skipped": skipped}


def _write_sidecar(lib_dir, media_name, r2_key, caption, log):
    """Write the .json sidecar library.list_creatives reads: public_url makes the
    downloaded photo a portal-ready real-photo card; client_note carries the gym's
    own one line about the photo (never fabricated). Idempotent: an existing sidecar
    (a reviewed note) is never clobbered."""
    stem = os.path.splitext(media_name)[0]
    side_path = os.path.join(lib_dir, stem + ".json")
    if os.path.exists(side_path):
        return
    payload = {"public_url": _public_url_for_key(r2_key)}
    if caption:
        payload["client_note"] = caption
    try:
        with open(side_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except OSError as exc:
        log(f"sidecar write failed: {type(exc).__name__}")


# ---- scan + generate: sync media, then draft the month for gyms newly ready --------

def _client_bases(clients=None):
    """The tenant bases of the CLIENT gyms to scan, in order.

    An explicit `clients` list (bases or account keys) wins. Otherwise discover from
    the account registry: every _ig client account (the client gyms), reduced to its
    base. LASSO's own accounts and blake_personal are excluded; the _fb mirrors fold
    onto the same base as the _ig account (the generation key)."""
    from .accounts import ACCOUNTS

    if clients:
        seen = []
        for c in clients:
            base = _base_of(c)
            if base and base not in seen:
                seen.append(base)
        return seen

    bases = []
    for acct in ACCOUNTS:
        key = acct.key or ""
        if key.startswith("lasso") or key == "blake_personal":
            continue
        if not key.endswith("_ig"):
            continue
        base = _base_of(key)
        if base and base not in bases:
            bases.append(base)
    return bases


def _base_of(key):
    """The tenant base for an account key or base ('gritx_ig' -> 'gritx')."""
    key = (key or "").strip()
    for suffix in ("_ig", "_fb"):
        if key.endswith(suffix):
            return key[: -len(suffix)]
    return key


def _account_for_base(base_key):
    """The GENERATION account (the _ig account) for a tenant base, or None."""
    from .accounts import get_account
    return get_account(f"{base_key}_ig")


def _has_approved_sources(account_key):
    """True when the gym has at least one APPROVED client source (i.e. it is
    onboarded and its material has cleared human approval). build_client_draft reads
    ONLY approved sources, so a gym with none can never draft a real caption."""
    from . import client_sources
    return bool(client_sources.approved_sources(account_key))


def _banned_words_for(base_key):
    """The gym's never-use words, read from its drafted bible (the intake writes the
    words verbatim into brand_voice/<base>/lasso_voice.md). Empty when the bible is
    missing or carries none. Never raises."""
    path = os.path.join("brand_voice", base_key, "lasso_voice.md")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return ()
    return _parse_banned_from_bible(raw)


def _parse_banned_from_bible(raw):
    """Pull the words following a 'Words to NEVER use:' / 'Never use these words:' line
    (the shape social_intake_reader writes) into a lowercased de-duped tuple. Ignores a
    '(none provided...)' placeholder. Pure."""
    import re
    words = []
    for line in (raw or "").splitlines():
        m = re.search(r"(?:words to never use|never use these words)\s*:\s*(.+)",
                      line, re.IGNORECASE)
        if not m:
            continue
        tail = m.group(1).strip()
        if not tail or tail.lower().startswith("(none"):
            continue
        for chunk in tail.replace("\n", ",").split(","):
            w = chunk.strip().lower()
            if w and w not in words:
                words.append(w)
    return tuple(words)


def _has_existing_calendar(store, base_key, start, days):
    """True when the gym ALREADY has content_calendar rows in any month the planned
    span touches (so we never regenerate a calendar). Uses the store's list_month
    (gym_id-scoped). A store that cannot list is treated as 'no calendar' so a brand
    new gym still gets its first build; a list error is treated as 'has calendar' so
    a flaky read never double-writes. Returns (has, checked)."""
    list_month = getattr(store, "list_month", None)
    if list_month is None:
        return False, False
    from datetime import timedelta
    months = sorted({(start + timedelta(days=i)).isoformat()[:7] for i in range(days)})
    for month in months:
        try:
            rows = list_month(base_key, month)
        except Exception:  # noqa: BLE001
            # A read failure must not cause a duplicate month: treat as 'has'.
            return True, True
        if rows:
            return True, True
    return False, True


def scan_and_generate(*, clients=None, store=None, r2=None, now=None, days=30,
                      logger=None):
    """For each onboarded client gym: sync its uploaded media, then build its DRAFT
    month from its REAL photos IF it is newly ready.

    A gym is BUILT when, after sync, ALL hold:
      * content_library/<base> has usable media (build_client_month's own guard), AND
      * the gym has approved client sources (onboarded), AND
      * no content_calendar rows exist yet for gym_id == base (never regenerate).
    A gym with no media is left AWAITING (no-op, logged). A gym that already has a
    calendar is left untouched. Every built calendar is DRAFTS (paused); NOTHING here
    publishes.

    clients   optional explicit list of bases/account keys; else discovered from the
              client account registry (the _ig accounts).
    store     an injectable SupabaseCalendarStore (list_month + delete_month +
              insert_rows). Defaults to the live store when portal creds are present;
              None when they are absent (then nothing is generated, only synced).
    r2        an injectable R2 client (list_keys/get_bytes). Defaults to the env client.

    Behind AGENT_CLIENT_MEDIA_SYNC. Flag OFF -> {ok:False, reason} and nothing touched.
    One gym failing never blocks the others (each is isolated in try/except).

    Returns {ok, scanned, synced, generated, awaiting, skipped_existing, results}."""
    log = logger or (lambda m: print(f"[client-media-sync] {m}"))
    if not config.client_media_sync_enabled():
        return {"ok": False, "reason": "AGENT_CLIENT_MEDIA_SYNC off",
                "scanned": 0, "synced": 0, "generated": 0, "awaiting": 0,
                "skipped_existing": 0, "results": []}

    from datetime import date
    start = now if isinstance(now, date) else date.today()

    r2 = r2 if r2 is not None else _default_r2()
    if store is None:
        store = _default_store()

    from .voice import load_voice

    bases = _client_bases(clients)
    results = []
    synced_total = 0
    generated = 0
    awaiting = 0
    skipped_existing = 0

    for base in bases:
        try:
            sync = sync_uploads(base, r2=r2, logger=log)
            synced_total += sync.get("synced", 0)

            account = _account_for_base(base)
            if account is None:
                results.append({"base": base, "status": "no_account"})
                continue

            lib_dir = _library_dir(base)

            # A gym with no approved sources is not onboarded yet: never draft.
            if not _has_approved_sources(account.key):
                results.append({"base": base, "status": "no_sources",
                                "synced": sync.get("synced", 0)})
                continue

            # Media guard: reuse the builder's own awaiting-media check so this path
            # and the builder agree on what counts as usable media.
            from .client_month_run import client_awaiting_media
            if client_awaiting_media(base, lib_dir):
                awaiting += 1
                results.append({"base": base, "status": "awaiting_media",
                                "synced": sync.get("synced", 0)})
                log(f"{base}: awaiting media (no usable photos/videos yet)")
                continue

            # Never regenerate: if the gym already has calendar rows, leave them.
            if store is None:
                results.append({"base": base, "status": "no_store",
                                "synced": sync.get("synced", 0)})
                log(f"{base}: media ready but no calendar store configured; not built")
                continue
            has_cal, _checked = _has_existing_calendar(store, base, start, days)
            if has_cal:
                skipped_existing += 1
                results.append({"base": base, "status": "has_calendar",
                                "synced": sync.get("synced", 0)})
                continue

            voice = load_voice(account.voice_doc_path())
            if voice is None:
                results.append({"base": base, "status": "no_voice",
                                "synced": sync.get("synced", 0)})
                log(f"{base}: media ready but voice doc missing; not built")
                continue

            banned = _banned_words_for(base)
            from .client_month_run import build_client_month
            built = build_client_month(
                account, base, start.isoformat(), days,
                voice=voice, library_path=lib_dir, store=store,
                banned_words=banned, logger=log)
            if built.get("ok"):
                generated += 1
                results.append({"base": base, "status": "generated",
                                "synced": sync.get("synced", 0),
                                "upserted": built.get("upserted", 0)})
                log(f"{base}: built DRAFT calendar, {built.get('upserted', 0)} row(s)")
            else:
                if built.get("awaiting_media"):
                    awaiting += 1
                results.append({"base": base,
                                "status": "not_built",
                                "reason": built.get("reason"),
                                "synced": sync.get("synced", 0)})
        except Exception as exc:  # noqa: BLE001
            # One gym's failure never blocks the others.
            log(f"{base}: scan failed: {type(exc).__name__}")
            results.append({"base": base, "status": "error",
                            "error": type(exc).__name__})
            continue

    return {"ok": True, "scanned": len(bases), "synced": synced_total,
            "generated": generated, "awaiting": awaiting,
            "skipped_existing": skipped_existing, "results": results}


# ---- default clients (injectable; real ones built lazily from env) -----------------

def _default_store():
    """The live portal calendar store when the portal Supabase data plane is armed,
    else None (then scan_and_generate syncs but generates nothing). Never logs creds."""
    if not config.portal_calendar_supabase_enabled():
        return None
    try:
        from .portal_calendar_store import SupabaseCalendarStore
        return SupabaseCalendarStore()
    except Exception:  # noqa: BLE001
        return None


class _R2:
    """List/get R2 wrapper for the sync path. Credentials are read lazily by env-var
    NAME, passed only to boto3, never logged. Mirrors intake_ingest._R2 (listener
    side) so both sides read the same bucket the same way."""

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


def _default_r2():
    """The real R2 client from env, or None when credentials are absent. boto3 is
    imported lazily so flag-off / tests never need the SDK. Credentials never logged."""
    key_id = os.environ.get(config.S3_ACCESS_KEY_ID_ENV)
    secret = os.environ.get(config.S3_SECRET_ACCESS_KEY_ENV)
    if not key_id or not secret or not config.S3_BUCKET:
        return None
    try:
        import boto3  # lazy
        s3 = boto3.client("s3", endpoint_url=config.S3_ENDPOINT or None,
                          region_name=config.S3_REGION or None,
                          aws_access_key_id=key_id, aws_secret_access_key=secret)
        return _R2(s3, config.S3_BUCKET)
    except Exception:  # noqa: BLE001
        return None
