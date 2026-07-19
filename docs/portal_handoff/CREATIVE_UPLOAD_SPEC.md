# Creative Upload Spec

Source of truth: `agent/intake_web.py` — `handle_upload()`, `validate_files()`, `_R2`, `ALLOWED_TYPES`.

---

## Portal Upload Lane (live today)

This is the only upload lane the portal builds for. The gym taps the upload link, picks files, and hits send. No portal-side upload UI is needed; Echo serves the page.

### How It Works

1. The portal shows the gym the upload URL returned by the intake POST: `https://echo-intake-web-production.up.railway.app/u/<token>`.
2. The gym opens that URL on their phone (or desktop), picks photos and videos, adds one optional sentence, and hits send.
3. Files land in R2 under `intake/<client_key>/incoming/` with a UTC timestamp prefix.
4. A sidecar JSON lands alongside: `{ "note": "...", "client": "...", "token_sha256": "...", "timestamp": "...", "filenames": [...] }`. The raw token is never stored; only its SHA-256 fingerprint is recorded.
5. Echo's listener service picks up the R2 drop on its next ingest pass. Files become PENDING library items for staff review.

### Accepted Types

| MIME type | Description |
|---|---|
| `image/jpeg` | |
| `image/png` | |
| `image/webp` | |
| `image/heic` | iPhone HEIC format accepted |
| `image/heif` | |
| `video/mp4` | |
| `video/quicktime` | .mov files |

EXIF data is kept. Files are stored unmodified. No server-side compression or conversion.

### Size Limits

| Limit | Default | Env override |
|---|---|---|
| Per file | 100 MB | `AGENT_INTAKE_MAX_FILE_MB` |
| Per request | 300 MB | `AGENT_INTAKE_MAX_REQUEST_MB` |
| Rate limit | 10 requests per minute per IP | `AGENT_INTAKE_RATE_PER_MINUTE` |
| Storage quota | Per-tenant cap (default 2,048 MB per `tenants.py`) | Set on tenant record |

Exceeding the storage quota returns `413 {"error": "storage quota exceeded; ask us to raise it"}`. The gym sees an error page.

### Tenant Isolation

Files land under `intake/<client_key>/incoming/`. The `client_key` is derived from the token lookup in Echo's env; the gym never sets it. One token resolves to exactly one client key. There is no path traversal (filenames are sanitized with `_safe_name()`). Listing the bucket is never exposed.

### What Happens After Upload

The listener's ingest pass picks up R2 drops. Each upload becomes a PENDING library item. Staff review and approve items before they enter the active content pool. Nothing auto-approves uploaded media.

---

## WhatsApp Lane (PLANNED, behind flag)

WhatsApp is a second intake lane for gyms who prefer to send content via WhatsApp.

**Status:** Behind `AGENT_WHATSAPP_INTAKE_ENABLED` flag (default `false`). Must not be armed until Meta App Review grants the `whatsapp_business_messaging` permission for this use case. See `docs/META_APP_REVIEW_KIT.md` in the Echo repo.

**The portal builds nothing for this lane today.** When it ships, Echo will route WhatsApp media through the same PENDING library flow. The portal display layer will not need to change.

---

## R2 Credentials

Echo reads R2 credentials from its own Railway env: `AGENT_S3_ACCESS_KEY_ID`, `AGENT_S3_SECRET_ACCESS_KEY`, `AGENT_S3_ENDPOINT`, `AGENT_S3_BUCKET`. The portal never holds these credentials and never touches R2 directly.
