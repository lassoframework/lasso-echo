# API Contract: Echo Endpoints the Portal Calls

Base URL: `https://echo-intake-web-production.up.railway.app`

All endpoints are on the **intake-web** Railway service (`agent/intake_web.py`). No other Echo service exposes a portal-facing HTTP API today.

---

## Auth Model

Every gym has one token. The token is an opaque string that Echo mints by hand (set in Railway env as `AGENT_INTAKE_TOKEN_<CLIENTKEY>`). The token is never plain-text in portal code, chat, or git. Echo stores tokens in its own gyms table. When `AGENT_INTAKE_ENC_KEY` is set in Echo's Railway env, the token is Fernet-encrypted at rest in the gyms table (the portal never decrypts it; Echo reconstructs the upload link server-side). The portal sends the token in the URL path on intake calls. On portal gym status calls, the portal uses the account key instead.

Echo validates the token by looking it up in its own Railway env. A bad token returns the same `404 Not Found` as a disabled flag. The raw token is never logged by Echo.

---

## Endpoints

### GET /healthz

**Purpose:** Service liveness check. Answers even when `AGENT_INTAKE_ENABLED=false`. Use this to confirm the service is up before showing the intake form.

**Auth:** None.

**Response 200:**
```json
{
  "ok": true,
  "intake_enabled": true
}
```

`intake_enabled` is `false` when the service is up but intake is not armed. Display a holding message if `false`.

**Example:**
```
GET https://echo-intake-web-production.up.railway.app/healthz
```

---

### GET /intake/<token>

**Purpose:** Serves the gym-facing HTML intake form. The portal can link the gym directly to this URL or embed the form in an iframe.

**Auth:** Token in path. `404` when the flag is off or token is unknown (indistinguishable).

**Response 200:** HTML page (LASSO V3 palette, mobile first). The portal does not need to re-render the form; it can redirect the gym here.

**Error responses:**
- `404 Not Found` — flag off or bad token.

---

### POST /intake/<token>

Two content types are accepted at this route. The portal uses `application/json`.

#### Portal JSON intake (server-to-server)

**Purpose:** Submit the gym's completed onboarding intake from the portal's wizard. The payload lands in R2 as `intake/<client>/incoming/<stamp>_intake.json` for Echo's listener to route through `submit_intake()` as PENDING sources. Nothing auto-approves.

**Auth:** Token in path. Same 404 behavior as GET.

**CORS:** The portal origin must be set in Echo's env as `AGENT_INTAKE_PORTAL_ORIGIN` (e.g. `https://ops.lassoframework.com`). Absent Origin (server-to-server call) is always allowed. Cross-origin from an unlisted origin is rejected with `403`.

**Request:**
```
POST /intake/<token>
Content-Type: application/json
```

Body schema (all fields optional except `gym.name`):

```json
{
  "gym": {
    "name": "District H",
    "locations": ["Indianapolis, IN"],
    "website": "https://districth.com",
    "ig_handle": "@districth",
    "fb_page": "districthfitness"
  },
  "voice": {
    "vibe": "Direct, community-first, never corporate",
    "words_to_use": ["strong", "real", "community"],
    "words_to_never_use": ["journey", "transformation", "grind"],
    "sample_post_links": []
  },
  "offers": {
    "front_door_offer": "21-day new member intro",
    "services": ["small group training", "open gym"],
    "exact_pricing_wording": "21 days for $21"
  },
  "audience": {
    "ideal_member": "Busy Indianapolis adults 28 to 45 who want real results without a big gym atmosphere",
    "prior_struggles": "Gyms that feel intimidating or impersonal"
  },
  "proof": {
    "wins": ["Jake dropped 18 lbs in 8 weeks and came back for more"],
    "verifiable_numbers": []
  },
  "media_notes": "Feature real members over stock. Avoid wide-angle distortion.",
  "approver": {
    "name": "Marcus Webb",
    "role": "Owner",
    "cell": "+13175550100",
    "email": "marcus@districth.com"
  }
}
```

