"""Disposable local PostgreSQL bootstrap for migration contract tests.

A session-scoped throwaway cluster (initdb in pytest's tmp dir, Unix-socket
only, no TCP listener) models the Echo/Supabase roles (anon, authenticated,
service_role with BYPASSRLS — matching Supabase's service_role). Each test
then gets a fresh public schema with the base media_source/media_asset schema
plus the DRAFT migration under test. Nothing here can reach a live plane: the
DSN is a Unix socket inside the pytest tmp directory.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

BASE_SCHEMA = "media_source_media_asset_20260827.sql"


def _pg_bindir():
    """Full server bin dir (initdb + pg_ctl + postgres + psql).

    PATH may carry the libpq CLIENT-only install, whose initdb fails with
    'program postgres is needed by initdb but was not found' — so prefer a
    homebrew postgresql@* server directory and require the server binary.
    """
    for opt in sorted(Path("/opt/homebrew/opt").glob("postgresql@*/bin"), reverse=True):
        if all((opt / tool).exists() for tool in ("initdb", "pg_ctl", "postgres", "psql")):
            return opt
    if all(shutil.which(t) for t in ("initdb", "pg_ctl", "postgres", "psql")):
        return Path(shutil.which("postgres")).parent
    pytest.skip("local PostgreSQL server binaries (initdb/pg_ctl/postgres/psql) not found")


def _run(argv, **kw):
    return subprocess.run(argv, text=True, capture_output=True, timeout=120, **kw)


class DisposablePostgres:
    """One throwaway cluster; sql()/sql_rc() run statements against its socket."""

    def __init__(self, tmpdir: Path):
        bindir = _pg_bindir()
        self.pg_ctl = bindir / "pg_ctl"
        # macOS limits Unix socket paths to roughly 104 bytes. pytest's nested
        # temp path can exceed that once PostgreSQL appends .s.PGSQL.<port>.
        self.sockdir = Path(tempfile.mkdtemp(prefix="hmc_sock_", dir="/tmp"))
        self.datadir = tmpdir / "data"
        # Socket-only cluster: the port number is inert (no TCP listener),
        # but postgres still requires one; keep it out of the ephemeral range.
        self.port = 55444
        init = _run([str(bindir / "initdb"), "-D", str(self.datadir),
                     "-N", "--no-sync", "-E", "UTF8"], cwd=tmpdir)
        if init.returncode:
            # The /tmp socket directory is created before initdb; a failed
            # initdb must not leak it (pytest's tmp dir only covers datadir).
            shutil.rmtree(self.sockdir, ignore_errors=True)
            pytest.skip("disposable PostgreSQL cluster cannot start in this environment (seatbelt denies shmget/socket planes): " + init.stderr.strip()[:200])
        start = _run([str(bindir / "pg_ctl"), "-D", str(self.datadir), "-w", "-t", "60",
                      "-l", str(self.datadir / "logfile"),
                      "-o", f"-k {self.sockdir} -p {self.port} -c listen_addresses='' "
                      "-c unix_socket_directories={self.sockdir}".format(self=self),
                      "start"], cwd=tmpdir)
        if start.returncode:
            log = self.datadir / "logfile"
            detail = log.read_text()[-1200:] if log.exists() else start.stderr.strip()
            # pg_ctl may time out after postgres starts. Confirm it has
            # stopped before removing the directory that owns its socket.
            status = _run([str(self.pg_ctl), "-D", str(self.datadir), "status"])
            if status.returncode == 0:
                self.stop()
            shutil.rmtree(self.sockdir, ignore_errors=True)
            raise RuntimeError("disposable PostgreSQL start failed: " + detail)
        self.dsn = f"host={self.sockdir} port={self.port} dbname=postgres"
        done = _run(["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                     "-d", self.dsn, "-c",
                     "create database echo_historical_clearance_test"])
        if done.returncode:
            self.stop()
            pytest.skip("test database creation failed: " + done.stderr.strip()[:300])
        self.dsn = f"host={self.sockdir} port={self.port} dbname=echo_historical_clearance_test"

    def stop(self):
        # Use the selected server's pg_ctl: the client-only libpq install on
        # PATH may not ship pg_ctl at all (or ships one for the wrong version).
        stopped = _run([str(self.pg_ctl), "-D", str(self.datadir), "-m", "fast", "-w", "stop"],
                       cwd=self.datadir.parent)
        if stopped.returncode:
            status = _run([str(self.pg_ctl), "-D", str(self.datadir), "status"])
            if status.returncode == 0:
                raise RuntimeError("disposable PostgreSQL did not stop; socket retained: "
                                   + str(self.sockdir) + " " + stopped.stderr.strip())
        shutil.rmtree(self.sockdir, ignore_errors=True)

    def sql(self, statement, *, dbname=None):
        dsn = self.dsn if dbname is None else \
            self.dsn.rsplit("dbname=", 1)[0] + "dbname=" + dbname
        done = _run(["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                     "-v", "VERBOSITY=verbose",
                     "-d", dsn, "-c", statement])
        if done.returncode:
            raise RuntimeError(done.stderr.strip())
        return done.stdout.strip()

    def sql_rc(self, statement, *, dbname=None):
        """Return SQLSTATE-bearing stderr for fail-closed assertions."""
        dsn = self.dsn if dbname is None else \
            self.dsn.rsplit("dbname=", 1)[0] + "dbname=" + dbname
        done = _run(["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                     "-v", "VERBOSITY=verbose",
                     "-d", dsn, "-c", statement])
        return done.returncode, done.stderr.strip()


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory):
    server = DisposablePostgres(tmp_path_factory.mktemp("hmc_pg"))
    yield server
    server.stop()


@pytest.fixture()
def hmc_db(pg_server):
    """Fresh public schema: Supabase-ish roles + base schema + DRAFT migration + seed."""
    pg = pg_server
    pg.sql("do $$ begin "
           "if not exists(select from pg_roles where rolname='anon') then create role anon; end if; "
           "if not exists(select from pg_roles where rolname='authenticated') then create role authenticated; end if; "
           "if not exists(select from pg_roles where rolname='service_role') then create role service_role; end if; "
           "end $$;", dbname="echo_historical_clearance_test")
    pg.sql("alter role service_role bypassrls", dbname="echo_historical_clearance_test")
    pg.sql("drop schema public cascade; create schema public; "
           "grant usage on schema public to anon,authenticated,service_role;",
           dbname="echo_historical_clearance_test")
    pg.sql((MIGRATIONS / BASE_SCHEMA).read_text(), dbname="echo_historical_clearance_test")
    pg.sql((MIGRATIONS / "DRAFT_historical_media_clearance_20261003.sql").read_text(),
           dbname="echo_historical_clearance_test")
    # Seed: gymx/src1/asset1 (md5 hash), gymy/src2/asset2, and assetX in gymx
    # whose source_id is deliberately gymy's src2 (cross-tenant poisoning
    # attempt the record RPC must refuse).
    pg.sql(
        "insert into public.media_source(id,gym_id,folder_id,connected_at) values "
        "('src1','gymx','folder1',now()),('src2','gymy','folder2',now());"
        "insert into public.media_asset(id,source_id,gym_id,kind,title,content_hash,indexed_at) values "
        "('asset1','src1','gymx','photo','a1','0123456789abcdef0123456789abcdef',now()),"
        "('asset2','src2','gymy','photo','a2','fedcba9876543210fedcba9876543210',now()),"
        "('assetX','src2','gymx','photo','ax','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',now());",
        dbname="echo_historical_clearance_test")
    return pg
