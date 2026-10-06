#!/bin/bash
# Disposable-Postgres check for migrations/calendar_approval_provenance_20261005.sql.
# Spins up a throwaway cluster in $TMPDIR, builds a MINIMAL schema (only the
# columns the migration's functions touch), applies the DRAFT migration, and
# asserts the proof-gate contract. Destroys the cluster on exit. No live data.
# NOTE: needs an environment that permits Postgres shared memory (shmget) —
# run it OUTSIDE the Codex sandbox (e.g. a maintainer shell with
# PATH=/opt/homebrew/opt/postgresql@17/bin:$PATH).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/approval_proof_pg.XXXXXX")"
SOCK="$WORK/sock"; DATA="$WORK/data"; LOG="$WORK/pg.log"
mkdir -p "$SOCK"
cleanup() { pg_ctl -D "$DATA" -m immediate stop >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT

initdb -D "$DATA" -U postgres --no-sync >/dev/null
pg_ctl -D "$DATA" -l "$LOG" -o "-k $SOCK -p 55444 -c listen_addresses=''" -w start >/dev/null

psql -h "$SOCK" -p 55444 -U postgres -d postgres -v ON_ERROR_STOP=1 -q <<'SQL'
create role anon;
create role authenticated;
create role service_role;
create table gyms (id uuid primary key, slug text, name text);
create table echo_intake_tokens (gym_id uuid, echo_account_key text);
create table echo_gym_settings (gym_id uuid primary key, autonomous boolean,
                                autonomy_updated_by text);
create table content_calendar (
  id uuid primary key, gym_id text, status text,
  published_at timestamptz, late_post_id text, variant_status text,
  image_url text, media_not_ready_reason text, account text, format text,
  post_date date, caption text, scheduled_at timestamptz,
  source_media_asset_id text, source_media_url text, byte_hash text,
  publish_reservation_day date, publish_claim_token uuid,
  pillar text, gbp_topic_type text, gbp_cta_type text, gbp_cta_url text,
  gbp_event jsonb, gbp_offer jsonb, gbp_location_id text);
SQL

psql -h "$SOCK" -p 55444 -U postgres -d postgres -v ON_ERROR_STOP=1 -q \
  -f "$ROOT/migrations/calendar_approval_provenance_20261005.sql"

PASS=0; FAIL=0
check() { # name expected actual
  if [ "$2" = "$3" ]; then PASS=$((PASS+1)); echo "ok   - $1";
  else FAIL=$((FAIL+1)); echo "FAIL - $1 (expected [$2], got [$3])"; fi
}
q() { psql -h "$SOCK" -p 55444 -U postgres -d postgres -v ON_ERROR_STOP=1 -qAtc "$1"; }
GBP_SNAPSHOT() { # id suffix, visible format; raw proof is read before a mutation
  local id="$1" fmt="$2" proof
  proof="$(q "select calendar_gbp_approval_snapshot(content_calendar.*) from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa$id'")"
  printf '{"caption":"caption %s","media_url":"https://cdn/%s.jpg","day_key":"2026-08-10","format":"%s","platform":"googlebusiness","gbp_proof":%s}' "$id" "$id" "$fmt" "$proof"
}

GYM_UUID="11111111-1111-1111-1111-111111111111"
OTHER_GYM="99999999-9999-9999-9999-999999999999"
q "insert into gyms values ('$GYM_UUID','swift-river-crossfit','Swift River')"
q "insert into gyms values ('$OTHER_GYM','other-gym','Other Gym')"
q "insert into echo_gym_settings values ('$GYM_UUID', false, 'test')"
q "insert into echo_gym_settings values ('$OTHER_GYM', false, 'test')"
SWIFT="swiftrivercrossfitd23567"  # normalised slug + fingerprint, like production
q "insert into echo_intake_tokens values ('$GYM_UUID','$SWIFT')"
q "insert into echo_intake_tokens values ('$OTHER_GYM','othergym999999')"

mkrow() { # id suffix status
  q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption)
     values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa$1','$SWIFT','$2','active','https://cdn/$1.jpg','instagram','feed','2026-08-10','caption $1')"
}
CLAIM() { q "select claim_calendar_publish_slot_owned('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa$1','$SWIFT','2026-08-10','America/New_York',2,true,true)"; }
DIGEST_OF() { q "select approval_digest from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa$1'"; }

# R1: Echo approve records status+digest but leaves provenance UNPROVED
mkrow 01 pending
check "echo approve updates row" "1" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01','$SWIFT')")"
check "echo approve leaves kind NULL (unproved)" "" "$(q "select coalesce(approval_kind,'') from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01'")"
check "echo approve leaves actor NULL" "" "$(q "select coalesce(approved_by,'') from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01'")"
check "echo approve leaves approved_at NULL" "" "$(q "select coalesce(approved_at::text,'') from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01'")"
check "echo approve stores digest" "t" "$(q "select approval_digest is not null from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01'")"
check "digest matches recomputation" "t" "$(q "select approval_digest = calendar_approval_digest(content_calendar.*) from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01'")"

