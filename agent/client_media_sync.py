"""
client_media_sync.py: the missing link that makes Echo START working on a CLIENT
gym the moment that gym UPLOADS its media.

The upload page (agent/intake_web.py) is a SEPARATE web process that touches R2 ONLY:
a gym's photos/videos land FRESH in R2 under intake/<base>/incoming/<stamp>_<name>
with a sidecar <stamp>_upload.json (per-file captions + a batch note).

The LISTENER'S ingest pass (agent/intake_ingest.py) then MOVES that media OUT of
incoming/ as it processes it (see intake_ingest._process_client): a captioned photo
is filed to the library directly, BUT an uncaptioned upload (a sidecar with no note)
is STAGED to intake/<base>/pending_caption/<name> with a sibling <stem>.json
(status=needs_caption) and its incoming/ object is DELETED; a conversion (HEIC->JPG,
MOV->MP4) archives the untouched source to intake/<base>/originals/<name>; thumbnails
go to intake/<base>/thumbs/<name>_thumb.jpg; the processed set is tracked in
intake/<base>/manifest.json. So a REAL gym's usable media can sit in pending_caption/
(and its raw source in originals/) with NOTHING left in incoming/.

The old sync_uploads listed ONLY incoming/ -> it found 0 media for a gym whose photos
ingest had already staged, so the client month builder
(client_month_run.build_client_month) saw an empty library and WAITED forever. This
module now pulls the gym's REAL media wherever ingest actually left it (pending_caption/
+ incoming/ + originals/), skipping thumbnails, JSON sidecars, and the manifest.

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

# The R2 upload layout. Fresh uploads (intake_web.handle_upload) carry two sidecar
# kinds under incoming/; ingest (intake_ingest) stages processed media under
# pending_caption/ with a sibling <stem>.json, archives raw sources under originals/,
# writes thumbnails under thumbs/, and tracks the processed set in manifest.json.
_UPLOAD_SIDECAR_SUFFIX = "_upload.json"
_INTAKE_SIDECAR_SUFFIX = "_intake.json"
_MANIFEST_BASENAME = "manifest.json"

# The prefixes under intake/<base>/ that actually hold the gym's REAL media, in the
# order we prefer them when the SAME photo lives in more than one (e.g. a converted
# JPG in pending_caption/ plus its raw HEIC source in originals/): the processed,
# usable form wins over the raw source archive.
_MEDIA_PREFIXES = ("pending_caption/", "incoming/", "originals/")

# Prefixes under intake/<base>/ we must NEVER sync as media (thumbnails, review /
# deadletter quarantines, the archived intake forms). Listed for clarity even though
# thumbs are also excluded by the _thumb suffix and none carry a media basename we keep.
_SKIP_PREFIXES = ("thumbs/", "review/", "deadletter/", "forms/", "_control/")

# ingest's thumbnail suffix (intake_ingest._make_thumbnail): "<stem>_thumb.jpg".
_THUMB_SUFFIX = "_thumb.jpg"


def _is_media_key(key):
    """True when an R2 key is an uploaded MEDIA object (not a sidecar, thumbnail, or
    the manifest), by extension. Thumbnails share a media extension, so they are
    excluded explicitly by their _thumb.jpg suffix; every *.json (batch/per-file
    sidecars and manifest.json) is excluded by extension."""
    if not key:
        return False
    base = os.path.basename(key)
    if base == _MANIFEST_BASENAME:
        return False
    if key.endswith(_UPLOAD_SIDECAR_SUFFIX) or key.endswith(_INTAKE_SIDECAR_SUFFIX):
        return False
    if base.endswith(_THUMB_SUFFIX):   # a thumbnail is not the gym's real media
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


def _caption_from_sidecar(data):
    """The gym's one line for a photo out of a per-file <stem>.json sidecar dict, or
    "". intake_web/ingest never settle on a single key name here (a fresh per-file
    intent could be "caption"; ingest's pending_caption sidecar is status-only with
    "note" when a caption later arrives), so we accept either "caption" or "note".
    A status-only needs_caption sidecar simply yields "" (nothing fabricated)."""
    if not isinstance(data, dict):
        return ""
    for k in ("caption", "note"):
        v = data.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return ""


def _read_captions(r2, prefixes, log):
    """Build one {stored_basename: caption} map for the gym across every prefix that
    can hold its media. Two caption shapes are merged, both keyed by the STORED media
    basename (exactly the downloaded file name):

      * the BATCH sidecar incoming/<stamp>_upload.json carries a `captions` map
        (basename -> the gym's line), written by intake_web.handle_upload;
      * a PER-FILE sibling <stem>.json next to a media object (pending_caption/ and
        incoming/) carries that one photo's caption/note.

    A flaky/malformed sidecar is skipped (never raises out). The batch map is read
    first; a per-file sidecar only fills a basename the batch did not already caption,
    so an explicit per-file note never silently loses to a blank batch entry."""
    captions = {}
    seen = set()
    for prefix in prefixes:
        try:
            keys = list(r2.list_keys(prefix) or [])
        except Exception as exc:  # noqa: BLE001
            log(f"caption read: list failed: {type(exc).__name__}")
            continue
        for key in keys:
            if key in seen:
                continue
            seen.add(key)
            # BATCH sidecar: an _upload.json carrying a captions map.
            if key.endswith(_UPLOAD_SIDECAR_SUFFIX):
                data = _load_json(r2, key)
                caps = data.get("captions") if isinstance(data, dict) else None
                if isinstance(caps, dict):
                    for name, cap in caps.items():
                        if name and cap:
                            captions.setdefault(str(name), str(cap))
                continue
            # PER-FILE sidecar: a <stem>.json next to a media object (skip the
            # manifest and the batch/intake sidecars, already handled).
            if not key.endswith(".json") or key.endswith(_INTAKE_SIDECAR_SUFFIX):
                continue
            if os.path.basename(key) == _MANIFEST_BASENAME:
                continue
            cap = _caption_from_sidecar(_load_json(r2, key))
            if not cap:
                continue
            stem = os.path.splitext(os.path.basename(key))[0]
            # pair the sidecar with its media basename by shared stem, matching the
            # extension we would actually download so a photo/video lines up.
            for ext in sorted(_MEDIA_EXTS):
                captions.setdefault(stem + ext, cap)
    return captions


def _load_json(r2, key):
    """Parse an R2 JSON object, or {} on missing/empty/malformed. Never raises."""
    try:
        raw = r2.get_bytes(key)
        if not raw:
            return {}
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}


def sync_uploads(base_key, *, r2=None, out_dir=None, logger=None):
    """List the gym's uploaded media in R2 and download the NEW files into the gym's
    content library, writing a .json sidecar (public_url + the gym's caption) per file.

    base_key   the tenant base ("gritx"): its R2 media lives under intake/<base>/
               pending_caption/ + incoming/ + originals/ (wherever ingest left it),
               and its library dir is content_library/<base>.
    r2         an injected R2 client with list_keys(prefix)/get_bytes(key). Defaults to
               the real boto3-backed client (built lazily from env; None when creds are
               absent, in which case this no-ops). Never logs credentials.
    out_dir    override the library dir (tests). Defaults to content_library/<base>.

    Returns {"synced": n_new, "skipped": n_already_present}. IDEMPOTENT: a media file
    whose target name already exists in the library is skipped, never re-downloaded;
    within one pass the SAME basename in more than one prefix is downloaded once (the
    processed form under pending_caption/incoming wins over the raw originals/ copy).
    Only image/video extensions are pulled; thumbnails, *.json sidecars, and
    manifest.json are never synced as media. Never raises out: a per-file error is
    logged and the rest continue."""
    log = logger or (lambda m: print(f"[client-media-sync] {m}"))
    base_key = (base_key or "").strip()
    if not base_key:
        return {"synced": 0, "skipped": 0}

    r2 = r2 if r2 is not None else _default_r2()
    if r2 is None:
        log(f"{base_key}: R2 not configured; nothing synced")
        return {"synced": 0, "skipped": 0}

    root = f"intake/{base_key}/"
    prefixes = [root + p for p in _MEDIA_PREFIXES]

    # Collect media keys across every prefix that can hold the gym's real media. The
    # FIRST prefix to claim a basename wins (_MEDIA_PREFIXES order: pending_caption/
    # then incoming/ then originals/), so a converted photo is not also re-filed from
    # its raw source archive.
    chosen = {}          # basename -> R2 key
    listed_any = False
    for prefix in prefixes:
        try:
            keys = list(r2.list_keys(prefix) or [])
        except Exception as exc:  # noqa: BLE001
            log(f"{base_key}: R2 list failed for {prefix}: {type(exc).__name__}")
            continue
        listed_any = True
        for key in keys:
            if not _is_media_key(key):
                continue
            name = os.path.basename(key)
            chosen.setdefault(name, key)

    if not listed_any or not chosen:
        return {"synced": 0, "skipped": 0}

    lib_dir = _library_dir(base_key, out_dir)
    os.makedirs(lib_dir, exist_ok=True)
    captions = _read_captions(r2, prefixes, log)

    synced = 0
    skipped = 0
    for name in sorted(chosen):
        key = chosen[name]
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
    """Write the .json sidecar library._load_sidecar reads: public_url makes the
    downloaded photo a portal-ready real-photo card; the gym's own one line about the
    photo goes in the "note" key (the EXACT key library._load_sidecar reads into
    client_note, never fabricated). Idempotent: an existing sidecar (a reviewed note)
    is never clobbered."""
    stem = os.path.splitext(media_name)[0]
    side_path = os.path.join(lib_dir, stem + ".json")
    if os.path.exists(side_path):
        return
    payload = {"public_url": _public_url_for_key(r2_key)}
    if caption:
        # library._load_sidecar reads data["note"] into Creative.client_note, so the
        # gym's caption MUST live under "note" to reach the drafter (writing
        # "client_note" here would be silently dropped by list_creatives).
        payload["note"] = caption
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


def _existing_feed_count(store, base_key, start, days):
    """(count, ok): how many FEED rows the gym already has in content_calendar across
    the planned span's months, gym-scoped. A FEED row is the unit that consumes one
    photo (a story pairs on the same photo and an FB mirror duplicates the same feed),
    so counting DISTINCT feed post_dates on instagram is the true 'how many photos are
    already placed' measure to compare against the current media count.

    ok=False (with count 0) means we could not read reliably (no list_month, or a read
    error): the caller then treats the gym as NOT extendable this pass and SKIPS, so a
    flaky read never triggers a delete-then-insert rebuild."""
    list_month = getattr(store, "list_month", None)
    if list_month is None:
        return 0, False
    from datetime import timedelta
    months = sorted({(start + timedelta(days=i)).isoformat()[:7] for i in range(days)})
    feed_dates = set()
    for month in months:
        try:
            rows = list_month(base_key, month) or []
        except Exception:  # noqa: BLE001
            return 0, False
        for row in rows:
            if not isinstance(row, dict):
                continue
            fmt = str(row.get("format", "")).lower()
            acct = str(row.get("account", "")).lower()
            # count one per feed post_date on instagram (skip the facebook mirror and
            # every story so the count equals photos placed, not total rows).
            if fmt == "feed" and acct in ("instagram", "ig", ""):
                feed_dates.add(row.get("post_date") or row.get("id") or len(feed_dates))
    return len(feed_dates), True


def scan_and_generate(*, clients=None, store=None, r2=None, now=None, days=30,
                      logger=None):
    """For each onboarded client gym: sync its uploaded media, then build its DRAFT
    month from its REAL photos IF it is newly ready.

    A gym is BUILT when, after sync, ALL hold:
      * content_library/<base> has usable media (build_client_month's own guard), AND
      * the gym has approved client sources (onboarded), AND
      * the gym has MORE unique media than the FEED rows already placed for gym_id ==
        base (grow-on-more: an equal count is idempotent and skipped, never rebuilt;
        the calendar is never longer than the media supports).
    A gym with no media is left AWAITING (no-op, logged). A gym whose calendar already
    matches its media count is left untouched. When the gym uploads MORE, the calendar
    EXTENDS up to the new count (gym-scoped delete-then-insert; cheap real-photo cards,
    no Gemini). Every built calendar is DRAFTS (paused); NOTHING here publishes.

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

            # Media guard: reuse the builder's own count/awaiting-media check so this
            # path and the builder agree on what counts as usable media.
            from .client_month_run import _client_media_count
            media_count = _client_media_count(lib_dir)
            if media_count <= 0:
                awaiting += 1
                results.append({"base": base, "status": "awaiting_media",
                                "synced": sync.get("synced", 0)})
                log(f"{base}: awaiting media (no usable photos/videos yet)")
                continue

            if store is None:
                results.append({"base": base, "status": "no_store",
                                "synced": sync.get("synced", 0)})
                log(f"{base}: media ready but no calendar store configured; not built")
                continue

            # GROW-ON-MORE, NEVER PAST MEDIA: compare the gym's current unique media
            # count against the FEED rows already placed. Only (re)build when the gym
            # has MORE media than placed feeds; an equal count is idempotent (skip, no
            # rebuild). We never build past media_count (the builder caps that too).
            existing_feeds, read_ok = _existing_feed_count(store, base, start, days)
            if not read_ok:
                # Could not read reliably: do NOT risk a duplicate/rebuild this pass.
                skipped_existing += 1
                results.append({"base": base, "status": "calendar_unreadable",
                                "synced": sync.get("synced", 0)})
                log(f"{base}: calendar read failed; left as-is (no rebuild)")
                continue
            if existing_feeds >= media_count:
                # Already built out to (or beyond) the media the gym has: idempotent.
                skipped_existing += 1
                results.append({"base": base, "status": "has_calendar",
                                "synced": sync.get("synced", 0),
                                "media_count": media_count,
                                "existing_feeds": existing_feeds})
                continue
            # else: media_count > existing_feeds -> (re)build up to the new count. The
            # builder's _apply does a gym-scoped delete-then-insert (client calendars
            # are cheap real-photo cards, no Gemini), so the calendar EXTENDS cleanly.

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
