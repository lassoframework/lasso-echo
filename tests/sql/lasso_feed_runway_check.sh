#!/usr/bin/env bash
# Synthetic PG17 actual function, no production connection or credentials.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PGBIN=/opt/homebrew/opt/postgresql@17/bin
WORK="$(mktemp -d "${TMPDIR:-/tmp}/lasso_runway_pg.XXXXXX")"
mkdir -p "$WORK/socket"
cleanup() { "$PGBIN/pg_ctl" -D "$WORK/data" -m immediate stop >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT
"$PGBIN/initdb" -D "$WORK/data" -U postgres --auth=trust --no-sync >/dev/null
"$PGBIN/pg_ctl" -D "$WORK/data" -l "$WORK/log" -o "-k $WORK/socket -p 55583 -c listen_addresses='' -c fsync=off" -w start >/dev/null
db() { "$PGBIN/psql" -X -q -A -t -v ON_ERROR_STOP=1 -h "$WORK/socket" -p 55583 -U postgres "$@"; }
db <<'SQL'
create role anon; create role authenticated; create role service_role;
create table content_calendar(id uuid primary key, gym_id text, account text, post_date date,
 slot_index integer,format text,caption text,image_url text,status text,variant_status text,
 logical_post_id uuid,scheduled_at timestamptz,pillar text,media_not_ready_reason text);
create table mode(autonomous boolean); insert into mode values(true);
create function calendar_gym_is_autonomous(gym text) returns boolean language plpgsql as $$ declare a boolean; begin select autonomous into a from mode for share; return a; end $$;
create table echo_infographic_artifacts(tenant text,image_url text,image_sha256 text,source_identity jsonb,evidence jsonb);
SQL
db -f "$ROOT/migrations/lasso_feed_runway_20261008.sql"
db <<'SQL'
create function candidate(account text, slot int, ident uuid) returns jsonb language sql as $$
select jsonb_build_object('id',ident,'gym_id','lasso','account',account,'post_date',(now() at time zone 'America/New_York')::date,
 'slot_index',slot,'format','feed','caption','Approved source copy','image_url','https://cdn.test/'||ident,
 'status','pending','variant_status','active','logical_post_id',ident,'scheduled_at',
 (((now() at time zone 'America/New_York')::date + case slot when 0 then time '07:30' when 1 then time '18:30' else time '12:00' end) at time zone 'America/New_York'),
 'pillar','doctrine'); $$;
create function artifact(r jsonb) returns void language sql as $$
insert into echo_infographic_artifacts values(case r->>'account' when 'instagram' then 'lasso_ig' else 'lasso_fb' end,
 r->>'image_url', repeat('a',64),jsonb_build_object('source_id','content_calendar:'||(r->>'id')||':caption',
 'source_hash',encode(sha256(convert_to(r->>'caption','UTF8')),'hex')),jsonb_build_object('grade_status','PASS',
 'image_sha256',repeat('a',64),'policy_version','current','brain_snapshot',jsonb_build_object('brain','hash'),
 'brief_model','gpt-6-astra','review_response_id','review-real','aspect','4:5'));
$$;
select artifact(candidate('instagram',0,'11111111-1111-4111-8111-111111111111'));
select artifact(candidate('facebook',0,'22222222-2222-4222-8222-222222222222'));
select artifact(candidate('facebook',1,'33333333-3333-4333-8333-333333333333'));
select artifact(candidate('facebook',1,'44444444-4444-4444-8444-444444444444'));
select artifact(candidate('facebook',2,'55555555-5555-4555-8555-555555555555'));
select artifact(candidate('instagram',1,'66666666-6666-4666-8666-666666666666'));
select artifact(candidate('instagram',2,'77777777-7777-4777-8777-777777777777'));
do $$ declare result jsonb; before_ig jsonb; begin
 result:=stage_lasso_runway_feed(candidate('instagram',0,'11111111-1111-4111-8111-111111111111'),'current','{"brain":"hash"}');
 assert result->>'result'='inserted',result::text;
 select to_jsonb(c) into before_ig from content_calendar c where account='instagram';
 result:=stage_lasso_runway_feed(candidate('facebook',0,'22222222-2222-4222-8222-222222222222'),'current','{"brain":"hash"}');
 assert result->>'result'='inserted',result::text;
 assert before_ig=(select to_jsonb(c) from content_calendar c where account='instagram');
 result:=stage_lasso_runway_feed(candidate('instagram',1,'66666666-6666-4666-8666-666666666666')||'{"logical_post_id":null}','current','{"brain":"hash"}');
 assert result->>'result'='inserted',result::text;
 assert (select logical_post_id is null from content_calendar where id='66666666-6666-4666-8666-666666666666');
 result:=stage_lasso_runway_feed(candidate('instagram',1,'66666666-6666-4666-8666-666666666666')||'{"logical_post_id":null}','current','{"brain":"hash"}');
 assert result->>'result'='idempotent',result::text;
 result:=stage_lasso_runway_feed(candidate('instagram',2,'77777777-7777-4777-8777-777777777777')-'logical_post_id','current','{"brain":"hash"}');
 assert result->>'result'='conflict';
 result:=stage_lasso_runway_feed(candidate('instagram',2,'77777777-7777-4777-8777-777777777777')||'{"logical_post_id":"malformed"}','current','{"brain":"hash"}');
 assert result->>'result'='conflict';
 result:=stage_lasso_runway_feed(candidate('instagram',2,'77777777-7777-4777-8777-777777777777')||'{"logical_post_id":7}','current','{"brain":"hash"}');
 assert result->>'result'='conflict';

 result:=stage_lasso_runway_feed(candidate('facebook',0,'22222222-2222-4222-8222-222222222222'),'current','{"brain":"hash"}');
 assert result->>'result'='idempotent';
 result:=stage_lasso_runway_feed(candidate('facebook',0,'22222222-2222-4222-8222-222222222222')||'{"caption":"changed"}','current','{"brain":"hash"}');
 assert result->>'result'='conflict';
 result:=stage_lasso_runway_feed(candidate('facebook',2,'55555555-5555-4555-8555-555555555555')-'account','current','{"brain":"hash"}');
 assert result->>'result'='conflict';
 result:=stage_lasso_runway_feed(candidate('facebook',2,'55555555-5555-4555-8555-555555555555')||'{"account":null}','current','{"brain":"hash"}');
 assert result->>'result'='conflict';
 update mode set autonomous=false;
 result:=stage_lasso_runway_feed(candidate('facebook',2,'55555555-5555-4555-8555-555555555555'),'current','{"brain":"hash"}');
 assert result->>'reason'='autonomy_not_confirmed';
 update mode set autonomous=true;
 result:=stage_lasso_runway_feed(candidate('facebook',2,'55555555-5555-4555-8555-555555555555'),'stale','{"brain":"hash"}');
 assert result->>'result'='conflict';
 result:=stage_lasso_runway_feed(candidate('facebook',2,'55555555-5555-4555-8555-555555555555')||'{"gym_id":"client"}','current','{"brain":"hash"}');
 assert result->>'result'='conflict';
 result:=stage_lasso_runway_feed(candidate('facebook',2,'55555555-5555-4555-8555-555555555555')||'{"publish_claim_token":"anything"}','current','{"brain":"hash"}');
 assert result->>'result'='conflict';
 insert into content_calendar(id,gym_id,account,post_date,slot_index,format,status,variant_status)
 values(gen_random_uuid(),'lasso','facebook',(now() at time zone 'America/New_York')::date,2,'story',null,'active');
 result:=stage_lasso_runway_feed(candidate('facebook',2,'55555555-5555-4555-8555-555555555555'),'current','{"brain":"hash"}');
 assert result->>'result'='occupied';
 assert (select count(*) from content_calendar where format='feed')=3;
end $$;
SQL
# Two distinct source candidates race for the same exact empty platform/slot.
db -c "select stage_lasso_runway_feed(candidate('facebook',1,'33333333-3333-4333-8333-333333333333'),'current','{\"brain\":\"hash\"}');" > "$WORK/a" &
A_PID=$!
db -c "select stage_lasso_runway_feed(candidate('facebook',1,'44444444-4444-4444-8444-444444444444'),'current','{\"brain\":\"hash\"}');" > "$WORK/b" &
B_PID=$!
wait "$A_PID"; wait "$B_PID"
db -c "do \$\$ begin assert (select count(*) from content_calendar where account='facebook' and format='feed' and slot_index=1)=1; end \$\$;"
cat "$WORK/a" "$WORK/b"
echo 'PASS: atomic account isolation, idempotency, stale proof, Story occupancy and concurrent slot claim'
