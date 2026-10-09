"""Disposable-PostgreSQL rehearsal of the media_asset tenant-binding draft
(migrations/DRAFT_media_asset_source_gym_guard_20261005.sql).

Spins up a throwaway PG cluster in a tmpdir (unix-socket only,
dynamic_shared_memory_type=mmap so an exhausted host shm segment does not
matter), applies the DRAFT, and proves:

  * apply succeeds on a clean schema and preserves pre-existing mismatches;
  * a NEW mismatched insert fails, an unrelated-column update of a preserved
    mismatch still succeeds (NOT VALID semantics), a gym_id-moving update fails;
  * re-applying the draft is an idempotent no-op;
  * a same-named WRONG-SHAPE index or constraint makes the draft FAIL CLOSED.

If no usable initdb/pg_ctl/psql exists or the cluster cannot start on this
host, the whole module SKIPS with the real reason — a rehearsal test must never
fake a pass.
"""
import os
import shutil
import socket
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

DRAFT = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "migrations",
                     "DRAFT_media_asset_source_gym_guard_20261005.sql")

SCHEMA = """
CREATE TABLE media_source (id text PRIMARY KEY, gym_id text NOT NULL);
CREATE TABLE media_asset (
  id text PRIMARY KEY,
  source_id text NOT NULL,
  gym_id text NOT NULL,
  kind text,
  title text,
  indexed_at timestamptz DEFAULT now()
);
INSERT INTO media_source VALUES ('src1', 'gymA');
-- a pre-existing mismatch the draft must PRESERVE
INSERT INTO media_asset VALUES ('a_mismatch', 'src1', 'gymB', 'photo', 'old');
"""