Field normalization (verified against `normalize_portal_intake()` in `agent/intake_web.py`):

| JSON path | Maps to `answers` field | Notes |
|---|---|---|
| `gym.name` | `gym_name` | Required; 400 if missing |
| `gym.locations` | `city` | List joined with newline |
| `gym.website` | `website` | String |
| `gym.ig_handle` | `ig_handle` | Stored but not a PENDING source |
| `gym.fb_page` | `fb_page` | Stored but not a PENDING source |
| `voice.*` | `voice` | Assembled as labelled text blocks |
| `offers.front_door_offer` | `offers` | |
| `offers.services` | `services` | |
| `offers.exact_pricing_wording` | `pricing_rule` | Used verbatim or not at all |
| `audience.ideal_member` + `audience.prior_struggles` | `audience` | |
| `proof.wins` + `proof.verifiable_numbers` | `proof` | |
| `media_notes` | `media_notes` | |
| `approver.name` + `approver.role` | `approver_name` | Combined as "Name (Role)" |
| `approver.cell` + `approver.email` | `approver_contact` | Comma joined |

Max field length: 4,000 chars (truncated silently on each field).

**Response 200:**
```json
{
  "status": "received",
  "account_key": "districth",
  "pending_source_count": 7,
  "upload_url": "https://echo-intake-web-production.up.railway.app/u/<token>"
}
```

- `account_key`: the client key Echo resolved from the token.
- `pending_source_count`: how many fact lines landed as PENDING sources (for display only; the real count collapses duplicates at ingest so a re-POST may yield fewer).
- `upload_url`: the media upload link. Show this to the gym immediately after intake submit: "Your intake is in. Now upload your photos and videos."

**Error responses:**
- `400 {"error": "gym.name is required"}` — `gym.name` is blank.
- `400 {"error": "the intake is empty"}` — all other fields are blank.
- `400 {"error": "invalid JSON"}` — body not parseable.
- `403 Not allowed` — Origin not on the allowlist.
- `404 Not Found` — flag off or bad token.
- `503 {"error": "storage unavailable"}` — R2 credentials not set in Echo's env.

**Re-POST behavior:** Safe. A re-POST lands a fresh payload. Sources dedupe at ingest. The account proposal (gym basics + approver) replaces the held one in place.

---

### POST /u/<token>

**Purpose:** Gym uploads photos and videos. This is the media upload lane. The portal shows the gym this URL after intake completes, typically as a button.

**Auth:** Token in path. Same 404 behavior.

**Request:** `multipart/form-data`
- `media` (required, multiple): image or video files.
- `note` (optional): one sentence about the upload, max 500 chars.

Accepted types: `image/jpeg`, `image/png`, `image/webp`, `image/heic`, `image/heif`, `video/mp4`, `video/quicktime`.

Limits:
- Per-file: 100 MB default (env `AGENT_INTAKE_MAX_FILE_MB`).
- Per-request: 300 MB default (env `AGENT_INTAKE_MAX_REQUEST_MB`).
- Rate: 10 requests per minute per IP (env `AGENT_INTAKE_RATE_PER_MINUTE`).
- Storage quota: per-tenant cap enforced; 413 when exceeded.

**Response:** HTML page ("Got it. Your content is in."). No JSON response on this route.

**Error responses:**
- `400 upload rejected` — bad file type or over size limit.
- `404 Not Found` — flag off or bad token.
- `413 too large` — request too large or storage quota exceeded.
- `429 slow down` — rate limited.
- `503 {"error": "storage unavailable"}` — R2 not configured.

---

### GET /u/<token>

**Purpose:** Serves the gym-facing HTML media upload page. The portal redirects or links the gym here. The page is mobile-first, pick-and-send, no login.

**Auth:** Token in path.

**Response 200:** HTML upload page.

**Error:** `404` — flag off or bad token.

---

### GET /portal/gym/<account_key>

**Purpose:** Portal gym status. Returns the gym's token status, upload link, last upload timestamp, and upload count. The portal uses this to show staff whether a gym's intake is armed and what they have uploaded.

