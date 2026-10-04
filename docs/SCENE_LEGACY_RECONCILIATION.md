# Scene Legacy Reconciliation — Obligation Manifest (read-only)

Status: **local-only evidence collector. No production writes, no SQL
application, no flag activation, no clearance granted.** PR268 and the draft
SQL remain DRAFT / OFF / UNAPPLIED; this document and tooling change nothing
in production.

## What it is

`scripts/visual_legacy_obligation_manifest.py` turns the saved 2026-10-04
evidence pages (`evidence/media-20261004/` + `MANIFEST_INDEX.json`, described
in `LIVE_MEDIA_PROVENANCE_AUDIT_20261004.md`) into a deterministic per-asset /
per-published-row **obligation ledger**. It is a bounded complement to
`visual_calendar_snapshot.py` and `visual_byte_inventory.py`, which cover
observed calendar rows and delivered bytes but not a complete obligation
ledger.

## Usage

```sh
python3 scripts/visual_legacy_obligation_manifest.py \
  --manifest ../../evidence/media-20261004/MANIFEST_INDEX.json \
  --out /tmp/visual_legacy_obligations.json
# optional, only when Blake supplies saved snapshots later:
#   --ledger-snapshot ledger.json --provider-snapshot provider.json \
#   --deleted-history-snapshot deleted.json
```

The tool never reads the live database and never downloads media. Optional
snapshots are validated as JSON and recorded by checksum only — they do not
clear any obligation.

## Validation performed (hard failures)

- SHA-256 of every page file against `MANIFEST_INDEX.json`.
- Page `kind` (`approved_photo_inventory` /
  `published_calendar_inventory_nontransactional`), sequential `offset`,
  per-page row counts, group totals and unique-ID counts.
- `project_id` and `snapshot_utc` consistency between pages and index.
- Required fields on every asset and published row; duplicate IDs rejected.
- The index must contain **exactly** the `assets` and `published` groups;
  missing or extra groups are hard failures.
- Value checks on every asset row: `kind` must be `photo`,
  `review_status` must be `approved`, and `gym_id` / `source_gym_id` /
  `source_id` must be non-empty; published rows require a non-empty
  `gym_id`.
- Complete referenced page set (missing/truncated pages rejected).
- Path traversal in manifest page paths rejected.

## What the output says

Every obligation carries exact evidence references
(`assets:<id>` / `published:<id>`) and stays `cleared: false` with distinct
unresolved reasons:

- `missing_provider_confirmation` — no provider post records in evidence.
- `missing_deleted_or_swapped_history` — deleted/swapped rows are invisible
  in surviving-row pages.
- `missing_original_to_delivered_byte_proof` — no original-to-delivered byte
  lineage; distinct URLs and absent joins prove nothing.
- `tenant_source_mismatch` — asset `gym_id` differs from `source_gym_id`.
- `cross_tenant_linked_source` — published row's linked source asset has a
  different asset `gym_id` or `source_gym_id` than the row's tenant gym
  (the obligation records all three IDs: row tenant gym, linked asset gym,
  linked asset source gym).
- `missing_source_reference` — published row has neither source asset ID nor
  source URL.
- `source_asset_id_absent_from_approved_snapshot` — linked ID is not among
  the approved photos (may be nonphoto, unapproved or missing; cause not
  proven by this join).

No counter, URL similarity, or absent join grants clearance. Output is
deterministic (sorted keys, no wall-clock fields) and written atomically with
mode 0600.

## Checksum caveat (read before relying on it)

`input_evidence_checksum` is a **consistency check only**, computed over the
SHA-256 values declared in `MANIFEST_INDEX.json`. The manifest index is a
**mutable, locally generated file that is not independently anchored or
signed**. The checksum detects accidental drift between the index and the
saved page files as they exist locally; it is **not tamper-proof** — anyone
editing both a page and the index would pass it. Do not cite the checksum
as integrity proof of the underlying evidence, and never as clearance.

## Counts confirmed against the saved audit (2026-10-04)

| Metric | Value |
| --- | ---: |
| Approved photo assets | 618 |
| Published calendar rows | 1,311 |
| Published rows with neither source reference | 787 |
| Approved-photo tenant/source mismatches | 17 |
| Published rows with cross-tenant linked source | 25 |
| Published rows with source asset ID / source URL | 300 / 316 |
| Distinct source asset IDs / present in approved snapshot | 115 / 57 |
| Cleared assets or rows | **0** |

The source pages were fetched in separate transactions, so this is an
internally checked inventory of a **non-atomic** snapshot — **not a clearance
receipt**. Tests: `tests/test_visual_legacy_obligation_manifest.py`.