def _psql(sockdir, dbname, sql=None, f=None, check=True):
    cmd = ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-h", sockdir,
           "-d", dbname, "-At"]
    if f:
        cmd += ["-f", f]
    r = subprocess.run(cmd, input=sql, capture_output=True, text=True,
                       timeout=120)
    if check and r.returncode != 0:
        raise AssertionError(f"psql failed: {r.stderr}")
    return r


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    if not os.path.exists(DRAFT):
        pytest.skip(f"draft migration not found at {DRAFT}")
    # Prefer a FULL server install's bin dir over a libpq-only shim on PATH
    # (a libpq initdb cannot find the postgres binary and cannot rehearse).
    import glob
    candidates = sorted(glob.glob("/opt/homebrew/opt/postgresql@*/bin"),
                        reverse=True) + [""]
    bins = {}
    for d in candidates:
        bins = {b: shutil.which(b, path=d or None)
                for b in ("initdb", "pg_ctl", "psql")}
        pg_bin = os.path.join(os.path.dirname(bins["initdb"] or "/dev/null"),
                              "postgres") if bins.get("initdb") else ""
        if all(bins.values()) and os.path.exists(pg_bin):
            break
    missing = [b for b, p in bins.items() if not p]
    if missing:
        pytest.skip(f"disposable-PG rehearsal needs {missing} on PATH")
    base = tmp_path_factory.mktemp("pgrehearsal")
    data, sockdir, logf = (str(base / "data"), str(base / "sock"),
                           str(base / "pg.log"))
    os.mkdir(sockdir)
    try:
        r = subprocess.run(
            [bins["initdb"], "-D", data, "-U", "postgres", "--auth=trust",
             "--no-sync", "-E", "UTF8"],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            pytest.skip(f"initdb failed on this host: {r.stderr.strip()[-400:]}")
        r = subprocess.run(
            [bins["pg_ctl"], "-D", data, "-l", logf, "-o",
             f"-k {sockdir} -h '' -c listen_addresses='' "
             "-c dynamic_shared_memory_type=mmap -c shared_buffers=16MB "
             "-c max_connections=10", "start"],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            tail = ""
            try:
                tail = open(logf).read()[-600:]
            except OSError:
                pass
            pytest.skip(f"disposable PG could not start on this host "
                        f"(shm exhaustion or sandbox): {tail}")
        # wait for readiness
        for _ in range(50):
            if socket.socket(socket.AF_UNIX).connect_ex(
                    os.path.join(sockdir, ".s.PGSQL.5432")) == 0:
                break
            import time
            time.sleep(0.2)
        else:
            pytest.skip("disposable PG socket never became ready")
        _psql(sockdir, "postgres", "SELECT 1")
        yield sockdir
    finally:
        subprocess.run([bins["pg_ctl"], "-D", data, "-m", "immediate", "stop"],
                       capture_output=True, timeout=60)


def _fresh_db(pg, name):
    _psql(pg, "postgres", f"CREATE DATABASE {name}")
    _psql(pg, name, SCHEMA)
    return name


def test_draft_apply_enforcement_and_idempotence(pg):
    db = _fresh_db(pg, "rehearsal_ok")
    _psql(pg, db, f=DRAFT)                       # applies cleanly
    # mismatch preserved and readable
    out = _psql(pg, db, "SELECT count(*) FROM media_asset a "
                        "JOIN media_source s ON s.id = a.source_id "
                        "WHERE a.gym_id IS DISTINCT FROM s.gym_id").stdout
    assert out.strip() == "1"
    # a NEW bad row fails
    r = _psql(pg, db, "INSERT INTO media_asset VALUES "
                      "('a_bad', 'src1', 'gymZ', 'photo', 'bad')", check=False)
    assert r.returncode != 0 and "media_asset_source_gym_fkey" in r.stderr
    # a gym_id-MOVING update of the preserved mismatch fails
    r = _psql(pg, db, "UPDATE media_asset SET gym_id = 'gymZ' "
                      "WHERE id = 'a_mismatch'", check=False)
    assert r.returncode != 0 and "media_asset_source_gym_fkey" in r.stderr
    # an UNRELATED-column update of the preserved mismatch still succeeds
    # (exact NOT VALID semantics: unrelated columns are not policed)
    _psql(pg, db, "UPDATE media_asset SET title = 'still fine' "
                  "WHERE id = 'a_mismatch'")
    # a good new row still inserts
    _psql(pg, db, "INSERT INTO media_asset VALUES "
                  "('a_good', 'src1', 'gymA', 'photo', 'ok')")
    # the FK is present and still NOT VALID; the index is shared/unique
    out = _psql(pg, db, "SELECT convalidated FROM pg_constraint "
                        "WHERE conname = 'media_asset_source_gym_fkey'").stdout
    assert out.strip() == "f"
    out = _psql(pg, db, "SELECT i.indisunique FROM pg_index i "
                        "JOIN pg_class c ON c.oid = i.indexrelid "
                        "WHERE c.relname = 'media_source_id_gym_key'").stdout
    assert out.strip() == "t"
    # idempotent: re-applying the whole draft is a verified no-op
    _psql(pg, db, f=DRAFT)


def test_draft_fails_closed_on_wrong_shape_objects(pg):
    db = _fresh_db(pg, "rehearsal_shape")

    # (1) wrong-shape INDEX: same name, but non-unique
    _psql(pg, db, "CREATE INDEX media_source_id_gym_key "
                  "ON media_source (id, gym_id)")
    r = _psql(pg, db, f=DRAFT, check=False)
    assert r.returncode != 0 and "wrong shape" in r.stderr, r.stderr
    _psql(pg, db, "DROP INDEX media_source_id_gym_key")

    # (2) wrong-shape index columns (unique, but wrong column set)
    _psql(pg, db, "CREATE UNIQUE INDEX media_source_id_gym_key "
                  "ON media_source (gym_id)")
    r = _psql(pg, db, f=DRAFT, check=False)
    assert r.returncode != 0 and "wrong shape" in r.stderr, r.stderr
    _psql(pg, db, "DROP INDEX media_source_id_gym_key")

    # (3) wrong-shape CONSTRAINT: right name, wrong FK columns
    _psql(pg, db, "CREATE UNIQUE INDEX media_source_id_gym_key "
                  "ON media_source (id, gym_id)")
    _psql(pg, db, "ALTER TABLE media_asset ADD CONSTRAINT "
                  "media_asset_source_gym_fkey FOREIGN KEY (source_id) "
                  "REFERENCES media_source (id) NOT VALID")
    r = _psql(pg, db, f=DRAFT, check=False)
    assert r.returncode != 0 and "wrong shape" in r.stderr, r.stderr
