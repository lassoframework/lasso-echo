-- Additive owned-only style hold/replacement. No existing functions, claim
-- logic, Story whitelist, protected caption/book, approvals or grants change.
-- CREATE FUNCTION (not OR REPLACE) refuses overwriting an installed version.
begin;
create function public.lasso_visual_snapshot_20261009(p jsonb)
returns jsonb language plpgsql immutable set search_path=public as $$
declare k text; result jsonb:=p;
begin
 if jsonb_typeof(p) is distinct from 'object' then return null; end if;
 foreach k in array array['created_at','published_at','scheduled_at','approved_at'] loop
  if not p ? k then return null; end if;
  if p->k='null'::jsonb then continue; end if;
  if jsonb_typeof(p->k) is distinct from 'string' or p->>k !~
    '^[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}(:?[0-9]{2})?)$'
  then return null; end if;
  result:=jsonb_set(result,array[k],to_jsonb(extract(epoch from (p->>k)::timestamptz)));
 end loop;
 return result;
exception when invalid_datetime_format or datetime_field_overflow or invalid_time_zone_displacement_value then return null;
end; $$;

create function public.hold_lasso_style_feed_20261009(
 p_row_id uuid,p_expected jsonb,p_first date,p_last date)
returns jsonb language plpgsql security definer set search_path=public as $$
declare f public.content_calendar%rowtype; expected jsonb;
 today date:=(now() at time zone 'America/New_York')::date;
begin
 expected:=public.lasso_visual_snapshot_20261009(p_expected);
 if p_row_id is null or expected is null or p_first is null or p_last is null
    or p_first>p_last or p_first<today-37 or p_last>today+1 then
  return jsonb_build_object('result','conflict','reason','invalid_scope'); end if;
 perform pg_advisory_xact_lock(hashtextextended('lasso',0));
 if public.calendar_gym_is_autonomous('lasso') is not true then
  return jsonb_build_object('result','conflict','reason','autonomy_not_confirmed'); end if;
 perform pg_advisory_xact_lock(hashtextextended('lasso|'||coalesce(p_expected->>'account','')||'|'||coalesce(p_expected->>'post_date',''),0));
 select * into f from public.content_calendar where id=p_row_id for update;
 if not found or public.lasso_visual_snapshot_20261009(to_jsonb(f)) is distinct from expected then
  return jsonb_build_object('result','conflict','reason','source_changed'); end if;
 if f.gym_id is distinct from 'lasso' or f.account not in ('instagram','facebook') or f.account is null
    or f.format is distinct from 'feed' or f.status is distinct from 'pending'
    or f.variant_status is distinct from 'active' or f.slot_index is null or f.slot_index not in (0,1,2)
    or f.post_date is null or f.post_date not between p_first and p_last
    or f.published_at is not null or f.late_post_id is not null
    or f.publish_claim_token is not null or f.publish_reservation_day is not null
    or f.approval_kind is not null or f.approved_by is not null or f.approved_at is not null or f.approval_digest is not null
    or f.media_not_ready_reason is not null then
  return jsonb_build_object('result','conflict','reason','not_unheld_owned_pending'); end if;
 update public.content_calendar set media_not_ready_reason='lasso_visual_style_review_required'
  where id=f.id returning * into f;
 return jsonb_build_object('result','held','id',f.id,'row',to_jsonb(f));
end; $$;

create function public.replace_lasso_style_feed_media_20261009(
 p_row_id uuid,p_expected jsonb,p_new_url text,p_policy text,p_brain jsonb,p_sha text,p_review_id text)
