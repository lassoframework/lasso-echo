#!/usr/bin/env bash
# Disposable PostgreSQL proof for the one-time CrossFit Sunnyside source rebind and
# its rollback. It uses a throwaway local cluster and synthetic rows only.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PGBIN=""
for candidate in /opt/homebrew/opt/postgresql@17/bin /opt/homebrew/opt/postgresql@16/bin; do
  if [[ -x "$candidate/initdb" && -x "$candidate/postgres" && -x "$candidate/pg_ctl" ]]; then
    PGBIN="$candidate"
    break
  fi
done
if [[ -z "$PGBIN" ]]; then
  INITDB_PATH="$(command -v initdb || true)"
  [[ -n "$INITDB_PATH" ]] && PGBIN="$(dirname "$INITDB_PATH")"
fi
PSQL="${PGBIN:+$PGBIN/}psql"
INITDB="${PGBIN:+$PGBIN/}initdb"
PG_CTL="${PGBIN:+$PGBIN/}pg_ctl"
if [[ ! -x "$PSQL" || ! -x "$INITDB" || ! -x "$PG_CTL" || ! -x "$PGBIN/postgres" ]]; then
  echo "SKIP: psql/initdb/pg_ctl are required for the disposable PostgreSQL check"
  exit 0
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/ss_media_rebind_pg.XXXXXX")"
SOCK="$WORK/sock"
DATA="$WORK/data"
LOG="$WORK/postgres.log"
PORT=55458
mkdir -p "$SOCK"
cleanup() {
  "$PG_CTL" -D "$DATA" -m immediate stop >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

"$INITDB" -D "$DATA" -U postgres --auth=trust --no-sync --no-instructions >/dev/null
"$PG_CTL" -D "$DATA" -l "$LOG" \
  -o "-k $SOCK -p $PORT -c listen_addresses='' -c fsync=off" -w start >/dev/null

db() {
  "$PSQL" -X -q -A -t -v ON_ERROR_STOP=1 \
    -h "$SOCK" -p "$PORT" -U postgres -d postgres "$@"
}
q() { db -c "$1" | tr -d '\r'; }
apply_sql() { db -f "$ROOT/migrations/DRAFT_crossfit_sunnyside_media_source_rebind_20261007.sql"; }
rollback_sql() { db -f "$ROOT/migrations/DRAFT_rollback_crossfit_sunnyside_media_source_rebind_20261007.sql"; }
expect_fail() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "FAIL: expected refusal: $label" >&2
    exit 1
  fi
  echo "ok: refused $label"
}
expect_value() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$actual" != "$expected" ]]; then
    echo "FAIL: $label expected [$expected], got [$actual]" >&2
    exit 1
  fi
  echo "ok: $label"
}

db <<'SQL'
CREATE TABLE public.gyms (
  id uuid PRIMARY KEY, name text NOT NULL
);
CREATE TABLE public.echo_intake_tokens (
  gym_id uuid NOT NULL, echo_account_key text NOT NULL
);
CREATE TABLE public.media_source (
  id text PRIMARY KEY, gym_id text NOT NULL, kind text NOT NULL,
  active boolean NOT NULL, revoked_externally boolean NOT NULL,
  sync_status text NOT NULL, sync_requested_at timestamptz,
  sync_started_at timestamptz, sync_finished_at timestamptz,
  sync_error text, sync_claim_token text, folder_id text UNIQUE,
  connected_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE public.media_asset (
  id text PRIMARY KEY, source_id text NOT NULL REFERENCES public.media_source(id),
  gym_id text NOT NULL, kind text NOT NULL, eligible boolean,
  review_status text
);
CREATE TABLE public.content_calendar (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), gym_id text NOT NULL,
  source_media_asset_id text, status text NOT NULL, approval_kind text
);

INSERT INTO public.gyms VALUES
  ('f574c06c-498a-45f8-a599-b2a8863fadfb', 'CrossFit Sunnyside'),
  ('99999999-9999-4999-8999-999999999999', 'Synthetic Other Gym');
INSERT INTO public.echo_intake_tokens VALUES
  ('f574c06c-498a-45f8-a599-b2a8863fadfb', 'crossfitsunnysidef574c0'),
  ('99999999-9999-4999-8999-999999999999', 'othergym999999');
INSERT INTO public.media_source
  (id,gym_id,kind,active,revoked_externally,sync_status,
   sync_requested_at,sync_claim_token,folder_id)
VALUES
  ('10f4bf47d5e24c4086fce5aa916e6768','crossfitsunnyside2616ac','gym_drive',
   true,false,'idle',NULL,NULL,'synthetic-drive-folder');
INSERT INTO public.media_asset(id,source_id,gym_id,kind,eligible,review_status)
SELECT 'ss-asset-' || n,
       '10f4bf47d5e24c4086fce5aa916e6768',
       'crossfitsunnysidef574c0',
       CASE WHEN n <= 9 THEN 'photo' ELSE 'video' END,
       CASE WHEN n <= 2 OR n = 10 THEN true ELSE false END,
       CASE WHEN n <= 13
            THEN 'approved' ELSE 'pending_review' END
  FROM generate_series(1,13) AS n;
INSERT INTO public.content_calendar(gym_id,source_media_asset_id,status,approval_kind)
SELECT 'crossfitsunnysidef574c0', id, CASE WHEN id IN ('ss-asset-1','ss-asset-2')
                                    THEN 'approved' ELSE 'pending' END,
       CASE WHEN id IN ('ss-asset-1','ss-asset-2') THEN 'operator' ELSE NULL END
  FROM public.media_asset
 WHERE substring(id from 10)::integer BETWEEN 1 AND 13;