# defect 1: Echo-token-only approval NEVER claims in Manual, digest or not
check "echo-only approval held at claim (Manual)" "" "$(CLAIM 01)"

# defect 1: the stamp RPC mints the human proof, atomically verified
D01="$(DIGEST_OF 01)"
# wrong gym -> zero rows, nothing stamped
check "stamp rejects wrong gym" "0" "$(q "select count(*) from calendar_stamp_verified_approval('$OTHER_GYM','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01','clerk-user-1','$D01')")"
check "wrong-gym attempt stamped nothing" "" "$(q "select coalesce(approval_kind,'') from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01'")"
# wrong digest -> zero rows
check "stamp rejects wrong echo digest" "0" "$(q "select count(*) from calendar_stamp_verified_approval('$GYM_UUID','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01','clerk-user-1','deadbeef')")"
# empty actor -> zero rows (no actor, no human proof)
check "stamp rejects empty actor" "0" "$(q "select count(*) from calendar_stamp_verified_approval('$GYM_UUID','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01','   ','$D01')")"
# spoof attempt: digest is right but actor is a body string -- the RPC cannot
# tell; the CONTRACT is that only the portal's authenticated Clerk session
# calls this. Verified here: gate requires nonempty actor (above) and the
# claim requires the stamp.
# happy path: exactly one row with the exact four fields
STAMP01="$(q "select gym_id::text||'|'||clerk_actor_id||'|'||approval_digest from calendar_stamp_verified_approval('$GYM_UUID','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01','clerk-user-1','$D01')")"
check "stamp returns exactly one row" "1" "$(test -n "$STAMP01" && echo 1 || echo 0)"
check "stamp returns exact fields" "$GYM_UUID|clerk-user-1|$D01" "$STAMP01"
check "stamp refuses to replace first human actor" "0" "$(q "select count(*) from calendar_stamp_verified_approval('$GYM_UUID','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01','clerk-user-2','$D01')")"
check "stamp sets human kind + actor" "human|clerk-user-1" "$(q "select approval_kind||'|'||approved_by from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01'")"
# publisher stamps scheduled_at AFTER approval; claim must still pass
q "update content_calendar set scheduled_at='2026-08-10 14:00:00+00' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01'"
C01="$(CLAIM 01)"
check "verified human proof claims in Manual" "1" "$([ -n "$C01" ] && echo 1 || echo 0)"

# defect 2: image mutation between Echo approve and the portal stamp -> the
# stamp's digest check fails closed (0 rows, no stamp)
mkrow 02 pending
q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02','$SWIFT')" >/dev/null
D02="$(DIGEST_OF 02)"
q "update content_calendar set image_url='https://cdn/02-reframed.jpg' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02'"
check "stamp rejects stale digest after image mutation" "0" "$(q "select count(*) from calendar_stamp_verified_approval('$GYM_UUID','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02','clerk-user-1','$D02')")"

# defect 2: image mutation AFTER the human stamp -> claim fails closed
mkrow 03 pending
q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa03','$SWIFT')" >/dev/null
D03="$(DIGEST_OF 03)"
q "select count(*) from calendar_stamp_verified_approval('$GYM_UUID','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa03','clerk-user-1','$D03')" >/dev/null
q "update content_calendar set image_url='https://cdn/03-reburned.jpg' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa03'"
check "post-stamp image mutation held at claim" "" "$(CLAIM 03)"

# defect 3: superseded variant can never be approved
q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption)
   values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa04','$SWIFT','pending','superseded','https://cdn/04.jpg','instagram','feed','2026-08-10','caption 04')"
check "approve refuses non-active variant" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa04','$SWIFT')")"

# defect 1: approved row with NO provenance at all fails closed in Manual
q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption)
   values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa05','$SWIFT','approved','active','https://cdn/05.jpg','instagram','feed','2026-08-10','caption 05')"
check "unproved approved row held (Manual)" "" "$(CLAIM 05)"

# defect 5: caption edit after the human stamp invalidates the proof
mkrow 06 pending
q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa06','$SWIFT')" >/dev/null
D06="$(DIGEST_OF 06)"
q "select count(*) from calendar_stamp_verified_approval('$GYM_UUID','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa06','clerk-user-1','$D06')" >/dev/null
q "update content_calendar set caption='edited after approval' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa06'"
check "post-approval caption edit held" "" "$(CLAIM 06)"

