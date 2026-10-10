-- Read-only verification after disposable installation.
select version();
select attname from pg_attribute where attrelid='public.content_calendar'::regclass
 and attname in ('creative_origin','generated_artifact_version_id','generated_artifact_sha256') and not attisdropped;
select tgname from pg_trigger where tgrelid='public.content_calendar'::regclass and tgname='zzz_calendar_generated_approval_guard';
select has_table_privilege('service_role','public.calendar_generated_artifact_versions','insert') as producer_cannot_issue_receipt,
 has_table_privilege('service_role','public.calendar_generated_artifact_versions','select') as trusted_read_available;
select proname,oid,proacl from pg_proc where proname in ('calendar_approval_digest','approve_calendar_row_if_media_ready','calendar_recover_unproved_approval','calendar_stamp_verified_approval');
