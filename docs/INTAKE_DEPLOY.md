# Intake web service — Railway deploy runbook

The texted-link upload page (`python -m agent intake-web`) runs as its OWN
Railway service in the SAME project, pointing at the SAME repo as the worker.
It touches R2 only, never /data (the volume belongs to the listener service).
It ships dark: every route except `/healthz` answers 404 until
`AGENT_INTAKE_ENABLED=true` is set by hand.

## Create the service (click by click)

1. Open the Echo project in Railway (the one running the `listen` worker).
2. Click **+ New** (top right) then **GitHub Repo** and pick `lasso-echo`
   (the same repo the worker deploys from).
3. Railway creates a second service. Rename it: click the service card, then
   the name at the top, and call it `echo-intake-web`.
4. Open the service's **Settings** tab:
   - Under **Deploy**, set **Custom Start Command** to:

         /opt/venv/bin/python -m agent intake-web

     (This overrides railway.json's `listen` startCommand, which belongs to
     the worker. The same line lives in the Procfile as the `web:` entry.)
   - Under **Deploy**, set **Healthcheck Path** to `/healthz`.
     The health route answers 200 with `{"ok": true, "intake_enabled": ...}`
     even while the intake flag is OFF, so a dark service still passes.
   - Do NOT attach a volume. This service must never see /data.
5. Open the service's **Settings > Networking** and click
   **Generate Domain**. Note the URL (e.g. `echo-intake-web.up.railway.app`).
   This is the base for both client links (same token, same env var):
   - intake form: `https://<domain>/intake/<token>` (the gym fills the seven
     section LASSO social intake; answers land as PENDING sources for that
     account and the confirmation offers the upload page)
   - media upload: `https://<domain>/u/<token>`
   The same intake route doubles as the ops portal's API: a POST with a JSON
   body (Content-Type application/json) lands the portal's intake payload the
   same way and returns {status, account_key, pending_source_count, upload_url}.
   Cross-origin calls are allowed ONLY from AGENT_INTAKE_PORTAL_ORIGIN (set it
   to the portal's origin; empty = same-origin only, never all origins).
6. Open the **Variables** tab and set the env vars below.
7. Deploy (Railway auto-deploys on save). Watch the logs for:

       Intake web online on :<port> (enabled: False)

## Environment variables

Railway injects `PORT` automatically; the app binds it. Set the rest by hand.

Shared with the worker (same values; copy them or use Railway shared vars):

| Var | What it is |
|---|---|
| `AGENT_S3_ENDPOINT` | R2 endpoint URL |
| `AGENT_S3_BUCKET` | R2 bucket name |
| `AGENT_S3_REGION` | R2 region (`auto` for Cloudflare) |
| `AGENT_S3_ACCESS_KEY_ID` | R2 access key id (secret, set by hand) |
| `AGENT_S3_SECRET_ACCESS_KEY` | R2 secret key (secret, set by hand) |

This service's own:

| Var | Default | What it is |
|---|---|---|
| `AGENT_INTAKE_ENABLED` | `false` | Master gate. Every route except `/healthz` is 404 until this is `true`. Arm it by hand when go-live is decided. |
| `AGENT_INTAKE_TOKEN_<CLIENTKEY>` | (none) | One per client gym, set by hand. The upload link token; `<CLIENTKEY>` uppercase (e.g. `AGENT_INTAKE_TOKEN_GYM_ALPHA`). Min 8 chars of `[A-Za-z0-9_-]`. Never logged, never persisted (a sha256 fingerprint rides the sidecar). |
| `AGENT_INTAKE_MAX_FILE_MB` | `100` | Per-file size cap. |
| `AGENT_INTAKE_MAX_REQUEST_MB` | `300` | Per-request size cap. |
| `AGENT_INTAKE_RATE_PER_MINUTE` | `10` | Per-IP request rate limit. |
| `AGENT_PORTAL_KEY` | (none) | Shared server-to-server secret for `GET /api/portal/intake-link/<account_key>`. Must match the same var set in Vercel portal env. Generate: `python -c "import secrets; print(secrets.token_urlsafe(40))"`. When unset, the endpoint 401s every request. Never log or expose cross-origin. |

## Portal intake-link endpoint (Option A scalable path)

Once `AGENT_ONBOARD_AUTOMINT=true` and `AGENT_INTAKE_ENC_KEY` are set, each
`onboard` run stores an encrypted token blob in Echo's SQLite DB. The portal
can fetch it automatically (no operator paste at 100-gym scale) using:

    GET /api/portal/intake-link/<account_key>
    X-Portal-Key: <AGENT_PORTAL_KEY value>

The endpoint is server-to-server only (no CORS headers, never call from a browser).
It returns:

    200: { account_key, intake_token_encrypted, token_minted_at }
    401: key missing, wrong, or AGENT_PORTAL_KEY unset
    404: feature off, gym not found, or no encrypted token yet

The portal UPSERTs `intake_token_encrypted` into its `echo_intake_tokens` table
and uses the shared `AGENT_INTAKE_ENC_KEY` to decrypt and build links server-side.
The raw token never leaves Echo; only the Fernet-encrypted blob travels over the wire.

The worker's Slack, Meta, and /data vars are NOT needed here. This service
never posts to Slack and never publishes; it only writes uploads to
`intake/<client>/incoming/` in R2, where the worker's ingest lane
(`AGENT_INTAKE_ENABLED` on the worker side) picks them up.

## Verify it is live

Dark (flag OFF — the expected state right after deploy):

    curl -s https://<domain>/healthz
    # -> {"ok": true, "intake_enabled": false}

    curl -s -o /dev/null -w "%{http_code}\n" https://<domain>/u/anytokenhere
    # -> 404   (everything but /healthz is dark)

Armed (after setting AGENT_INTAKE_ENABLED=true and a client token):

    curl -s https://<domain>/healthz
    # -> {"ok": true, "intake_enabled": true}

    curl -s -o /dev/null -w "%{http_code}\n" https://<domain>/u/<real-token>
    # -> 200   (the upload page)

    curl -s -o /dev/null -w "%{http_code}\n" https://<domain>/u/wrongtoken00
    # -> 404   (unknown token is indistinguishable from off)

End to end: open `https://<domain>/u/<real-token>` on a phone, upload one
photo with a one-line note, then confirm the object appears under
`intake/<client>/incoming/` in the R2 bucket and the worker's next ingest
pass files it into that client's library.