# stamp refuses a published row (terminal, no new stamp)
q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption,published_at)
   values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa07','$SWIFT','published','active','https://cdn/07.jpg','instagram','feed','2026-08-10','caption 07', now())"
check "stamp refuses published row" "0" "$(q "select count(*) from calendar_stamp_verified_approval('$GYM_UUID','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa07','clerk-user-1','whatever')")"

# defect 4: Auto -> Manual flip is read from the DB INSIDE the claim txn.
q "update echo_gym_settings set autonomous=true where gym_id='$GYM_UUID'"
mkrow 08 pending
C08="$(q "select claim_calendar_publish_slot_owned('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa08','$SWIFT','2026-08-10','America/New_York',2,false,true)")"
check "autonomous gym: pending row claims, gate armed" "1" "$([ -n "$C08" ] && echo 1 || echo 0)"
q "update echo_gym_settings set autonomous=false where gym_id='$GYM_UUID'"
mkrow 09 pending
check "Auto->Manual flip holds pending row at claim" "" "$(CLAIM 09)"

# The gated publisher receives the exact row that won the locked claim, plus
# the authoritative autonomy observed in that transaction. A prefetched
# caption/image must never be used as the outbound payload after this point.
q "update echo_gym_settings set autonomous=true where gym_id='$GYM_UUID'"
mkrow 12 pending
q "update content_calendar set caption='locked caption 12',image_url='https://cdn/locked-12.jpg' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa12'"
PROVEN12="$(q "select claim_calendar_publish_slot_proven_owned('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa12','$SWIFT','2026-08-11','America/New_York',2,false)")"
check "IG/FB proven claim returns locked caption" "locked caption 12" "$(printf '%s' "$PROVEN12" | python3 -c 'import json,sys; print(json.load(sys.stdin)["row"]["caption"])')"
check "IG/FB proven claim returns locked image" "https://cdn/locked-12.jpg" "$(printf '%s' "$PROVEN12" | python3 -c 'import json,sys; print(json.load(sys.stdin)["row"]["image_url"])')"
check "IG/FB proven claim returns claim-time Auto" "True" "$(printf '%s' "$PROVEN12" | python3 -c 'import json,sys; print(json.load(sys.stdin)["autonomous_at_claim"])')"
check "IG/FB proven repeat claim loses" "t" "$(q "select (claim_calendar_publish_slot_proven_owned('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa12','$SWIFT','2026-08-11','America/New_York',2,false)) is null")"
mkrow 13 pending
q "update content_calendar set caption='clean body [why] rationale' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa13'"
check "Auto cleanup uses exact caption CAS" "0" "$(q "select count(*) from calendar_patch_caption_autonomous_clean('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa13','$SWIFT','pending','stale caption','clean body')")"
check "Auto cleanup persists clean caption" "1" "$(q "select count(*) from calendar_patch_caption_autonomous_clean('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa13','$SWIFT','pending','clean body [why] rationale','clean body')")"
check "Auto cleanup clears proof" "t" "$(q "select approval_kind is null and approval_digest is null from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa13'")"
q "update echo_gym_settings set autonomous=false where gym_id='$GYM_UUID'"
mkrow 14 pending
check "Manual flip blocks autonomous cleanup" "0" "$(q "select count(*) from calendar_patch_caption_autonomous_clean('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa14','$SWIFT','pending','caption 14','clean body')")"

# A plausible six-hex suffix is not an account mapping.
q "update echo_gym_settings set autonomous=true where gym_id='$GYM_UUID'"
q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption)
   values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa11','swiftrivercrossfitabcdef','pending','active','https://cdn/11.jpg','instagram','feed','2026-08-10','caption 11')"
check "arbitrary suffix cannot inherit autonomy" "" "$(q "select claim_calendar_publish_slot_owned('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa11','swiftrivercrossfitabcdef','2026-08-10','America/New_York',2,false,true)")"
q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa11','swiftrivercrossfitabcdef')" >/dev/null
D11="$(DIGEST_OF 11)"
check "arbitrary suffix cannot receive human stamp" "0" "$(q "select count(*) from calendar_stamp_verified_approval('$GYM_UUID','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa11','clerk-user-1','$D11')")"
if q "insert into echo_intake_tokens values ('$OTHER_GYM','$SWIFT')" >/dev/null 2>&1; then
  check "duplicate account key rejected" "rejected" "inserted"
else
  check "duplicate account key rejected" "rejected" "rejected"
fi

# ---- visible-card snapshot compare (Echo half, 2026-10-05) -------------------
# p_expected rides the SAME atomic UPDATE as status+digest: a stale snapshot
# matches zero rows, flips nothing and stamps nothing (Echo 409s
# review_refresh_required). p_expected DEFAULT NULL is the legacy flag-OFF
# behavior (2-arg call still works and still approves).
SNAP() { echo "{\"caption\": \"caption $1\", \"media_url\": \"https://cdn/$1.jpg\", \"day_key\": \"2026-08-10\", \"format\": \"feed\", \"platform\": \"instagram\"}"; }

