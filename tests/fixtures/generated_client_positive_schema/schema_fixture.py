"""Disposable full schema prerequisite; never a production restore or positive CLI proof.

Uses the existing release installer's labeled synthetic base and exact migrations.
No seed executes, controls stay OFF, and no external transport or /data is used.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import random
import subprocess
import tempfile

from tests import test_generated_client_release_manifest_pg as release

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = Path(__file__).with_name('frozen_inputs.json')
TENANT = 'fixture-gym'
PRINCIPALS = {
    'owner': ('positive_owner', 'fixer_forward_media_owner_20261006'),
    'reader': ('positive_reader', 'generated_hosted_byte_reader_20261009'),
    'producer': ('positive_producer', 'generated_issuer_dispatch_producer_20261009'),
    'issuer': ('positive_issuer', 'generated_issuer_dispatch_issuer_20261009'),
    'service': ('positive_service', 'service_role'),
}


def verify_frozen_inputs():
    for relative, expected in json.loads(MANIFEST.read_text()).items():
        actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        assert actual == expected, f'frozen schema input changed: {relative}'


@contextmanager
def disposable_schema():
    import psycopg
    verify_frozen_inputs()
    binaries = release.pg17_bin_dir()
    if binaries is None:
        raise RuntimeError('positive_schema_prerequisite_missing: PostgreSQL 17 server binaries')
    with tempfile.TemporaryDirectory(prefix='echo_positive_schema_', dir='/tmp') as directory:
        temp = Path(directory)
        socket = temp / 'socket'
        socket.mkdir()
        port = random.randint(41000, 59000)
        subprocess.run([str(binaries/'initdb'), '-D', str(temp/'cluster'), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=60)
        subprocess.run([str(binaries/'pg_ctl'), '-D', str(temp/'cluster'), '-l', str(temp/'pg.log'),
                        '-o', f"-k {socket} -p {port} -c listen_addresses=''", '-w', 'start'],
                       check=True, capture_output=True, timeout=60)
        try:
            def connect(login):
                return psycopg.connect(host=str(socket), port=port, dbname='postgres',
                                       user=login, autocommit=True)
            with connect('postgres') as admin:
                release.install(admin)
                release.apply(admin, ROOT/'migrations/DRAFT_generated_issuer_dispatch_20261009.sql')
                for login, role in PRINCIPALS.values():
                    admin.execute(psycopg.sql.SQL(
                        'create role {} login inherit nosuperuser nocreatedb nocreaterole noreplication nobypassrls; grant {} to {}'
                    ).format(psycopg.sql.Identifier(login), psycopg.sql.Identifier(role), psycopg.sql.Identifier(login)))
                admin.execute('grant generated_hosted_byte_issuer_20261009 to positive_issuer')
                admin.execute("insert into generated_hosted_byte_principals_20261009 values ('positive_reader',%s,false,true),('positive_issuer',%s,true,false)", (TENANT,TENANT))
                admin.execute("insert into generated_issuer_dispatch_principals_20261009 values ('positive_producer',%s,true,false),('positive_issuer',%s,false,true)", (TENANT,TENANT))
                admin.execute("insert into generated_client_control_20261009 values ('positive_owner',%s,'positive_reader',false)", (TENANT,))
                yield admin, connect
        finally:
            subprocess.run([str(binaries/'pg_ctl'), '-D', str(temp/'cluster'), '-m', 'immediate', '-w', 'stop'],
                           check=True, capture_output=True, timeout=60)
