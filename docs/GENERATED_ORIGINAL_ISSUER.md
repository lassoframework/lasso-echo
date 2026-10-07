# Generated originals issuer preparation

This checkout contains an isolated issuer and read-only evidence backend for the
signed v1 prepare contract. It is default OFF and grants no calendar authority.
No production storage, IAM role, signing key, owner endpoint, or provider call was
created during implementation.

The next owner slice is `agent/forward_media_generated_owner.py` with the unapplied
`DRAFT_fixer_generated_owner_read_20261007.sql`. It implements transport-neutral
POST `/snapshot` and `/history-check` handlers with explicit issuer/verifier read
tokens, per-token tenant allowlists and a dedicated database reader. It defaults
OFF and is not registered on Echo's publisher/intake server. A separately deployed
TLS owner service, credentials and tokens are still absent. Every response is
HOLD with `brand_source_verified`, `photo_inventory_complete`, `history_complete`
and `reviewed_no_match` false. `eligible_photo_count` is null, including an empty
database. Supplied job/copy/palette revisions are echoed for binding diagnostics;
that echo does not prove they came from trusted approved source records.

The draft RPC reads one consistent READ ONLY REPEATABLE READ snapshot, then the
adapter rolls it back before remote I/O. Its digests include current fleet
calendar rows (including undated/unpublished/unknown rows), forward claims/use,
audited originals, photo reservations and the requested tenant's assets/sources.
Counts/digests describe only database observations. They never assert external
Drive inventory completeness, exact/perceptual nonmatch, or full historical
coverage. A published 1,398-row corpus alone cannot fulfill those contracts.
No raw calendar/source rows are returned; read tokens cannot query another
tenant's inventory via these handlers. Malformed or rebound PNG/SHA submissions
hold before the database query.

`stage_prepared_generated_original` is an explicit fail-closed final boundary.
It defaults OFF and still holds if enabled, before any cursor, lock or write.
The existing positive owner grant requires a certified same-gym Drive photo
reservation. A generated original cannot satisfy that source contract, so no
generated clearance, registry row, render manifest or reservation is created.
The missing authority is a trusted generated job bound to tenant/date/palette/
copy, a complete authenticated inventory, full exact/perceptual/undated/unresolved
history, and a generated reservation/claim fence that revalidates all revisions
under the existing graph lock. A signed receipt or constructed preparation object
cannot substitute for those authorities.

The issuer makes a fresh Astra Responses request, preserves the exact inline PNG
before Echo's existing resize helper, performs an independent Astra pixel/copy/
palette review, checks current trusted owner revisions and full reuse history,
then conditionally writes the original and a signed execution envelope. Both need
pinned object versions with verified COMPLIANCE retention. The backend resolves
objects by the owner's job/request identity, authenticates the envelope, retrieves
both provider responses independently, rechecks current sources, inventory and
history, and returns v1 evidence. Mutable creative studio sidecars, ArtifactStore
upserts/cache entries and ordinary media_host URLs are not consulted.

The existing R2 destination is the only destination permitted by the production
factory. Its actual versioning and object-lock API readback must pass. An
unsupported API, absent version, absent COMPLIANCE lock, or mutable credential
scope holds this lane. Do not create another bucket, switch providers, enable
billing, or expand access to make preflight green without separate authorization.

External release prerequisites

1. The storage owner checks the existing destination's versioning and object-lock
   capabilities. If they are unavailable, the exact hold is retained. A separately
   authorized retained storage design must precede a change of destination.
2. Provision separate issuer create-only and verifier read-only credentials. Deny
   the existing media hoster and all producer keys write/delete access to both
   `echo-generated-originals/` and `echo-generated-receipts/`. The issuer must have
   conditional-create-only writes, no delete, overwrite, retention shortening or
   governance bypass. Record independently reviewed policy evidence. Retention
   APIs alone do not establish these IAM denials.
3. Provision an isolated issuer runtime, separate Ed25519 key and independent
   registry approval root. The producer/calendar worker never receives either
   signing key, registry approval private key or namespace write credentials. The
   verifier has no issuer key or write permission. Restrict OpenAI keys by role;
   issuer executes, verifier retrieves stored responses. Confirm actual provider
   permissions and retrieval availability before activation.