# matching snapshot approves and stamps digest, provenance still UNPROVED
mkrow 20 pending
check "matching snapshot approves" "1" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa20','$SWIFT','$(SNAP 20)'::jsonb)")"
check "snapshot approve leaves kind NULL" "" "$(q "select coalesce(approval_kind,'') from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa20'")"
check "snapshot approve stores digest" "t" "$(q "select approval_digest is not null from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa20'")"

# legacy 2-arg call (p_expected DEFAULT NULL) is byte-for-byte old behavior
mkrow 21 pending
check "flag-off 2-arg approve still works" "1" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa21','$SWIFT')")"

# stale caption -> zero rows, nothing stamped
mkrow 22 pending
q "update content_calendar set caption='edited AFTER the tap' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa22'"
check "stale caption refuses" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa22','$SWIFT','$(SNAP 22)'::jsonb)")"
check "stale caption flipped nothing" "pending" "$(q "select status from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa22'")"
check "stale caption stamped nothing" "t" "$(q "select approval_digest is null from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa22'")"

# stale final photo
mkrow 23 pending
q "update content_calendar set image_url='https://cdn/23-NEW.jpg' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa23'"
check "stale photo refuses" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa23','$SWIFT','$(SNAP 23)'::jsonb)")"

# stale visible date
mkrow 24 pending
q "update content_calendar set post_date='2026-08-11' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa24'"
check "stale date refuses" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa24','$SWIFT','$(SNAP 24)'::jsonb)")"

# stale platform (canonical account)
mkrow 25 pending
q "update content_calendar set account='facebook' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa25'"
check "stale platform refuses" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa25','$SWIFT','$(SNAP 25)'::jsonb)")"

# stale format (row really is a story now; snapshot says feed)
mkrow 26 pending
q "update content_calendar set format='story' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa26'"
check "stale format refuses" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa26','$SWIFT','$(SNAP 26)'::jsonb)")"

# NULL format on the row is the same effective 'feed' as the snapshot says
mkrow 27 pending
q "update content_calendar set format=null where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa27'"
check "null format matches effective feed" "1" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa27','$SWIFT','$(SNAP 27)'::jsonb)")"

# Google Business photo is a supported visible-card format. The same atomic
# snapshot compare still rejects a stale format or canonical platform.
q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption)
   values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa37','$SWIFT','pending','active','https://cdn/37.jpg','googlebusiness','photo','2026-08-10','caption 37')"
GBP_PHOTO_SNAPSHOT="$(GBP_SNAPSHOT 37 photo)"
check "matching GBP photo snapshot approves" "1" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa37','$SWIFT','$GBP_PHOTO_SNAPSHOT'::jsonb)")"
q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption)
   values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa38','$SWIFT','pending','active','https://cdn/38.jpg','googlebusiness','photo','2026-08-10','caption 38')"
q "update content_calendar set format='feed' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa38'"
GBP_STALE_FORMAT="$(GBP_SNAPSHOT 38 photo)"
check "stale GBP photo format refuses" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa38','$SWIFT','$GBP_STALE_FORMAT'::jsonb)")"
check "stale GBP photo format leaves pending" "pending" "$(q "select status from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa38'")"

# Every format actually emitted by gbp_planner must retain its own row identity
# through the locked approve and unproved-retry comparisons.
GBP_ID=42
for GBP_FORMAT in update event offer photo; do
  q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption)
     values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa$GBP_ID','$SWIFT','pending','active','https://cdn/$GBP_ID.jpg','googlebusiness','$GBP_FORMAT','2026-08-10','caption $GBP_ID')"
  GBP_EXPECTED="$(GBP_SNAPSHOT "$GBP_ID" "$GBP_FORMAT")"
  check "GBP $GBP_FORMAT exact snapshot approves" "1" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa$GBP_ID','$SWIFT','$GBP_EXPECTED'::jsonb)")"
  check "GBP $GBP_FORMAT exact retry compares" "1" "$(q "select count(*) from calendar_recover_unproved_approval('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa$GBP_ID','$SWIFT','$GBP_EXPECTED'::jsonb)")"
  check "GBP $GBP_FORMAT keeps digest" "t" "$(q "select approval_digest = calendar_approval_digest(content_calendar.*) from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa$GBP_ID'")"
  GBP_ID=$((GBP_ID+1))
done

q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption)
   values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa46','$SWIFT','pending','active','https://cdn/46.jpg','googlebusiness','event','2026-08-10','caption 46')"
