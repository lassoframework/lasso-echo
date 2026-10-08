# Historical photo corpus and review evidence packet

Date: 2026-10-08. Status: DRAFT, offline, read-only. Owner of this document:
Agent A of the bounded K3 package (brief
`work/evidence/echo-historical-clearance-k3-swarm-brief-20261008.md`).
`tools/historical_photo_review_packet.py` builds an **evidence packet for
independent human/auditor review**. It is not a clearance, not a signature,
and never produces an automatic positive decision. The existing
certificate/owner route (`agent/forward_media_photo_certificate.py`,
`agent/forward_media_owner_photo_prepare.py`) remains the only authority path;
nothing here creates eligibility, provisions keys, applies SQL, clears a live
photo, or publishes. The companion clearance-interface contract, if present,
is owned by Agent B (`docs/HISTORICAL_PHOTO_CLEARANCE_INTERFACE.md`) and is
read-only for this lane.

## Corpus scope

The corpus is the **complete fleet send history across all tenants and all
send routes**, reconciled per visual:

- `status`, `published_at`, `late_post_id` (every send route, including rows
  that only reached draft/pending/failed),
- provider receipt refs, publish claim refs and reservation refs,
- the historical original (`source`), the delivered object (`delivered`),
  the thumbnail when present, and any available snapshots,
- `media_kind`: `still_photo`, `video`, `carousel`, `other`. Video and
  carousel visuals must carry COMPLETE per-frame byte digests (`frames`) with
  an attested `frames_total` and `frames_coverage_ref`, or an explicit
  `frames_hold_reason`; they are never silently omitted, and one frame of a
  multi-frame item is never sufficient (see below).

The current photo-pool census
(`work/evidence/echo-photo-pool-current-census-20261008.md`) lists 143
`used_count=0` approved photos (Zanshin 88, ENG 39, Bolton Club 11, TopFuel
5). Those are candidates only — a registry counter, not proof of nonuse. The
verified send census referenced by `agent/forward_media_source_history.py`
includes 1,398 published rows and 1,050 distinct delivered URLs; re-census at
audit time, never treat counts as a spec.

## Input export

The tool consumes one authoritative offline JSON export:

- `cutover`: `cutover_id`, timezone-aware `cutover_at`, `evidence_refs` —
  the immutable cutover the corpus was frozen at.
- `scope_manifest`: the independent, SIGNED cutover scope manifest (see next
  section). Required; a missing, unsigned, altered or stale manifest, or one
  signed by an unapproved key, holds the build.
- `candidate`: exactly one candidate's authenticated source/version binding:
  `calendar_row_id` (UUID), `tenant_id`, `group_key`, `post_date`,
  `source_asset_id`, `source_version_id`, `source_receipt_ref`, and
  all-or-nothing object refs `{url (https), sha256 (sha256:…), length}` for
  `source`, `delivered` and optional `thumbnail`, plus `evidence_refs` and
  optional `hold_reasons`. Missing or ambiguous bytes must be expressed as
  `hold_reasons`; they hold the whole packet.
- `pages`: stable paginated corpus pages (see below).
- `dispositions`: exactly one independent review ruling per corpus
  `history_key`: `exact_match`, `visual_match`, `reviewed_nonmatch`, or
  `unresolved`. Non-unresolved rulings require a `review_evidence_ref` AND
  `inspected_objects`: one `{sha256, evidence_ref}` entry for **every
  inspected byte object** of the visual — source, delivered, thumbnail (when
  present), every snapshot and every frame — an exact set match (no
  omissions, no extras, no duplicate shas), each with a non-empty immutable
  evidence ref. `unresolved` requires an explicit `unresolved_reason`, no
  evidence ref, and `inspected_objects: null`.

## Signed cutover scope manifest (schema_version 2)

Completeness is proven against an INDEPENDENT signed manifest, never against
the export's own claims. A self-reported null-to-null page chain is transport
evidence only; by itself it NEVER means the corpus is complete.

Shape:

```json
{
  "key_id": "scope-key-1",
  "signature_hex": "<ed25519 signature, 128 hex chars>",
  "payload": {
    "manifest_kind": "historical_photo_cutover_scope",
    "cutover_id": "…",
    "cutover_at": "<tz-aware iso ts>",
    "generated_at": "<tz-aware iso ts>",
    "source_query_id": "<query/source identity, e.g. fleet-send-corpus-query:v7>",
    "expected_history_keys_digest": "sha256:<canonical-json digest of the SORTED complete history-key list>",
    "expected_history_keys_count": <int, independently attested>,
    "tenant_route_counts": [
      {"tenant_id": "…", "send_route": "…", "expected_count": <int>}
    ]
  }
}
```

- The signature is Ed25519 over the canonical JSON payload. Approved public
  keys are supplied **by the caller at run time** (`build_packet(export,
  approved_keys={key_id: pub_hex})`, CLI `--approved-key KEY_ID:HEX`,
  repeatable, required). Keys are NEVER embedded in the tool, the export, or
  the manifest. No approved key, an unknown `key_id`, or a malformed
  signature holds (`scope_manifest_key_unapproved` /
  `scope_manifest_unsigned`); a valid-format signature that does not verify
  holds (`scope_manifest_altered`).
- `tenant_route_counts` must have unique (tenant, route) pairs; duplicate
  pairs are a shape hold.

Reconciliation (`reconcile_scope`) is exact and fail-closed:

1. `cutover_id` + `cutover_at` must equal the export cutover — stale or
   mismatched cutover holds (`scope_manifest_stale`).