4. Complete the trusted read API owned by the final calendar authority. The
   diagnostic adapter above deliberately cannot attest completeness. It must
   authenticate dedicated read-only issuer/verifier tokens. `/snapshot` accepts
   `{"request": GenerationRequest fields}` and returns exactly bound request,
   approved `copy` (headline, facts array, cta, footer), verified `palette`,
   `brand_source_verified`, complete history and photo inventory booleans,
   `eligible_photo_count`, `photo_inventory_revision`, and `history_revision`.
   Copy/palette digests are sha256 of this module's canonical JSON. `/history-check`
   accepts request, exact image SHA and base64 pixels and returns the same request,
   SHA, complete corpus revision, `history_complete` and `reviewed_no_match`.
   Include exact/perceptual matches, undated assets and unresolved history; missing
   data holds. This API is not an adapter around mutable local producer sidecars.
5. The control-plane owner signs a registry envelope with schema_version 1,
   valid_until UTC, `key` matching ApprovedGenerationKey, owner_evidence_url,
   and storage_controls. Required controls and existing destination bindings are
   listed in `configured_lane`. Include audit_owner and audit_receipt_sha256.
   Store the registry in an independently managed read-only deployment mount;
   configure its root public key separately. Runtime cannot issue an approval.
6. Call `configured_lane('issuer').issue(trusted_owner_request)` from an isolated
   authenticated job endpoint. Wire its returned packet into
   `prepare_generated_original(packet, trusted_owner_request,
   configured_lane('verifier'), enabled=True)` in the separate owner service.
   The final owner transaction still must recheck photo/history/source revisions,
   hold its own locks and reserve the object. Preparation is not send clearance.
   Client gap filling stays held until this separate owner integration exists;
   never silently fall back to the ordinary hoster or relabel old artwork.

Offline owner adapter validation

`python -m pytest -q tests/test_forward_media_generated_owner.py
tests/test_forward_media_generated_issuer.py tests/test_forward_media_generated_prepare.py`
checks read-token authentication/tenant scope, request/pixel binding, default OFF,
explicit incomplete responses, and refusal by the existing issuer/verifier client.
`python tests/test_forward_media_generated_owner_pg.py` runs a disposable PG17
fixture with synthetic roles and records. It checks reader/writer separation,
read transaction ownership, fleet/undated/source revision changes, and zero grants
or reservations. Neither command is a production authority or deployment check.

Environment names

`AGENT_GENERATED_ORIGINAL_ISSUER_ENABLED=true` (absent by default),
`AGENT_GENERATED_REGISTRY_PATH`, `AGENT_GENERATED_REGISTRY_ROOT_PUBLIC_HEX`.
For each role prefix `AGENT_GENERATED_ISSUER` / `AGENT_GENERATED_VERIFIER`, set
`_S3_ACCESS_KEY_ID`, `_S3_SECRET_ACCESS_KEY`, `_OPENAI_API_KEY`, `_OWNER_READ_TOKEN`.
Only issuer has `AGENT_GENERATED_ISSUER_PRIVATE_KEY_HEX`. Secrets are set manually
in isolated deployment environments and are never returned by preflight.

Run `python scripts/generated_original_preflight.py --role verifier` for read-only
registry/credential/storage checks. It performs no upload, generation, provider
call, database mutation or provisioning. Add `--request path.json` to read the
trusted owner snapshot. Successful preflight is only the observed check list; it
is not proof of IAM policy enforcement, original issuance or owner acceptance.

Candidate thumbnail handoff

The fixed signed generated v1 payload intentionally has no thumbnail fields. Its
source tuple is `image_url`, `image_sha256`, `image_fingerprint` (MD5), and
`image_length`. The final owner may preserve a legacy null thumbnail tuple or a
full independently attested thumbnail URL/SHA256/MD5/length tuple in candidate
metadata. Never add those fields to the signed v1 payload, which rejects extras,
or infer a thumbnail render edge from an original receipt. The thumbnail owner
must authenticate that separate edge against these exact original bytes.