GBP_STALE_EVENT="$(GBP_SNAPSHOT 46 offer)"
check "GBP event cannot approve as offer" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa46','$SWIFT','$GBP_STALE_EVENT'::jsonb)")"
check "GBP event/offer mismatch leaves pending" "pending" "$(q "select status from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa46'")"

# The structured card is a required part of GBP proof. Every digest-bound field
# is compared in the same UPDATE, including tenant identity, pillar, CTA,
# location and raw event/offer JSON.
q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption,gbp_topic_type,gbp_cta_type,gbp_cta_url,gbp_location_id,pillar)
   values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa47','$SWIFT','pending','active','https://cdn/47.jpg','googlebusiness','update','2026-08-10','caption 47','STANDARD','BOOK','https://book.old','place-1','education')"
GBP_CTA_BEFORE="$(GBP_SNAPSHOT 47 update)"
check "GBP missing structured proof refuses" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa47','$SWIFT','{\"caption\":\"caption 47\",\"media_url\":\"https://cdn/47.jpg\",\"day_key\":\"2026-08-10\",\"format\":\"update\",\"platform\":\"googlebusiness\"}'::jsonb)")"
check "GBP wrong gym identity refuses" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa47','$SWIFT',jsonb_set('$GBP_CTA_BEFORE'::jsonb,'{gbp_proof,gym_id}',to_jsonb('othergym'::text)))")"
q "update content_calendar set gbp_cta_url='https://book.new' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa47'"
check "GBP stale CTA refuses pending approve" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa47','$SWIFT','$GBP_CTA_BEFORE'::jsonb)")"
check "GBP stale CTA leaves pending and unproved" "t" "$(q "select status='pending' and approval_digest is null from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa47'")"

q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption,gbp_topic_type,gbp_offer)
   values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa48','$SWIFT','pending','active','https://cdn/48.jpg','googlebusiness','offer','2026-08-10','caption 48','OFFER','{\"couponCode\":\"SAVE10\",\"termsConditions\":\"Old terms\",\"redeemOnlineUrl\":\"https://offer.old\"}'::jsonb)"
GBP_OFFER_BEFORE="$(GBP_SNAPSHOT 48 offer)"
q "update content_calendar set gbp_offer=jsonb_set(gbp_offer,'{termsConditions}','\"New terms\"'::jsonb) where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa48'"
check "GBP stale offer refuses pending approve" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa48','$SWIFT','$GBP_OFFER_BEFORE'::jsonb)")"

q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption,gbp_topic_type,gbp_event)
   values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa49','$SWIFT','pending','active','https://cdn/49.jpg','googlebusiness','event','2026-08-10','caption 49','EVENT','{\"title\":\"Open house\",\"schedule\":{\"startDate\":\"2026-08-10\",\"endDate\":\"2026-08-11\"}}'::jsonb)"
GBP_EVENT_BEFORE="$(GBP_SNAPSHOT 49 event)"
q "update content_calendar set gbp_event=jsonb_set(gbp_event,'{schedule,startDate}','\"2026-08-12\"'::jsonb) where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa49'"
check "GBP stale event refuses pending approve" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa49','$SWIFT','$GBP_EVENT_BEFORE'::jsonb)")"

q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption,pillar,gbp_location_id)
   values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa50','$SWIFT','pending','active','https://cdn/50.jpg','googlebusiness','photo','2026-08-10','caption 50','photo','place-1')"
GBP_LOCATION_BEFORE="$(GBP_SNAPSHOT 50 photo)"
q "update content_calendar set gbp_location_id='place-2' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa50'"
check "GBP stale location refuses pending approve" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa50','$SWIFT','$GBP_LOCATION_BEFORE'::jsonb)")"
q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption,pillar)
   values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa51','$SWIFT','pending','active','https://cdn/51.jpg','googlebusiness','update','2026-08-10','caption 51','education')"
GBP_PILLAR_BEFORE="$(GBP_SNAPSHOT 51 update)"
q "update content_calendar set pillar='promotion' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa51'"
check "GBP stale pillar refuses pending approve" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa51','$SWIFT','$GBP_PILLAR_BEFORE'::jsonb)")"

