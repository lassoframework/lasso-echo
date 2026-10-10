#!/usr/bin/env python3
"""Operator CLI for the exact-byte historical seed preparation (20261010).

Read-only against the source DB. Connects through libpq defaults/environment
(psycopg.connect with no arguments); this process never reads, prints or logs
any secret value. Runs inside a single READ ONLY, REPEATABLE READ snapshot so
the keyset-paginated census is consistent. It never executes the generated
SQL, never activates the gate and never writes to any table.

Output artifacts are PRIVATE: the manifest and seed SQL contain source media
URLs and content_calendar row ids. `--out` must be a NEW owner-only directory
(mode 0700); artifact files are written mode 0600. The tool fails before any
write if the output path already exists and is not an empty directory, so a
blocked rerun can never leave stale usable SQL beside an incomplete manifest.
Raw URLs are never printed to stdout/stderr.

Usage:
  python3 scripts/exact_byte_history_seed_20261010.py \
      --mapping mapping.json --out /path/to/new-outdir \
      --allowed-host cdn.example.com [--allowed-host ...]

Exit codes: 0 complete seed artifacts written; 1 blocked (manifest only,
INCOMPLETE_DO_NOT_APPLY); 2 hard failure (no artifacts guaranteed).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.exact_byte_history_seed_20261010 import (  # noqa: E402
    BASELINE_MIN_ROWS, PAGE_SIZE, SeedError, make_https_fetcher, run,
)

CENSUS_SQL = """
select c.id::text as row_id,
       public.exact_byte_row_revision_20261010(c) as revision,
       to_jsonb(c) as row
from public.content_calendar c
where (c.status = 'published' or c.published_at is not null
       or c.late_post_id is not null)
  and (%s::uuid is null or c.id > %s::uuid)
order by c.id
limit %s
"""

SNAPSHOT_SQL = "select public.exact_byte_historical_snapshot_20261010()"


class PgReader:
    """Single read-only repeatable-read snapshot census reader."""

    def __init__(self, conn):
        self._conn = conn

    def census_page(self, after_id, limit):
        with self._conn.cursor() as cur:
            cur.execute(CENSUS_SQL, (after_id, after_id, limit))
            return [{'row': r[2], 'revision': r[1]} for r in cur.fetchall()]

    def historical_snapshot(self):
        with self._conn.cursor() as cur:
            cur.execute(SNAPSHOT_SQL)
            return cur.fetchone()[0]


def prepare_output_dir(path):
    """Fail closed unless `path` is absent (created 0700) or an empty dir.

    A pre-existing non-directory or non-empty directory is rejected BEFORE any
    write so a blocked rerun cannot leave stale usable SQL beside an
    incomplete manifest. Existing empty directories are forced to 0700.
    """
    if os.path.exists(path):
        if not os.path.isdir(path) or os.listdir(path):
            raise SeedError('output path already exists and is not an empty '
                            'directory; refusing to write beside stale '
                            'artifacts')
        os.chmod(path, 0o700)
    else:
        os.mkdir(path, 0o700)


def write_artifact(path, text):
    """Owner-only (0600), exclusive-create artifact write."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--mapping', required=True,
                        help='explicit tenant/target/sibling/no-media/census-'
                             'review mapping JSON')
    parser.add_argument('--out', required=True,
                        help='NEW output directory (created 0700; must not '
                             'already exist non-empty)')
    parser.add_argument('--allowed-host', action='append', default=[],
                        help='exact HTTPS media host allowed for observation fetches')
    parser.add_argument('--page-size', type=int, default=PAGE_SIZE)
    parser.add_argument('--baseline-min', type=int, default=BASELINE_MIN_ROWS,
                        help='census floor; may be raised above %d but never '
                             'lowered' % BASELINE_MIN_ROWS)
    args = parser.parse_args(argv)

    if args.page_size < 1 or args.page_size > 10000:
        print('page size out of bounds', file=sys.stderr)
        return 2
    # The floor can be raised by the operator but never lowered.
    baseline_min = max(int(args.baseline_min), BASELINE_MIN_ROWS)
    try:
        with open(args.mapping, 'r', encoding='utf-8') as fh:
            mapping = json.load(fh)
        prepare_output_dir(args.out)
    except (SeedError, OSError, json.JSONDecodeError) as exc:
        print('seed preparation failed closed: %s' % exc, file=sys.stderr)
        return 2

    try:
        import psycopg
    except ImportError:
        print('psycopg is required (already a project dependency)', file=sys.stderr)
        return 2

    try:
        with psycopg.connect(connect_timeout=10) as conn:
            conn.execute('set transaction isolation level repeatable read read only')
            reader = PgReader(conn)
            fetcher = make_https_fetcher(args.allowed_host)
            result = run(reader, mapping, fetcher=fetcher,
                         baseline_min=baseline_min, page_size=args.page_size)
    except SeedError as exc:
        print('seed preparation failed closed: %s' % exc, file=sys.stderr)
        return 2

    out = args.out
    manifest_path = os.path.join(
        out, 'exact_byte_history_seed_manifest_20261010.json')
    write_artifact(manifest_path,
                   json.dumps(result.manifest, indent=2, sort_keys=True) + '\n')

    if result.seed_sql is None:
        print('BLOCKED: manifest only (INCOMPLETE_DO_NOT_APPLY): %s' % manifest_path)
        for b in result.blockers[:20]:
            print('  blocker: %s' % b, file=sys.stderr)
        return 1

    seed_path = os.path.join(out, 'exact_byte_history_seed_20261010.sql')
    readback_path = os.path.join(out, 'exact_byte_history_readback_20261010.sql')
    write_artifact(seed_path, result.seed_sql)
    write_artifact(readback_path, result.readback_sql)
    print('COMPLETE_UNAPPLIED artifacts written (owner-only; contain source '
          'URLs and row ids):')
    print('  seed:     %s (sha256 %s)' % (seed_path, result.manifest['seed_sql_sha256']))
    print('  readback: %s' % readback_path)
    print('  manifest: %s' % manifest_path)
    print('Seed SQL was NOT executed; the gate remains OFF. Apply and readback '
          'are separate authorized operator steps, each bracketed by a '
          'separately privileged read-only gate-OFF receipt.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
