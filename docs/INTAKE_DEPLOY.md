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
   This is the base for both client links (same signed token, minted from the
   one shared secret; see "Minting a gym's link" below):
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
| `AGENT_INTAKE_SIGNING_SECRET` | (none) | The ONE shared secret that signs every gym's link. Set it ONCE here (and on the listener service, which mints links in SMS replies). NEVER set it on the ops portal. Long random string, treat like a password. Never logged; the token carries only a client key + an HMAC, never this value. See "Minting a gym's link" below. |
| `AGENT_INTAKE_TOKEN_<CLIENTKEY>` | (none) | **LEGACY / optional.** A pinned per-gym token, still honored as an override so a gym already on an old link keeps it during the cutover. Not needed for new gyms once the signing secret is set. `<CLIENTKEY>` uppercase (e.g. `AGENT_INTAKE_TOKEN_GYM_ALPHA`). Min 8 chars of `[A-Za-z0-9_-]`. Never logged, never persisted (a sha256 fingerprint rides the sidecar). |
| `AGENT_INTAKE_MAX_FILE_MB` | `100` | Per-file size cap. |
| `AGENT_INTAKE_MAX_REQUEST_MB` | `300` | Per-request size cap. |
| `AGENT_INTAKE_RATE_PER_MINUTE` | `10` | Per-IP request rate limit. |

The worker's Slack, Meta, and /data vars are NOT needed here. This service
never posts to Slack and never publishes; it only writes uploads to
`intake/<client>/incoming/` in R2, where the worker's ingest lane
(`AGENT_INTAKE_ENABLED` on the worker side) picks them up.

## Minting a gym's link (no per-gym env var, no redeploy)

Every gym's link is derived from the one `AGENT_INTAKE_SIGNING_SECRET`. To get a
gym's links, run this where the secret lives (the listener service shell, or any
box with the secret and `AGENT_UPLOAD_BASE_URL` set):

    python -m agent intake-link --account gym_alpha_ig

It prints the signed intake form link and the media upload link. Hand them to the
gym. No env change, no redeploy, no restart. Onboard the 2nd through 100th gym the
same way. The secret is never printed.

The token format is `b64url(client_key).b64url(hmac)`: the client key is carried
in the link and the HMAC proves it was minted with our secret. The service
recomputes the HMAC on each request; a tampered or forged link is a 404.

Follow-up (not built yet, no rewrite needed): the ops portal will mint a gym's
link automatically on client-add by calling an authenticated Echo endpoint that
wraps the same `intake_web.link_for()` the CLI uses. The portal never holds the
secret; the secret stays on this service.

### Killing one gym's link (revocation)

Rotating the shared secret would kill EVERY gym's link at once. To kill ONE gym
(a churned client, a leaked link) without touching the rest:

    python -m agent intake-revoke --account gym_alpha_ig      # link now 404s
    python -m agent intake-unrevoke --account gym_alpha_ig    # link works again

Revocation is an R2 denylist the intake-web service reads at verify time (it
touches R2 only, never /data). A revoked gym's link is a 404 everywhere, exactly
like an unknown token.

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