# Approved but unproved legacy rows have no digest yet; these refusals prove
# recovery independently rechecks raw structured content, not just the digest.
for FIELD in cta offer event; do
  case "$FIELD" in cta) ID=52; FORMAT=update;; offer) ID=53; FORMAT=offer;; event) ID=54; FORMAT=event;; esac
  q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption,gbp_cta_url,gbp_offer,gbp_event)
     values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa$ID','$SWIFT','approved','active','https://cdn/$ID.jpg','googlebusiness','$FORMAT','2026-08-10','caption $ID','https://old','{\"termsConditions\":\"Old\"}'::jsonb,'{\"title\":\"Old\"}'::jsonb)"
  BEFORE="$(GBP_SNAPSHOT "$ID" "$FORMAT")"
  case "$FIELD" in
    cta) q "update content_calendar set gbp_cta_url='https://new' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa$ID'";;
    offer) q "update content_calendar set gbp_offer='{\"termsConditions\":\"New\"}'::jsonb where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa$ID'";;
    event) q "update content_calendar set gbp_event='{\"title\":\"New\"}'::jsonb where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa$ID'";;
  esac
  check "GBP stale $FIELD refuses unproved reaffirmation" "0" "$(q "select count(*) from calendar_recover_unproved_approval('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa$ID','$SWIFT','$BEFORE'::jsonb)")"
  check "GBP stale $FIELD leaves legacy digest null" "t" "$(q "select approval_digest is null from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa$ID'")"
done

# An actual null caption matches the null value in the visible-card snapshot.
mkrow 28 pending
q "update content_calendar set caption=null where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa28'"
NULL_CAPTION_SNAPSHOT='{"caption": null, "media_url": "https://cdn/28.jpg", "day_key": "2026-08-10", "format": "feed", "platform": "instagram"}'
check "null row caption matches null snapshot caption" "1" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa28','$SWIFT','$NULL_CAPTION_SNAPSHOT'::jsonb)")"

mkrow 29 pending
SPACED_MEDIA_SNAPSHOT='{"caption": "caption 29", "media_url": " https://cdn/29.jpg ", "day_key": "2026-08-10", "format": "feed", "platform": "instagram"}'
check "media URL is exact" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa29','$SWIFT','$SPACED_MEDIA_SNAPSHOT'::jsonb)")"

mkrow 30 pending
q "update content_calendar set caption=null where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa30'"
EMPTY_CAPTION_SNAPSHOT='{"caption": "", "media_url": "https://cdn/30.jpg", "day_key": "2026-08-10", "format": "feed", "platform": "instagram"}'
check "null and empty captions stay distinct" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa30','$SWIFT','$EMPTY_CAPTION_SNAPSHOT'::jsonb)")"

# wrong gym + valid snapshot still cannot approve (scope is orthogonal)
mkrow 33 pending
check "snapshot cannot cross gyms" "0" "$(q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa33','othergym999999','$(SNAP 33)'::jsonb)")"

# A failed portal stamp can be retried only after a fresh exact-card review.
mkrow 34 pending
q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa34','$SWIFT','$(SNAP 34)'::jsonb)" >/dev/null
check "unproved retry returns current row" "1" "$(q "select count(*) from calendar_recover_unproved_approval('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa34','$SWIFT','$(SNAP 34)'::jsonb)")"
MIXED_CASE_SNAPSHOT='{"caption": "caption 34", "media_url": "https://cdn/34.jpg", "day_key": "2026-08-10", "format": " FEED ", "platform": " INSTAGRAM "}'
check "unproved retry normalizes format and platform" "1" "$(q "select count(*) from calendar_recover_unproved_approval('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa34','$SWIFT','$MIXED_CASE_SNAPSHOT'::jsonb)")"
check "unproved retry rejects stale caption" "0" "$(q "select count(*) from calendar_recover_unproved_approval('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa34','$SWIFT','$(SNAP 35)'::jsonb)")"
q "update content_calendar set caption='changed' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa34'"
check "unproved retry rejects edited digest" "0" "$(q "select count(*) from calendar_recover_unproved_approval('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa34','$SWIFT','$(SNAP 34)'::jsonb)")"
mkrow 36 pending
q "select count(*) from approve_calendar_row_if_media_ready('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa36','$SWIFT','$(SNAP 36)'::jsonb)" >/dev/null
D36="$(DIGEST_OF 36)"
q "select count(*) from calendar_stamp_verified_approval('$GYM_UUID','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa36','clerk-user-1','$D36')" >/dev/null
check "proved approval cannot recover" "0" "$(q "select count(*) from calendar_recover_unproved_approval('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa36','$SWIFT','$(SNAP 36)'::jsonb)")"