2. The exported history-key count must equal `expected_history_keys_count`
   (`scope_manifest_count_mismatch`) and the canonical digest of the sorted
   exported key list must equal `expected_history_keys_digest`
   (`scope_manifest_key_set_mismatch`).
3. The per-(tenant, route) counts of the export must equal
   `tenant_route_counts` exactly — an omitted tenant, an omitted route, or a
   phantom attested pair holds (`scope_manifest_tenant_route_mismatch`).

Any missing, stale, unsigned, altered, or omitted-tenant/route/page evidence
results in a hold. There is no "trusted exporter" shortcut.

## Pagination ledger and transport completeness

Pages form an explicit cursor chain: the first page's `cursor` is null, each
`next_cursor` equals the following page's `cursor`, and the final
`next_cursor` is null. Every page carries `row_count == len(rows)` and a
`page_digest` (canonical-JSON sha256 over its rows); both are re-verified.
Any truncation flag — `truncated`, `row_limit_applied`, `byte_limit_applied`,
`limit_applied`, `has_more` — fails the build with `pagination_truncated`.
The historical 2,500-row lookup cap (`SourceHistoryStore.history` default)
and the 4 MiB signed-packet cap are **not** absorbed silently: a corpus that
exceeds them must be exported as more pages, and any export produced under a
cap must say so and fail here. The page chain proves only that the export
transport was not truncated; corpus completeness is proven solely by the
signed scope manifest reconciliation above.

## Frame coverage for video/carousel

A video or carousel visual must either carry `frames_hold_reason` (explicit
hold, surfaces in `packet_status: hold`) or COMPLETE frame coverage:

- `frames_total`: integer ≥ 1, the attested total frame count,
- `frames_coverage_ref`: non-empty immutable attestation ref for that total,
- `frames`: unique, ascending `frame_index` values exactly `0..frames_total-1`,
  each `{frame_index, sha256, length}`.

One frame of a multi-frame item fails (`visual_frames_incomplete`), as do
duplicate/unsorted indices, a missing attestation ref, or a missing/invalid
total (`visual_frames_coverage_required`). Every frame sha must additionally
appear in the disposition's `inspected_objects` binding.

## Packet semantics

The emitted packet (`schema_version: 2`,
`packet_kind: historical_photo_review_evidence`) contains:

- `decision: "review_packet_only"`, `clearance: false`,
  `no_automatic_positive_decision: true` — always, unconditionally.
- `packet_status`: `hold` when the candidate has hold reasons or any visual
  is unresolved, has unreachable bytes, or is a frame-held video/carousel;
  otherwise `complete_for_independent_review`. Neither value clears anything;
  both merely state whether the evidence is complete enough for an
  independent reviewer to rule.
- `candidate`: the authenticated source/version block, unchanged.
- `scope_manifest`: the validated manifest facts (cutover identity,
  query/source identity, expected counts/digest, key id, manifest digest) —
  no key material, no signature.
- `visuals`: every corpus visual with its stable `history_key`, full
  send-route reconciliation fields, per-object reachability (`{url, sha256,
  length, reachable: true}` or `{url, reachable: false}` — unreachable bytes
  are recorded, never dropped), frames with `frames_total` /
  `frames_coverage_ref` or frames-hold, the review `disposition`,
  `review_evidence_ref`, the per-object `inspected_objects` evidence binding,
  and explicit `unresolved_reasons`.
- `coverage`: totals, per-disposition and per-media-kind counts, unresolved /
  byte-unreachable / frames-held counts, page count.
- `pagination_ledger`: cursor, next cursor, row count and page digest per
  page.
- `corpus_digest` (sha256 over the canonical visuals list), `cutover_digest`
  (sha256 over cutover identity + candidate + corpus digest + scope manifest
  digest) and `packet_digest` (sha256 over the whole packet) — immutable
  references for cross-checking at audit and at any later certificate step.
- `evidence_refs`: union of cutover and candidate evidence refs.

`PacketHold` reasons are static strings only; URLs, bytes and export contents
never leak into errors. CLI output files are written `0600`.

## Usage

```
python3 tools/historical_photo_review_packet.py export.json -o packet.json \
  --approved-key scope-key-1:<ed25519 pub hex>
```

Prints `{packet_status, packet_digest, coverage}`. Tests:
`python3 -m pytest tests/test_historical_photo_review_packet.py -q`
(requires `cryptography` for manifest verification).

## Non-goals and gaps

- No database access, no network reads, no agent imports: byte digests must
  already be authenticated upstream (source verifier / receipts); this tool
  checks shape, completeness and coverage, it does not fetch bytes.
- `exact_match` / `visual_match` are recorded as independent-review rulings
  with evidence refs; the packet flags but never adjudicates them.
- The packet is unsigned. **This packet cannot yet feed Agent B's
  certificate path** (`forward_media_photo_certificate.verify`): per
  `docs/HISTORICAL_PHOTO_CLEARANCE_INTERFACE.md` §1/§2a, the only bridge is a
  fail-closed reconciler binding (a) the exact certificate candidate fields
  and (b) the signed corpus snapshot (including the `excluded_rows_count` /
  `excluded_rows_digest` zero-exclusion pair) against this evidence. Today
  that exists ONLY as the pure offline pre-signing check
  `reconcile_review_evidence`, which returns no authority; no code maps this
  packet into a signed certificate payload. B's `schema_version = 1` corpus
  contract accepts still photos only, so any video/carousel row — even one
  with complete frame coverage here — remains outside the certificate scope
  until a future reviewed frame-aware version. Any mismatch, stale corpus or
  missing media kind must fail closed on that side.
