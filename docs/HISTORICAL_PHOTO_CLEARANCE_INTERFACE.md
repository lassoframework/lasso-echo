# Historical photo clearance — coordination interface (proposal, 2026-10-08)

Status: PROPOSED by Agent B. Agent A reads this document but does not edit it.
Nothing here clears a photo; the only positive route is the existing
`forward_media_photo_certificate.verify(packet, approved_key, snapshot,
expected_candidate)` plus the owner grant RPCs (`fixer_prepare_owner_photo_
20261007` / `fixer_prepare_owner_staged_photo_20261008`).

## 1. What A produces

A's `tools/historical_photo_review_packet.py` produces an **evidence packet for
independent human/auditor review** — not a clearance and not a signature. **A's
packet cannot yet feed B's certificate path automatically.** The only bridge is
a fail-closed reconciler that binds (a) the exact certificate candidate fields
and (b) the signed corpus snapshot against A's evidence. That reconciler
exists today ONLY as a pure, offline pre-signing check
(`reconcile_review_evidence` in `agent/forward_media_photo_certificate.py`,
see §2a): it raises on any ambiguity and returns no authority. No code maps
A's packet into a signed certificate payload; that mapping is NOT implemented
and must not be assumed from this prose. B consumes A's output only after an
approved independent auditor has reviewed it and produced the signed packet
shape already enforced by `verify`:

- `payload` keys exactly: `schema_version`, `audit_id`, `auditor_id`, `key_id`,
  `policy_id`, `baseline_id`, `generation`, `spine_digest`, `candidate`,
  `dispositions`, `disposition_digest`, `decision`
  (`reviewed_no_prior_visual_use`), `stated_visual_uncertainty` (non-empty).
- `signature_hex`: Ed25519 over the canonical JSON payload.
- Corpus snapshot (from the dedicated RPC, not from A): `policy_approved`,
  `scope_complete`, `policy_id`, `baseline_id`, `generation`, `spine_digest`,
  `rows[]` with `history_key`, `resolved`, `media_kind`, `visual_sha256`,
  `published_binding_ref`, **plus the exclusion-evidence pair**
  `excluded_rows_count` (integer) and `excluded_rows_digest` (`sha256:` hex) —
  see §2a.
- One disposition per corpus row: `history_key`, `disposition`
  (`reviewed_visual_nonmatch` only), `inspected_sha256` equal to the row's
  `visual_sha256`, `published_binding_ref` equal to the row's, and a non-empty
  `review_evidence_ref`.

A's per-object fields (`history_key`, source/delivered/thumbnail URL, byte
SHA/length, content kind, published binding, review disposition, unresolved
reason, coverage counts, pagination ledger, corpus digest) map onto the corpus
rows and dispositions above. A's "unresolved" objects must surface as rows the
signed packet cannot cover — `verify` then holds; A never decides.

## 2. Versioned schema contract

`schema_version = 1` (`CORPUS_SCHEMA_VERSION` in
`agent/forward_media_photo_certificate.py`) is the only accepted version:

- **Still photos only.** Every corpus row must have `media_kind =
  'still_photo'` and `resolved = true`, with a `reviewed_visual_nonmatch`
  disposition bound to the row's exact `visual_sha256` and
  `published_binding_ref`. Video/carousel rows that can carry the photo cannot
  be silently omitted: dispositions must cover every snapshot row exactly
  (count, unique keys, digest), and the signed `spine_digest` binds the exact
  reviewed corpus generation. Any non-still or unresolved row fails closed
  (`certificate_history_unresolved_or_matching`).

## 2a. Exclusion evidence (new, fail closed)

The frozen snapshot RPC `fixer_forward_media_photo_snapshot_20261007()` omits
the baseline's `excluded_rows_json` (video rows held out under a reviewed
exclusion ruling) from `rows`, so signed dispositions can never cover them —
a positive certificate could silently accept excluded history. Under
`schema_version = 1` `verify` therefore REQUIRES explicit, SQL-computed
zero-exclusion evidence in the snapshot:

- `excluded_rows_count`: integer, must be exactly `0`. Missing key, `NULL`,
  boolean, non-integer or any positive value holds
  (`certificate_exclusions_unaccounted`).