# Existing approved Manual rows predate digest capture. A fresh exact-card
# tap fills only the digest; the authenticated portal stamps the actor later.
mkrow 39 approved
check "legacy approved requires exact card" "0" "$(q "select count(*) from calendar_recover_unproved_approval('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa39','$SWIFT','$(SNAP 35)'::jsonb)")"
check "stale legacy tap leaves digest null" "t" "$(q "select approval_digest is null from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa39'")"
check "legacy approved recovers digest" "1" "$(q "select count(*) from calendar_recover_unproved_approval('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa39','$SWIFT','$(SNAP 39)'::jsonb)")"
check "legacy digest matches locked row" "t" "$(q "select approval_digest = calendar_approval_digest(content_calendar.*) from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa39'")"
check "legacy recovery did not mint actor" "t" "$(q "select approval_kind is null and approved_by is null and approved_at is null from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa39'")"
check "recovered legacy row still held at Manual claim" "" "$(CLAIM 39)"
D39="$(DIGEST_OF 39)"
check "portal can stamp recovered legacy row" "1" "$(q "select count(*) from calendar_stamp_verified_approval('$GYM_UUID','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa39','clerk-user-39','$D39')")"
check "proved legacy row cannot recover again" "0" "$(q "select count(*) from calendar_recover_unproved_approval('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa39','$SWIFT','$(SNAP 39)'::jsonb)")"

mkrow 40 approved
check "wrong gym cannot capture legacy digest" "0" "$(q "select count(*) from calendar_recover_unproved_approval('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa40','othergym999999','$(SNAP 40)'::jsonb)")"
check "wrong gym left legacy digest null" "t" "$(q "select approval_digest is null from content_calendar where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa40'")"
q "update content_calendar set media_not_ready_reason='hold' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa40'"
check "media hold cannot capture legacy digest" "0" "$(q "select count(*) from calendar_recover_unproved_approval('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa40','$SWIFT','$(SNAP 40)'::jsonb)")"

mkrow 41 approved
q "update content_calendar set approval_digest='old-digest' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa41'"
check "mismatched stored digest cannot recover" "0" "$(q "select count(*) from calendar_recover_unproved_approval('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa41','$SWIFT','$(SNAP 41)'::jsonb)")"
check "mismatched stored digest was preserved" "old-digest" "$(DIGEST_OF 41)"

# Keep the resolver's settings-row lock open across a transaction. A mode
# update must wait for that claim-side transaction, never pass it mid-claim.
cat >"$WORK/lock-holder.sql" <<SQL
begin;
select calendar_gym_is_autonomous('$SWIFT');
\! touch "$WORK/lock-acquired"
select pg_sleep(2);
commit;
SQL
psql -h "$SOCK" -p 55444 -U postgres -d postgres -v ON_ERROR_STOP=1 -qAt \
  -f "$WORK/lock-holder.sql" >"$WORK/lock-holder.out" 2>"$WORK/lock-holder.err" &
HOLDER_PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -e "$WORK/lock-acquired" ] && break
  sleep 0.1
done
check "claim-side resolver acquired settings lock" "yes" "$([ -e "$WORK/lock-acquired" ] && echo yes || echo no)"
if q "set lock_timeout='200ms'; update echo_gym_settings set autonomous=false where gym_id='$GYM_UUID'" >/dev/null 2>&1; then
  check "mode update waits for claim transaction" "blocked" "updated"
else
  check "mode update waits for claim transaction" "blocked" "blocked"
fi
if q "set lock_timeout='200ms'; update echo_intake_tokens set echo_account_key='movedkey' where gym_id='$GYM_UUID'" >/dev/null 2>&1; then
  check "token reassignment waits for claim transaction" "blocked" "updated"
else
  check "token reassignment waits for claim transaction" "blocked" "blocked"
fi
wait "$HOLDER_PID"
q "update echo_gym_settings set autonomous=false where gym_id='$GYM_UUID'"

# defect 4: unresolved gym (no gyms row) fails closed even on the Auto lane
q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption)
   values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa10','ghostgym','pending','active','https://cdn/10.jpg','instagram','feed','2026-08-10','caption 10')"
check "unresolved gym fails closed" "" "$(q "select claim_calendar_publish_slot_owned('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa10','ghostgym','2026-08-10','America/New_York',2,false,true)")"

# flag OFF contract: gate param FALSE keeps legacy behavior for a Manual gym
q "update content_calendar set account='facebook' where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa05'"
check "flag-off claim ignores provenance entirely" "1" "$(q "select case when claim_calendar_publish_slot_owned('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa05','$SWIFT','2026-08-10','America/New_York',2,true,false) is not null then '1' else '' end")"

# LASSO's already-live capacity-three exception includes Stories. The proof
# flag is off here, so capacity/format semantics remain independent of proof.
q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption)
   values ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01','lasso','approved','active','https://cdn/story.jpg','instagram','story','2026-08-10','story')"
check "capacity-three LASSO Story claim succeeds with proof flag off" "1" "$(q "select case when claim_calendar_publish_slot_owned('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01','lasso','2026-08-10','America/New_York',3,true,false) is not null then '1' else '' end")"
q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption)
   values ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb02','lasso','approved','active','https://cdn/reel.jpg','instagram','reel','2026-08-10','unsupported')"