returns jsonb language plpgsql security definer set search_path=public as $$
declare f public.content_calendar%rowtype; expected jsonb; v_tenant text;
begin
 expected:=public.lasso_visual_snapshot_20261009(p_expected);
 if p_row_id is null or expected is null or coalesce(p_new_url,'') !~ '^https://'
    or coalesce(p_sha,'') !~ '^[0-9a-f]{64}$' or nullif(btrim(p_review_id),'') is null
    or p_policy is distinct from 'lasso-astra-2026-10-09-grounded-editorial-v2'
    or jsonb_typeof(p_brain) is distinct from 'object'
    or coalesce(p_brain->>'brand_voice/lasso_visual_standard.md','') !~ '^[0-9a-f]{64}$' then
  return jsonb_build_object('result','conflict','reason','invalid_current_review'); end if;
 perform pg_advisory_xact_lock(hashtextextended('lasso',0));
 if public.calendar_gym_is_autonomous('lasso') is not true then
  return jsonb_build_object('result','conflict','reason','autonomy_not_confirmed'); end if;
 perform pg_advisory_xact_lock(hashtextextended('lasso|'||coalesce(p_expected->>'account','')||'|'||coalesce(p_expected->>'post_date',''),0));
 select * into f from public.content_calendar where id=p_row_id for update;
 if not found or public.lasso_visual_snapshot_20261009(to_jsonb(f)) is distinct from expected then
  return jsonb_build_object('result','conflict','reason','source_changed'); end if;
 if f.gym_id is distinct from 'lasso' or f.account is null or f.account not in ('instagram','facebook')
    or f.format is distinct from 'feed' or f.status is distinct from 'pending' or f.variant_status is distinct from 'active'
    or f.slot_index is null or f.slot_index not in (0,1,2)
    or f.post_date is null or f.post_date not between (now() at time zone 'America/New_York')::date-37 and (now() at time zone 'America/New_York')::date+1
    or f.published_at is not null or f.late_post_id is not null or f.publish_claim_token is not null or f.publish_reservation_day is not null
    or f.approval_kind is not null or f.approved_by is not null or f.approved_at is not null or f.approval_digest is not null
    or f.media_not_ready_reason is null or f.media_not_ready_reason not in
      ('lasso_visual_style_review_required','caption_changed_needs_new_visual','cross_date_media_repeat_needs_new_visual')
    or (f.media_not_ready_reason='caption_changed_needs_new_visual' and p_new_url=f.image_url) then
  return jsonb_build_object('result','conflict','reason','not_owned_held_pending'); end if;
 v_tenant:=case when f.account='instagram' then 'lasso_ig' else 'lasso_fb' end;
 if not exists (select 1 from public.echo_infographic_artifacts a where a.tenant=v_tenant and a.image_url=p_new_url
    and a.image_sha256=p_sha and a.evidence->>'image_sha256'=p_sha and a.evidence->>'grade_status'='PASS'
    and a.evidence->>'review_response_id'=p_review_id and a.evidence->>'policy_version'=p_policy
    and a.evidence->'brain_snapshot'=p_brain and a.evidence->'style_conformant'='true'::jsonb
    and a.evidence->'style_violations'='[]'::jsonb
    and coalesce(a.evidence->>'aspect','') in ('','4:5','1:1','1.91:1')
    and (not a.evidence ? 'verified_dimensions' or a.evidence->'verified_dimensions'='null'::jsonb
      or case when jsonb_typeof(a.evidence->'verified_dimensions')='object'
        and jsonb_typeof(a.evidence->'verified_dimensions'->'width')='number'
        and jsonb_typeof(a.evidence->'verified_dimensions'->'height')='number'
        and a.evidence->'verified_dimensions'->>'width' ~ '^[1-9][0-9]*$'
        and a.evidence->'verified_dimensions'->>'height' ~ '^[1-9][0-9]*$'
      then (a.evidence->'verified_dimensions'->>'width')::numeric /
           (a.evidence->'verified_dimensions'->>'height')::numeric between 0.8 and 1.91
        and a.evidence->'verified_dimensions'->>'image_sha256'=p_sha else false end)
    and a.evidence->>'visual_standard_version'='lasso-grounded-editorial-2026-10-09-v1'
    and a.source_identity=jsonb_build_object('source_id','content_calendar:'||f.id::text||':caption',
      'source_hash',encode(sha256(convert_to(f.caption,'UTF8')),'hex'))) then
  return jsonb_build_object('result','conflict','reason','current_review_missing'); end if;
 update public.content_calendar set image_url=p_new_url,source_media_url=p_new_url,source_media_asset_id=null,
   thumbnail_url=null,media_not_ready_reason=null where id=f.id returning * into f;
 return jsonb_build_object('result','replaced','id',f.id,'row',to_jsonb(f));
end; $$;
revoke all on function public.lasso_visual_snapshot_20261009(jsonb) from public,anon,authenticated,service_role;
revoke all on function public.hold_lasso_style_feed_20261009(uuid,jsonb,date,date) from public,anon,authenticated;
revoke all on function public.replace_lasso_style_feed_media_20261009(uuid,jsonb,text,text,jsonb,text,text) from public,anon,authenticated;
grant execute on function public.hold_lasso_style_feed_20261009(uuid,jsonb,date,date) to service_role;
grant execute on function public.replace_lasso_style_feed_media_20261009(uuid,jsonb,text,text,jsonb,text,text) to service_role;
commit;