**Gate:** Requires `AGENT_PORTAL_APPROVALS=true` in Echo's Railway env. Returns `403 {"error": "portal access is disabled"}` when the flag is off.

**Auth:** No token in path. The account key is not a secret. The portal must be server-side when calling this endpoint (do not expose from a client-side browser context).

**CORS:** Same origin rules as the intake endpoint. Set `AGENT_INTAKE_PORTAL_ORIGIN` to the portal's origin.

**Response 200:**
```json
{
  "account_key": "districth",
  "upload_link": "https://echo-intake-web-production.up.railway.app/u/<token>",
  "token_status": "ACTIVE",
  "last_upload_at": "20260718T193000Z",
  "upload_count": 7,
  "intake_status": "ACTIVE"
}
```

- `token_status` / `intake_status`: `ACTIVE`, `REVOKED`, or `NOT_SET`.
- `upload_link`: the gym's upload URL (reconstructed from Fernet-decrypted token when `AGENT_INTAKE_ENC_KEY` is set, else from stored plaintext). `null` when unavailable.
- `last_upload_at`: UTC timestamp string from the most recent R2 media key. `null` when R2 is not configured or no uploads yet.
- `upload_count`: count of media objects in R2 `intake/<account_key>/incoming/`. `null` when R2 is not configured.

**Error responses:**
- `403 {"error": "portal access is disabled"}` — `AGENT_PORTAL_APPROVALS=false`.
- `404 {"error": "gym not found"}` — account key not in gyms table.

---

## PLANNED Endpoints (not yet built)

These endpoints do not exist in Echo today. They are specified here so the portal CC can build stubs and wire them when Echo ships them. Mark all portal UI that depends on these as **PLANNED** until STATUS.md says otherwise.

### GET /api/calendar/<account_key>?month=YYYY-MM

**Purpose:** Returns the 30-day calendar for a gym.

**Auth (planned):** Bearer token or signed request with the gym token. Exact auth TBD.

**Planned response:**
```json
{
  "account_key": "districth",
  "month": "2026-07",
  "days": [
    {
      "date": "2026-07-01",
      "drafts": [
        {
          "draft_id": "abc123",
          "status": "pending",
          "caption_preview": "First 40 chars of caption...",
          "platform": "instagram",
          "scheduled_for": "2026-07-01T18:30:00-04:00"
        }
      ]
    }
  ]
}
```

States: `pending`, `approved`, `skipped`, `superseded`, `expired`, `blocked`.

**Portal behavior until live:** Display a static "approval happens in Slack" message on the calendar page.

---

### POST /api/approve/<account_key>/<draft_id>

**Purpose:** Portal-native approval action. Gated behind `AGENT_PORTAL_APPROVALS` flag (not yet defined in `agent/config.py` as of HEAD `fea0e31`). Slack is the only approval channel today.

**Planned request:**
```json
{
  "action": "approve" | "edit" | "skip" | "deny" | "kill",
  "note": "optional edit instruction",
  "actor_slack_id": "U06EPUUCL13",
  "confirmed": true
}
```

**Planned response:**
```json
{
  "ok": true,
  "action": "approve",
  "draft_id": "abc123",
  "detail": "published: media_id=12345"
}
```

**Portal behavior until live:** All approval actions display "Approve this post in your Slack channel." Link to the gym's Slack channel.

---

### GET /api/report/<account_key>?days=30

**Purpose:** Returns the assembled 30-day report.

**Planned response:** The `build_report()` output shape from `agent/reporting.py`:
```json
{
  "account_key": "districth",
  "window_days": 30,
  "engagement_rate": 0.048,
  "engagement_rate_baseline": 0.039,
  "followers": 1842,
  "followers_net": 47,
  "followers_growth_rate": 0.026,
  "posting_freq_current": 12,
  "posting_freq_baseline": 4,
  "top_posts": [...],
  "bottom_posts": [...],
  "gaps": [],
  "health": "growing"
}
```

**Portal behavior until live:** Display a "Reporting coming soon" holding card.