check "capacity-three LASSO unsupported format is rejected" "" "$(q "select claim_calendar_publish_slot_owned('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb02','lasso','2026-08-10','America/New_York',3,true,false)")"


# GBP uses a guarded claim with a returned creative, independent of IG/FB capacity.
GBP_ID="cccccccc-cccc-cccc-cccc-cccccccccc01"
q "insert into content_calendar (id,gym_id,status,variant_status,image_url,account,format,post_date,caption,gbp_topic_type,gbp_location_id) values ('$GBP_ID','$SWIFT','approved','active','https://cdn/gbp.jpg','googlebusiness','photo','2026-08-10','GBP caption','STANDARD','locations/1')"
GBP_CLAIM() { q "select count(*) from claim_calendar_gbp_publish_owned('$GBP_ID','$SWIFT')"; }
check "GBP Manual bare approved held" "0" "$(GBP_CLAIM)"
q "update content_calendar set status='pending' where id='$GBP_ID'"
check "GBP pending held" "0" "$(GBP_CLAIM)"
q "update content_calendar set status='approved',approval_kind='human',approved_by='user_coach',approved_at=now(),approval_digest=calendar_approval_digest(content_calendar.*) where id='$GBP_ID'"
for FIELD in caption image_url gbp_topic_type gbp_cta_type gbp_cta_url gbp_location_id pillar; do
  q "update content_calendar set $FIELD='changed' where id='$GBP_ID'"
  check "GBP stale $FIELD digest held" "0" "$(GBP_CLAIM)"
  q "update content_calendar set approval_digest=calendar_approval_digest(content_calendar.*) where id='$GBP_ID'"
done
for FIELD in gbp_event gbp_offer; do
  q "update content_calendar set $FIELD='{\"title\":\"changed\"}'::jsonb where id='$GBP_ID'"
  check "GBP stale $FIELD digest held" "0" "$(GBP_CLAIM)"
  q "update content_calendar set approval_digest=calendar_approval_digest(content_calendar.*) where id='$GBP_ID'"
done
check "GBP exact human proof photo claims" "1" "$(GBP_CLAIM)"
check "GBP repeated claim loses" "0" "$(GBP_CLAIM)"
check "GBP returned/current creative retained" "changed" "$(q "select caption from content_calendar where id='$GBP_ID'")"
q "update content_calendar set status='approved',publish_claim_token=null,approval_kind=null,approved_by=null,approved_at=null,approval_digest=null where id='$GBP_ID'"
q "update echo_gym_settings set autonomous=true where gym_id='$GYM_UUID'"
check "GBP autonomous bare approved claims" "1" "$(GBP_CLAIM)"
q "update content_calendar set status='approved',publish_claim_token=null,caption='locked GBP caption',image_url='https://cdn/locked-gbp.jpg' where id='$GBP_ID'"
GBP_MODE="$(q "select claim_calendar_gbp_publish_with_mode_owned('$GBP_ID','$SWIFT')")"
check "GBP mode wrapper returns locked caption" "locked GBP caption" "$(printf '%s' "$GBP_MODE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["row"]["caption"])')"
check "GBP mode wrapper returns claim-time Auto" "True" "$(printf '%s' "$GBP_MODE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["autonomous_at_claim"])')"
q "update content_calendar set status='approved',publish_claim_token=null where id='$GBP_ID'"
q "update echo_gym_settings set autonomous=false where gym_id='$GYM_UUID'"
check "GBP Auto to Manual holds old approved" "0" "$(GBP_CLAIM)"
q "update content_calendar set gym_id='ghostgym' where id='$GBP_ID'"
check "GBP unresolved gym holds" "0" "$(q "select count(*) from claim_calendar_gbp_publish_owned('$GBP_ID','ghostgym')")"
q "update content_calendar set gym_id='$SWIFT',approval_kind='human',approved_by='user_coach',approved_at=now() where id='$GBP_ID'"
q "update content_calendar set approval_digest=calendar_approval_digest(content_calendar.*) where id='$GBP_ID'"
# Two actual database sessions compete; only the first may claim and retain ownership.
q "begin; select count(*) from claim_calendar_gbp_publish_owned('$GBP_ID','$SWIFT'); select pg_sleep(1); commit" > "$WORK/gbp-first" &
GBP_FIRST_PID=$!
q "select count(*) from claim_calendar_gbp_publish_owned('$GBP_ID','$SWIFT')" > "$WORK/gbp-second" &
GBP_SECOND_PID=$!
wait "$GBP_FIRST_PID" "$GBP_SECOND_PID"
check "GBP concurrent claims have one winner" "1" "$(awk '/^[01]$/ {s+=$1} END {print s}' "$WORK/gbp-first" "$WORK/gbp-second")"

echo "---"
echo "pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
