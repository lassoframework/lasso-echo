-- DRAFT / UNAPPLIED / DEFAULT OFF. Requires the forward-media claim draft and
-- the existing GBP calendar/connection columns. No credentials or gates enabled.
-- One final linearization point for row/creative/media/token/destination. No
-- network/provider I/O occurs under these locks. This does not prevent an ADMIN
-- revoking a lease after COMMIT; a durable provider-attempt fence is separate.
begin;

do $$ declare required text; begin
 foreach required in array array['id','gym_id','account','format','post_date','caption','image_url',
  'source_media_url','thumbnail_url','visual_group_key','source_media_asset_id','render_manifest_digest',
  'gbp_topic_type','gbp_cta_type','gbp_cta_url','gbp_event','gbp_offer','gbp_location_id','pillar'] loop
  if not exists(select 1 from information_schema.columns where table_schema='public'
    and table_name='content_calendar' and column_name=required) then
   raise exception 'exact GBP creative column missing: %',required; end if;
 end loop;
 -- The existing tenant/location key must exclude concurrent duplicate inserts;
 -- do not silently weaken exact destination identity on a different schema.
 if not exists(select 1 from pg_index i where i.indrelid='public.gym_gbp_connections'::regclass
   and i.indisunique and i.indisvalid and i.indnkeyatts=2 and i.indpred is null
   and (select array_agg(a.attname::text order by a.attname)
        from unnest(i.indkey::smallint[]) with ordinality k(attnum,n)
        join pg_attribute a on a.attrelid=i.indrelid and a.attnum=k.attnum
        where k.n<=i.indnkeyatts)=array['gbp_location_id','portal_gym_key']) then
  raise exception 'unique same gym GBP location key required'; end if;
end; $$;

create function public.fixer_authorize_gbp_forward_send_20261008(
 p_calendar_row_id uuid,p_claim_token uuid,p_expected_creative jsonb,
 p_native_account_id text,p_expected_location_id text,p_send_kind text)
returns boolean language plpgsql security definer
set search_path=pg_catalog,public as $$
declare r public.content_calendar%rowtype; c public.gym_gbp_connections%rowtype;
 expected jsonb; revision text; evidence uuid; matches integer;
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'forward media authority requires read committed isolation' using errcode='25000'; end if;
 if p_calendar_row_id is null or p_claim_token is null
  or jsonb_typeof(p_expected_creative) is distinct from 'object'
  or octet_length(p_expected_creative::text)>65536
  or nullif(btrim(p_native_account_id),'') is null
  or nullif(btrim(p_expected_location_id),'') is null
  or p_send_kind is null or p_send_kind not in ('post','gallery') then
  raise exception 'exact GBP send binding required' using errcode='23514'; end if;
 -- Global graph before calendar, then destination, matching existing claim
 -- order. GBP connection writers touch only that connection table.
 perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
 select * into r from public.content_calendar where id=p_calendar_row_id for update;
 if not found or r.account is distinct from 'googlebusiness'
  or r.publish_claim_token is distinct from p_claim_token or r.status is distinct from 'publishing'
  or r.published_at is not null or r.late_post_id is not null
  or r.gbp_location_id is distinct from p_expected_location_id
  or (p_send_kind='gallery') is distinct from (lower(coalesce(r.format,''))='photo') then
  raise exception 'current owned GBP row required' using errcode='23514'; end if;
 -- Attestation revision covers media identity, not all caption/GBP fields.
 -- Require the complete outgoing input snapshot as well, including explicit
 -- nulls. This JSON is compared to the locked row; it grants no authority.
 select jsonb_object_agg(key,value) into expected from jsonb_each(to_jsonb(r))
  where key=any(array['id','gym_id','account','format','post_date','caption','image_url',
   'source_media_url','thumbnail_url','visual_group_key','source_media_asset_id',
   'render_manifest_digest','gbp_topic_type','gbp_cta_type','gbp_cta_url','gbp_event',
   'gbp_offer','gbp_location_id','pillar']);
 if p_expected_creative is distinct from expected then
  raise exception 'full outgoing GBP creative changed' using errcode='23514'; end if;
 select count(*) into matches from public.gym_gbp_connections
  where portal_gym_key=r.gym_id and gbp_location_id=r.gbp_location_id;
 if matches<>1 then
  raise exception 'unique current same gym GBP destination required' using errcode='23514'; end if;
 select * into c from public.gym_gbp_connections
  where portal_gym_key=r.gym_id and gbp_location_id=r.gbp_location_id for share;
 if not found or c.status is distinct from 'connected'
  or c.zernio_account_id is distinct from p_native_account_id then
  raise exception 'current same gym GBP destination changed' using errcode='23514'; end if;
 revision:=public.fixer_forward_media_attestation_request_20261006(r.id)->>'revision';
 select evidence_id into evidence from public.fixer_forward_media_lineage_20261006
  where calendar_row_id=r.id and row_revision=revision order by verified_at desc limit 1;
 if evidence is null then
  raise exception 'current trusted GBP media evidence required' using errcode='23514'; end if;
 -- Existing gate, provenance, history, occupancy and same-token receipt rules
 -- remain the sole media authority, inside this same locked transaction.
 if public.fixer_claim_forward_media_20261006(r.id,p_claim_token,evidence,revision) is distinct from true then
  raise exception 'atomic forward media authority refused publication' using errcode='23514'; end if;
 return true;
end; $$;
revoke all on function public.fixer_authorize_gbp_forward_send_20261008(uuid,uuid,jsonb,text,text,text)
 from public,anon,authenticated,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006;
grant execute on function public.fixer_authorize_gbp_forward_send_20261008(uuid,uuid,jsonb,text,text,text)
 to service_role;
commit;
