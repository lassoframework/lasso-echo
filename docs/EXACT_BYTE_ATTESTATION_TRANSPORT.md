# Exact-byte attestation transport (signer -> control bucket -> publisher)

PR #405 runtime verifies every armed exact-byte send against a FRESH (<=300s)
Ed25519-signed lock/serving attestation. The signer and publisher Railway
services have separate volumes, so the signed envelope travels through a
PRIVATE MUTABLE R2 CONTROL bucket, separate from the locked media bucket.

## Services

- **Signer** (`railway.exact-byte-attester.json`, TEMPLATE ONLY): an
  always-on single-replica worker running `agent.exact_byte_attestation_job`.
  Railway's native cron is documented as 5-minute-minimum and best-effort, so
  it cannot keep a <=300s envelope fresh; the worker instead refreshes
  immediately on start and then about every
  `AGENT_EXACT_BYTE_ATTESTATION_REFRESH_SECONDS` (default 60, clamped
  10..240), holds an exclusive flock for its whole lifetime, retries each
  cycle boundedly (3 attempts, 2s/4s backoff), keeps running after a failed
  cycle (never silently exits on transient failures), and stops gracefully on
  SIGTERM/SIGINT. Holds the Cloudflare
  lock-read token, media read credentials, control WRITE credentials and the
  Ed25519 signing seed. Each armed run rebuilds the envelope from live
  Cloudflare REST lock readback + controlled public byte probe (reusing
  `scripts/exact_byte_sign_lock_attestation.py`), requires the
  seed-derived public key to EQUAL the publisher's pinned
  `AGENT_EXACT_BYTE_ADMIN_PUBLIC_KEY_B64` (a wrong or missing pin means
  zero uploads, not an overwrite the publisher would hold on), validates
  envelope freshness before upload (integer non-bool timestamps,
  `observed_at` <= now, age <=300s, `expires_at` > now, signed lifetime
  <=300s, remaining TTL >=60s) so a slow Cloudflare/public probe can never publish an
  already-unusable envelope, verifies it locally, uploads to the fixed
  control object, then proves the readback: exact byte identity,
  re-verified signature/schema/identity and re-validated freshness
  (>=60s remaining TTL). A failed cycle never extends staleness; the worker keeps running and the publisher holds once the last good envelope goes stale.
- **Publisher**: holds ONLY the media read token, a read-only control token
  and the pinned administrator public key. With
  `AGENT_EXACT_BYTE_ATTESTATION_SOURCE=r2-control` plus the pinned control
  env (`AGENT_EXACT_BYTE_CONTROL_R2_ACCOUNT_ID`,
  `AGENT_EXACT_BYTE_CONTROL_R2_BUCKET`,
  `AGENT_EXACT_BYTE_CONTROL_OBJECT_KEY`,
  `AGENT_EXACT_BYTE_CONTROL_R2_RO_ACCESS_KEY_ID`,
  `AGENT_EXACT_BYTE_CONTROL_R2_RO_SECRET_ACCESS_KEY`), every verification
  re-fetches the bounded (<=64KiB) control object with an authenticated
  read-only client. No cached success, no row-supplied value is trusted.

## Source selection is explicit and mutually exclusive

- `local` (or unset): `AGENT_EXACT_BYTE_ATTESTATION_PATH` required, all
  control envs forbidden. Existing behavior, unchanged.
- `r2-control`: all five control envs required, `ATTESTATION_PATH` forbidden.
- Any overlap or unknown value is a fail-closed hold.

## Failure posture

Missing/denied/timeout/truncated/oversized transport, wrong signature,
schema or identity drift, stale or future envelopes all hold the send. A
failure after a previous success still holds (per-send re-fetch). A store
constructed during a transport outage keeps a complete pinned config and
recovers on the next successful fresh fetch; no send is authorized during
the outage. The signer never replaces a good control object with an
envelope that failed local pre-upload verification.

## Arming checklist (manual, secrets in env only)

1. Provision the private control bucket and scoped tokens (signer: write;
   publisher: read-only) in the pinned R2 account.
2. Signer service: set the four secrets, media identity, probe key, serving
   evidence path, the pinned administrator public key
   (`AGENT_EXACT_BYTE_ADMIN_PUBLIC_KEY_B64`, which must equal the
   seed-derived key or the worker refuses with zero uploads), control
   identity, then
   `AGENT_EXACT_BYTE_ATTESTATION_JOB_ENABLED=true`.
3. Publisher service: set control identity + read-only control token,
   `AGENT_EXACT_BYTE_ATTESTATION_SOURCE=r2-control`, and remove
   `AGENT_EXACT_BYTE_ATTESTATION_PATH`.
