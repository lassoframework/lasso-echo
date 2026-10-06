"""Run explicit portal notice SQL shape against 0383 and optional draft 0384.

Usage: python tests/test_held_portal_release_pg.py /absolute/portal/checkout
No database URL or production access: disposable Unix-socket PG17 only.
"""
import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

BIN = Path('/opt/homebrew/opt/postgresql@17/bin')
if not (BIN / 'postgres').exists():
    BIN = Path(shutil.which('postgres') or '/nonexistent').parent


def run(argv, **kwargs):
    done = subprocess.run(argv, text=True, capture_output=True, timeout=60, **kwargs)
    if done.returncode:
        raise AssertionError(done.stderr[-5000:])
    return done.stdout.strip()


def main(portal):
    assert '17.' in run([str(BIN / 'postgres'), '--version']), 'PG17 required'
    with tempfile.TemporaryDirectory(prefix='echo-held-portal-pg-') as temp:
        base = Path(temp)
        data = base / 'data'
        run([str(BIN / 'initdb'), '-D', str(data), '-A', 'trust', '--no-locale'])
        run([str(BIN / 'pg_ctl'), '-D', str(data), '-l', str(base / 'server.log'),
             '-o', f"-k {base} -p 55486 -h ''", '-w', 'start'])
        try:
            psql = [str(BIN / 'psql'), '-X', '-q', '-v', 'ON_ERROR_STOP=1',
                    '-h', str(base), '-p', '55486', '-d', 'postgres']
            run(psql, input='''
              create role anon nologin;
              create role authenticated nologin;
              create role service_role nologin bypassrls;
              create schema auth;
              create function auth.jwt() returns jsonb language sql stable
                as $$ select '{}'::jsonb $$;
              create table public.app_users(id uuid primary key, clerk_user_id text);
              create table public.gym_assignments(gym_id uuid, app_user_id uuid);
              create table public.support_tickets(
                id uuid primary key default gen_random_uuid(),
                product text not null, source text not null, client_id text,
                raw_text text not null, classification text, status text not null,
                escalated boolean not null default false, hold_tier text,
                request_version bigint not null default 0,
                bot_identity text, slack_user_id text,
                slack_channel_id text, slack_thread_ts text,
                fix_pr_url text, prior_sha text, lane text,
                resolved_at timestamptz, verification_before jsonb,
                verification_after jsonb);
              create table public.support_messages(
                id uuid primary key default gen_random_uuid(),
                ticket_id uuid not null references public.support_tickets(id),
                author_type text not null, author_id text, body text not null,
                direction text, slack_event_id text, slack_ts text,
                delivery_status text, attachments jsonb,
                created_at timestamptz not null default now());
              alter table public.support_tickets enable row level security;
              alter table public.support_messages enable row level security;
              grant usage on schema public to service_role;
              grant select,insert,update on public.support_messages to service_role;
              grant select on public.support_tickets to service_role;
            ''')
            for name in ('0381_fixer_client_resolution_delivery_guard.sql',
                         '0382_fixer_delivery_resolution_predecessor.sql',
                         '0383_fixer_held_delivery_release.sql'):
                run(psql + ['-f', str(portal / 'supabase/migrations' / name)])
            fixture = Path(__file__).with_name('held_portal_release.verify.sql')
            run(psql + ['-f', str(fixture)])
            print('0383 exact route-null portal held release fixture passed')
            run(psql + ['-f', str(portal / 'supabase/migrations/0384_fixer_current_notice_resolution.sql')])
            run(psql + ['-f', str(fixture)])
            print('0384 draft preserves exact route-null portal held release fixture')
        finally:
            run([str(BIN / 'pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('portal_checkout', type=Path)
    main(parser.parse_args().portal_checkout)
