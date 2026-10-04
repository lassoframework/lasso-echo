# Visual media evidence reconciler (offline, deterministic)

`scripts/visual_media_evidence_reconcile.py` reconciles saved historical
visual-media evidence without touching anything live. It never queries a
database, never reads a URL, and never reads media bytes.

## Zero historical clearance

**Every historical-use / asset-clearance obligation is `unresolved`. The
report resolves nothing, ever.** Matching delivered/source hashes are
recorded as `byte_identity_observed` evidence only; they do not prove
original use and never clear an asset. The only thing that could ever
resolve an obligation is a future, separately authenticated, immutable
original-use receipt format. No such receipt is integrated today, so no
obligation can resolve. An omitted provider manifest, a missing gym, or a
perfect hash match all still yield `unresolved`.

## Usage

```
python3 scripts/visual_media_evidence_reconcile.py \
    CALENDAR_SNAPSHOT.json ASSET_SNAPSHOT.json REPORT.json \
    [--byte-observation-manifest BYTES.json] \
    [--provider-manifest PROVIDER.json] \
    [--postlog-manifest POSTLOG.json]
```

- `CALENDAR_SNAPSHOT.json` — output of `scripts/visual_calendar_snapshot.py`
  (`visual-calendar-snapshot-v1`), or any JSON object/array with calendar rows.
- `ASSET_SNAPSHOT.json` — output of `scripts/visual_asset_snapshot.py`
  (`visual-asset-snapshot-v1`), or any JSON object/array with asset rows.
  Real-schema `public.media_asset` rows carry `id`, `source_id`, `gym_id`,
  `kind`, `title`, `mime_type`, `content_hash`, `rendition_key`,
  `rendition_url`, `eligible`, `excluded_by_coach`, `used_count`,
  `last_used_at`, `indexed_at`, `review_status` — with NO `source_media_url`,
  `sha256`, or `md5`. `content_hash` is a Drive MD5 hint only, never
  original-byte proof, and is never validated as a digest or used as byte
  identity; `rendition_url` is a processed-rendition URL and is never treated
  as a source URL. Legacy rows may instead carry `source_media_url`,
  `sha256`/`md5`, and lifecycle `state`. `rendition_key` is an identity hint
  only — a match never proves original use.
  The asset snapshot rejects null or blank values for the live schema's NOT
  NULL columns: `id`, `source_id`, `gym_id`, `kind`, `title`,
  `excluded_by_coach`, `used_count`, `indexed_at`, and `review_status`.
  Nullable selected fields, including MIME/hash/rendition fields, `eligible`,
  and `last_used_at`, are preserved as null when observed.
- Optional manifests: `visual-delivered-byte-inventory-v1` byte observations
  (from `scripts/visual_byte_inventory.py`), provider obligation rows, and a
  publish postlog. Rows join on the calendar row id (`row_ref`). The provider
  manifest may be omitted; a missing or unknown provider obligation is
  recorded and never resolves anything. Every supplied calendar row must have
  a persisted, nonblank `id` (or `row_ref`); the reconciler never fabricates a
  snapshot-row reference. Every supplied provider/postlog row must likewise
  have a nonblank row reference (`row_ref`, with supported persisted aliases).
  An explicitly present but blank/null primary persisted reference (`id` for
  calendar rows, `row_ref` for manifest rows) fails closed even when a
  supported alias carries a nonblank value: aliases are consulted only when
  the primary field is absent from the row. Every supplied byte-observation
  row must be an object with a nonblank `row_ref` (no aliases); duplicate row
  refs across byte rows are rejected.

## Output

The report (`visual-media-evidence-reconciliation-v1`) is written atomically
with owner-only (0600) permissions, sorted JSON keys, and a trailing newline.
It records the path, SHA-256, and row count of every input, a summary with
per-reason counts, one obligation entry per calendar row (sorted by
`row_ref`), and one obligation entry per asset (sorted by `asset_ref`),
including assets with no calendar row — unknown history remains unresolved.
`summary.resolved` is always 0. Replaying the same inputs produces
byte-identical output (deterministic replay).

## Fail-closed integrity checks (abort, exit 2, no output written)

- Missing, blank, duplicate, or ambiguous calendar row refs; no synthetic
  snapshot-row refs are created.
- Missing, blank, or duplicate provider/postlog row refs. A supplied manifest
  is rejected rather than silently dropping a bad row or overwriting a prior
  row. An explicitly present blank primary persisted reference (`id` /
  `row_ref`) aborts even if an alias is nonblank; aliases apply only when the
  primary field is absent.
- Byte-observation manifest rows that are not objects, or that have a missing
  or blank `row_ref`; duplicate byte-row refs are also rejected (no silent
  row skipping or alias substitution).
- Duplicate asset IDs, duplicate asset `source_media_url` values, or
  duplicate `rendition_key` values (ambiguous matching).
- Malformed digest values: any `sha256`/`md5` present that is not full-length
  hex (64/32 chars).
- Exact-URL mismatch between a byte observation and the calendar row's
  delivered or source URL.
- Disagreement between md5 and sha256 when both algorithms are present on
  both compared records and exactly one matches.

## Obligation-level fail-closed reasons

Every obligation is `unresolved` with an explicit reason list in
deterministic precedence order; `original_use_unproven` is always present:

1. `malformed_input` — the row is not a usable object (a wholly malformed
   input file aborts the run with exit 2 before any output).
2. `unknown_date` — missing or unparseable `post_date`.
3. `tenant_mismatch` — calendar row gym differs from the matched asset gym.
4. `missing_tenant` — the calendar row or matched asset has no gym.
5. `deleted_or_swapped_asset` — the matched asset is deleted/swapped/replaced;
   saved evidence can never prove original use for it.
6. `no_matching_asset` — no asset matched by id or source URL (row level), or
   no calendar history references the asset (asset level).
7. `byte_observation_error` — an exact-URL observation failed in the manifest.
8. `no_rendition_identity` — an asset matched but no hash/rendition
   identity exists anywhere to compare; also reported on a per-asset
   obligation when the asset records no `rendition_key`, source URL, or
   digest at all.
9. `provider_obligation_unknown` — the provider manifest is present but
   missing the row or reports an unknown obligation.
10. `source_match_hint_only` — source ID/URL/rendition-key agreement
    without byte identity.
11. `original_use_unproven` — always; no authenticated immutable original-use
    receipt is integrated.

## Hard limits

- Zero historical clearance: the reconciler never clears assets and never
  asserts no prior use.
- `byte_identity_observed` ties a delivered object to saved source bytes
  only; it does not establish original use and does not exclude prior use.
- Source ID and URL matches are hints, never proof of original use.
- Calendar snapshots are non-atomic observed scans; deleted/orphan ledger
  history may be absent from every input.