SQL
db -c "DELETE FROM public.content_calendar WHERE source_media_asset_id='bad-asset';
         DELETE FROM public.media_asset WHERE id='bad-asset';
         INSERT INTO public.media_asset VALUES
           ('bad-asset','10f4bf47d5e24c4086fce5aa916e6768','other-tenant','photo',true,'approved');"
expect_fail "cross-tenant linked asset" apply_sql
expect_value "cross-tenant asset refusal left source unchanged" "crossfitsunnyside2616ac" \
  "$(q "SELECT gym_id FROM public.media_source WHERE id='10f4bf47d5e24c4086fce5aa916e6768'")"
db -c "DELETE FROM public.media_asset WHERE id='bad-asset';"

db -c "INSERT INTO public.media_source
         (id,gym_id,kind,active,revoked_externally,sync_status,folder_id)
       VALUES ('synthetic-conflict','crossfitsunnysidef574c0','gym_drive',true,false,'idle','other-folder');"
expect_fail "another source already on canonical key" apply_sql
db -c "DELETE FROM public.media_source WHERE id='synthetic-conflict';"

db -c "INSERT INTO public.content_calendar(gym_id,source_media_asset_id,status)
       VALUES ('other-tenant','ss-asset-1','pending');"
expect_fail "cross-tenant calendar reference" apply_sql
db -c "DELETE FROM public.content_calendar WHERE gym_id='other-tenant';"

db -c "UPDATE public.media_source SET sync_status='indexing'
       WHERE id='10f4bf47d5e24c4086fce5aa916e6768';"
expect_fail "active sync state" apply_sql
db -c "UPDATE public.media_source SET sync_status='idle'
       WHERE id='10f4bf47d5e24c4086fce5aa916e6768';"
db -c "CREATE FUNCTION public.synthetic_source_trigger() RETURNS trigger
       LANGUAGE plpgsql AS \$\$ BEGIN RETURN NEW; END \$\$;
       CREATE TRIGGER synthetic_source_trigger BEFORE UPDATE ON public.media_source
       FOR EACH ROW EXECUTE FUNCTION public.synthetic_source_trigger();"
expect_fail "unreviewed media_source trigger" apply_sql
expect_value "trigger refusal left source unchanged" "crossfitsunnyside2616ac" \
  "$(q "SELECT gym_id FROM public.media_source WHERE id='10f4bf47d5e24c4086fce5aa916e6768'")"
db -c "DROP TRIGGER synthetic_source_trigger ON public.media_source;
       DROP FUNCTION public.synthetic_source_trigger();"

BEFORE="$(q "SELECT (SELECT count(*) FROM public.media_asset),
                    (SELECT count(*) FROM public.media_asset WHERE review_status='approved'),
                    (SELECT count(*) FROM public.content_calendar WHERE status='approved'),
                    (SELECT count(*) FROM public.content_calendar WHERE approval_kind='operator')")"
apply_sql
expect_value "source rebound to live portal key" "crossfitsunnysidef574c0" \
  "$(q "SELECT gym_id FROM public.media_source WHERE id='10f4bf47d5e24c4086fce5aa916e6768'")"
expect_value "all linked assets now match active source tenant" "13" \
  "$(q "SELECT count(*) FROM public.media_asset a JOIN public.media_source s ON s.id=a.source_id WHERE s.id='10f4bf47d5e24c4086fce5aa916e6768' AND s.active AND a.gym_id=s.gym_id")"
expect_value "all linked calendar rows remain on canonical tenant" "13" \
  "$(q "SELECT count(*) FROM public.content_calendar c JOIN public.media_asset a ON a.id=c.source_media_asset_id JOIN public.media_source s ON s.id=a.source_id WHERE s.id='10f4bf47d5e24c4086fce5aa916e6768' AND c.gym_id=s.gym_id")"
apply_sql # repeat is a verified idempotent no-op
expect_value "asset/review/calendar state unchanged" "$BEFORE" \
  "$(q "SELECT (SELECT count(*) FROM public.media_asset),
               (SELECT count(*) FROM public.media_asset WHERE review_status='approved'),
               (SELECT count(*) FROM public.content_calendar WHERE status='approved'),
               (SELECT count(*) FROM public.content_calendar WHERE approval_kind='operator')")"

db -c "INSERT INTO public.media_asset VALUES
       ('after-rebind','10f4bf47d5e24c4086fce5aa916e6768','crossfitsunnysidef574c0','photo',true,'approved');"
expect_fail "rollback after asset manifest changed" rollback_sql
expect_value "failed rollback left source canonical" "crossfitsunnysidef574c0" \
  "$(q "SELECT gym_id FROM public.media_source WHERE id='10f4bf47d5e24c4086fce5aa916e6768'")"
db -c "DELETE FROM public.media_asset WHERE id='after-rebind';"
rollback_sql
expect_value "rollback restored stale fail-closed key" "crossfitsunnyside2616ac" \
  "$(q "SELECT gym_id FROM public.media_source WHERE id='10f4bf47d5e24c4086fce5aa916e6768'")"
rollback_sql # rollback repeat is idempotent
apply_sql
expect_value "forward rebind after rollback" "crossfitsunnysidef574c0" \
  "$(q "SELECT gym_id FROM public.media_source WHERE id='10f4bf47d5e24c4086fce5aa916e6768'")"

echo "PASS: CrossFit Sunnyside media-source rebind and rollback preconditions"