- `excluded_rows_digest`: `sha256:` digest of the baseline's
  `excluded_rows_json::text`, must equal the canonical digest of the empty
  array (`digest([])`). A count of `0` with a non-matching digest holds.

Both fields are emitted only by the additive, default-off wrapper
`fixer_forward_media_photo_snapshot_exclusion_20261008()` (DRAFT
`migrations/DRAFT_fixer_photo_historical_clearance_20261008.sql`), which
derives them from the authoritative baseline row and returns the frozen
not-approved shape whenever the scope is unapproved or the baseline is
missing. `IndependentPhotoAuditor` now reads the snapshot ONLY through that
wrapper; old-format snapshots from the frozen RPC fail closed. Accepting any
excluded history row requires a future reviewed frame-aware `schema_version`
with per-frame video/carousel dispositions — never a silent skip.

`reconcile_review_evidence(evidence, snapshot, expected_candidate=None)` (same
module) is a pure offline pre-signing check: it applies the same
zero-exclusion gate, then requires A's packet (`packet_kind
'historical_photo_review_evidence'`, `decision 'review_packet_only'`,
`clearance false`, `packet_status 'complete_for_independent_review'`, zero
unresolved/byte-unreachable/frame-held visuals) to cover EXACTLY the snapshot
`rows` — same unique `history_key` set, every row a resolved still photo with
a `reviewed_nonmatch` disposition whose reachable `delivered.sha256` equals
the row's `visual_sha256` — and, when `expected_candidate` is given, binds the
packet's candidate block to the exact certificate candidate fields (identity
fields, source url/sha256/length, all-or-nothing thumbnail object). It returns
a fact summary only; it is NOT a clearance path and is not wired into any
grant flow.

- **Bounds.** At most `MAX_CORPUS_ROWS = 2500` corpus rows and
  `MAX_PACKET_BYTES = 4 MiB` canonical payload. Larger corpora fail closed
  (`certificate_dispositions_incomplete` / `certificate_shape_invalid`);
  handling them requires a future reviewed `schema_version = 2` with explicit
  frame-level video/carousel dispositions and pagination proof — never
  truncation. v2 is NOT implemented in this package.
- **Candidate binding.** The signed candidate tuple binds tenant, group, post
  date, source asset/URL/MD5/SHA-256/length/receipt, delivered image
  URL/MD5/SHA-256/length, render recipe digest, content digest, and the
  all-or-nothing nullable thumbnail tuple. Renamed, rehosted, cropped or
  thumbnail-colliding derivatives fail `certificate_candidate_changed` or
  `certificate_signature_invalid`. No generic reference string or count proves
  clearance.
- **Freshness.** `baseline_id`/`generation`/`spine_digest` must equal the
  current approved snapshot; any drift holds
  (`certificate_corpus_stale_or_unapproved`). Signer must match an approved
  key/auditor/policy (`certificate_signer_unapproved`).

## 3. Negative-only lookup reconciliation

`agent/forward_media_source_history.py` (`SourceHistoryStore.history`) answers
only "was this exact original registered as sent" from a bounded index.
Absence from it is **never** treated as approval and plays no role in
`verify`. Positive authority flows exclusively: signed certificate →
`IndependentPhotoAuditor.submit/lookup_for_owner` → owner re-verification →
SQL grant RPC under locks. This is documented in the module docstrings of both
files; no code path converts negative absence into clearance.

## 4. Concurrency

Per-audit admission is gated by the `fixer_owner_photo_reserve_20261007` RPC
inside `run_photo_pass` / `run_staged_photo_pass`: exactly one worker reserves
an audit; a non-True result skips it with no duplicate staging, and an
uncertain COMMIT halts the pass (`uncertain_authority_commit`) rather than
re-admitting. Offline coverage: `tests/test_forward_media_historical_
clearance.py::ConcurrentReservationTests`.

## 5. Field changes

Any change to packet/corpus fields requires bumping `schema_version`,
coordinating here first, and keeping `verify` fail-closed on unknown versions
(already enforced: any version ≠ 1 holds `certificate_shape_invalid`). The
exclusion-evidence pair (`excluded_rows_count`, `excluded_rows_digest`) is an
additive snapshot-side tightening within `schema_version = 1`: it only narrows
acceptance (old-format snapshots hold), so it does not change the signed
payload shape and requires no version bump. Accepting positive exclusions is a
field change and DOES require a future reviewed version.
