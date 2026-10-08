-- DRAFT ONLY. No production application or release is authorized by this file.
-- One attended Swift orphan counter reset, never a generic bypass/admission RPC.
-- Source schema: media_source_media_asset_20260827.sql, review migrations,
-- portal_action_receipt_draft_20261004.sql and the frozen live portal RPC export
-- tests/fixtures/forward_lock_entry/portal-function-definitions-20261008.sql.
-- Read-only catalog check 2026-10-08, project ooqcvmcjspeltuuhcvlh: all six
-- ordinary tables owned postgres; all below identity column types matched.
-- Columns calendar=42, asset=36, source=17, receipt=18, swap=12, review=10.
-- service_role BYPASSRLS, not superuser/createrole/replication/owner member.
-- Existing triggers: calendar logical-post guard and two LASSO Story guards;
-- review append-only; receipt immutable/touch. No aa_swift name conflicts.
-- Fresh live DDL/role ownership must still be read back before any actual apply.
-- No TTL or fleet switch. Installation begins ACTIVE and unbound. Release is
-- attended, exact-operation/receipt-digest bound after independent readback.
-- Separate owner stops/fencing and evidence are still required for SQLite,
-- external provider queues, byte/history clearance and prospective admission.
-- Database owners/superusers remain privileged DDL principals: never give the
-- service/operator role table ownership, superuser, replication or owner membership.
BEGIN;
LOCK TABLE public.media_source, public.media_asset, public.content_calendar,
  public.portal_action_receipt, public.portal_swap_guard,
  public.media_asset_review_event IN ACCESS EXCLUSIVE MODE;

-- Refuse missing or incompatible identity columns rather than silently omit a
-- trigger. These are the exact relation/column/type names used below. Full-row
-- before/after JSON preserves all additional deployed columns automatically.
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM (VALUES
    ('media_source','id','text'), ('media_source','gym_id','text'),
    ('media_source','active','boolean'), ('media_asset','id','text'),
    ('media_asset','source_id','text'), ('media_asset','gym_id','text'),
    ('media_asset','used_count','integer'), ('media_asset','last_used_at','timestamp with time zone'),
    ('media_asset','content_hash','text'), ('media_asset','drive_modified','timestamp with time zone'),
    ('media_asset','review_status','text'), ('media_asset','eligible','boolean'),
    ('media_asset','excluded_by_coach','boolean'),
    ('content_calendar','id','uuid'), ('content_calendar','gym_id','text'),
    ('content_calendar','source_media_asset_id','text'),
    ('portal_action_receipt','id','bigint'), ('portal_action_receipt','gym_id','text'),
    ('portal_action_receipt','row_id','uuid'), ('portal_action_receipt','selected_asset','jsonb'),
    ('portal_action_receipt','status','text'),
    ('portal_swap_guard','gym_id','uuid'), ('portal_swap_guard','account_key','text'),
    ('portal_swap_guard','post_id','uuid'), ('portal_swap_guard','status','text'),
    ('media_asset_review_event','gym_id','text'), ('media_asset_review_event','asset_id','text')
  ) v(tbl,col,typ)
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_attribute a
      WHERE a.attrelid = to_regclass('public.' || r.tbl) AND a.attname = r.col
        AND NOT a.attisdropped AND format_type(a.atttypid,a.atttypmod) = r.typ) THEN
      RAISE EXCEPTION 'Swift fence schema mismatch: %.% expected %', r.tbl,r.col,r.typ;
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role'
    AND (rolsuper OR rolcreaterole OR rolreplication)) OR EXISTS (
      SELECT 1 FROM pg_class c WHERE c.oid IN (
        'public.media_source'::regclass,'public.media_asset'::regclass,
        'public.content_calendar'::regclass,'public.portal_action_receipt'::regclass,
        'public.portal_swap_guard'::regclass,'public.media_asset_review_event'::regclass)
        AND pg_has_role('service_role',c.relowner,'MEMBER')) THEN
    RAISE EXCEPTION 'Swift fence service_role owns or can administer a guarded relation';
  END IF;
END $$;

CREATE SCHEMA swift_maintenance_20261008;
REVOKE ALL ON SCHEMA swift_maintenance_20261008 FROM PUBLIC, anon, authenticated, service_role;
CREATE TABLE swift_maintenance_20261008.fence (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  active boolean NOT NULL DEFAULT true,
  operation_id uuid UNIQUE,
  operator_login name,
  candidate_manifest_sha256 text CHECK (candidate_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  bound_at timestamptz,
  release_receipt_sha256 text,
  released_at timestamptz,
  CHECK ((operation_id IS NULL AND operator_login IS NULL AND candidate_manifest_sha256 IS NULL AND bound_at IS NULL)
    OR (operation_id IS NOT NULL AND operator_login IS NOT NULL AND candidate_manifest_sha256 IS NOT NULL AND bound_at IS NOT NULL)),
  CHECK ((active AND release_receipt_sha256 IS NULL AND released_at IS NULL)
    OR (NOT active AND operation_id IS NOT NULL AND released_at IS NOT NULL
      AND release_receipt_sha256 ~ '^[0-9a-f]{64}$'))
);
INSERT INTO swift_maintenance_20261008.fence(singleton) VALUES (true);
CREATE TABLE swift_maintenance_20261008.permit (
  operation_id uuid PRIMARY KEY, backend_pid integer NOT NULL, transaction_id bigint NOT NULL,
  operator_login name NOT NULL, asset_id text NOT NULL,
  before_row jsonb NOT NULL, after_row jsonb NOT NULL,
  consumed boolean NOT NULL DEFAULT false
);
CREATE TABLE swift_maintenance_20261008.receipt (
  operation_id uuid PRIMARY KEY, operator_login name NOT NULL,
  candidate_manifest_sha256 text NOT NULL, manifest jsonb NOT NULL,
  before_row jsonb NOT NULL, after_row jsonb NOT NULL,
  transaction_id bigint NOT NULL, committed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
REVOKE ALL ON ALL TABLES IN SCHEMA swift_maintenance_20261008 FROM PUBLIC, anon, authenticated, service_role;

CREATE FUNCTION swift_maintenance_20261008.is_swift(p_key text) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
  SELECT coalesce(p_key IN ('swiftrivercrossfite5c9db','swiftrivercrossfitd23567',
    'e5c9db81-110d-4308-9bb7-3ad3bf563a0b'),false)
$$;

-- Follow the source relationship even when a preexisting denormalized tenant
-- key is wrong. A source linked to a Swift-keyed asset also belongs inside the
-- fence; the component cannot escape by borrowing the corrupt row's other key.
CREATE FUNCTION swift_maintenance_20261008.source_is_swift(p_id text)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS $$
  SELECT EXISTS (SELECT 1 FROM public.media_source s WHERE s.id=p_id AND
    (swift_maintenance_20261008.is_swift(s.gym_id) OR EXISTS (
      SELECT 1 FROM public.media_asset a WHERE a.source_id=s.id
        AND swift_maintenance_20261008.is_swift(a.gym_id))))
$$;
CREATE FUNCTION swift_maintenance_20261008.asset_is_swift(p_id text)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS $$
  SELECT EXISTS (SELECT 1 FROM public.media_asset a WHERE a.id=p_id AND
    (swift_maintenance_20261008.is_swift(a.gym_id)
      OR swift_maintenance_20261008.source_is_swift(a.source_id)))
$$;

CREATE FUNCTION swift_maintenance_20261008.row_is_swift(p_table text,p_row jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
  IF p_row IS NULL THEN RETURN false; END IF;
  IF swift_maintenance_20261008.is_swift(p_row->>'gym_id')
     OR swift_maintenance_20261008.is_swift(p_row->>'account_key') THEN RETURN true; END IF;
  IF p_table = 'media_source' AND swift_maintenance_20261008.source_is_swift(p_row->>'id') THEN RETURN true; END IF;
  IF p_table = 'media_asset' AND swift_maintenance_20261008.source_is_swift(p_row->>'source_id') THEN RETURN true; END IF;
  IF p_table IN ('content_calendar','media_asset_review_event','portal_action_receipt')
    AND swift_maintenance_20261008.asset_is_swift(CASE p_table
      WHEN 'content_calendar' THEN p_row->>'source_media_asset_id'
      WHEN 'media_asset_review_event' THEN p_row->>'asset_id'
      ELSE p_row->'selected_asset'->>'asset_id' END) THEN RETURN true; END IF;
  IF p_table IN ('portal_action_receipt','portal_swap_guard') AND EXISTS (
    SELECT 1 FROM public.content_calendar c WHERE c.id::text = CASE p_table
      WHEN 'portal_swap_guard' THEN p_row->>'post_id' ELSE p_row->>'row_id' END
      AND swift_maintenance_20261008.row_is_swift('content_calendar',to_jsonb(c))) THEN RETURN true; END IF;
  RETURN false;
END $$;

CREATE FUNCTION swift_maintenance_20261008.guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE b jsonb; a jsonb; f swift_maintenance_20261008.fence%ROWTYPE;
BEGIN
  IF TG_OP <> 'INSERT' THEN b := to_jsonb(OLD); END IF;
  IF TG_OP <> 'DELETE' THEN a := to_jsonb(NEW); END IF;
  IF NOT (swift_maintenance_20261008.row_is_swift(TG_TABLE_NAME,b)
    OR swift_maintenance_20261008.row_is_swift(TG_TABLE_NAME,a)) THEN
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF; RETURN NEW;
  END IF;
  -- Every Swift writer holds this always-present shared lock until transaction
  -- end. A binding/activation takes FOR UPDATE and waits for older writers.
  SELECT * INTO STRICT f FROM swift_maintenance_20261008.fence WHERE singleton FOR SHARE;
  IF NOT f.active THEN
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF; RETURN NEW;
  END IF;
  IF TG_TABLE_NAME = 'media_asset' AND TG_OP = 'UPDATE' AND session_user = f.operator_login THEN
    UPDATE swift_maintenance_20261008.permit SET consumed=true
      WHERE operation_id=f.operation_id AND operator_login=session_user
        AND backend_pid=pg_backend_pid() AND transaction_id=txid_current()
        AND asset_id=b->>'id' AND before_row=b AND after_row=a AND NOT consumed;
    IF FOUND THEN RETURN NEW; END IF;
  END IF;
  RAISE EXCEPTION 'Swift persistent maintenance fence: write denied' USING ERRCODE='55000';
END $$;

CREATE FUNCTION swift_maintenance_20261008.no_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN RAISE EXCEPTION 'Swift maintenance immutable state / truncate denied'; END $$;
CREATE FUNCTION swift_maintenance_20261008.guard_truncate() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE f swift_maintenance_20261008.fence%ROWTYPE;
BEGIN
  SELECT * INTO STRICT f FROM swift_maintenance_20261008.fence WHERE singleton FOR SHARE;
  IF f.active THEN RAISE EXCEPTION 'Swift maintenance truncate denied'; END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER immutable_receipt BEFORE UPDATE OR DELETE ON swift_maintenance_20261008.receipt
  FOR EACH ROW EXECUTE FUNCTION swift_maintenance_20261008.no_mutation();
CREATE TRIGGER no_receipt_truncate BEFORE TRUNCATE ON swift_maintenance_20261008.receipt
  FOR EACH STATEMENT EXECUTE FUNCTION swift_maintenance_20261008.no_mutation();
CREATE TRIGGER no_fence_delete BEFORE DELETE ON swift_maintenance_20261008.fence
  FOR EACH ROW EXECUTE FUNCTION swift_maintenance_20261008.no_mutation();
CREATE TRIGGER no_fence_truncate BEFORE TRUNCATE ON swift_maintenance_20261008.fence
  FOR EACH STATEMENT EXECUTE FUNCTION swift_maintenance_20261008.no_mutation();

DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['media_source','media_asset','content_calendar','portal_action_receipt','portal_swap_guard','media_asset_review_event'] LOOP
    EXECUTE format('CREATE TRIGGER aa_swift_maintenance_fence BEFORE INSERT OR UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION swift_maintenance_20261008.guard()',t);
    EXECUTE format('ALTER TABLE public.%I ENABLE ALWAYS TRIGGER aa_swift_maintenance_fence',t);
    EXECUTE format('CREATE TRIGGER aa_swift_maintenance_no_truncate BEFORE TRUNCATE ON public.%I FOR EACH STATEMENT EXECUTE FUNCTION swift_maintenance_20261008.guard_truncate()',t);
    EXECUTE format('ALTER TABLE public.%I ENABLE ALWAYS TRIGGER aa_swift_maintenance_no_truncate',t);
  END LOOP;
END $$;

-- Only the installation administrator can bind this once. The operator must
-- connect as this actual LOGIN: SET ROLE/GUCs never prove operator identity.
-- Digest is SHA256 of manifest::jsonb::text in UTF8, computed by PostgreSQL.
CREATE FUNCTION swift_maintenance_20261008.bind(p_operation uuid,p_operator name,p_manifest_sha256 text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE f swift_maintenance_20261008.fence%ROWTYPE;
BEGIN
  SELECT * INTO STRICT f FROM swift_maintenance_20261008.fence WHERE singleton FOR UPDATE;
  IF f.operation_id IS NOT NULL OR p_operation IS NULL OR p_manifest_sha256 IS NULL
    OR p_manifest_sha256 !~ '^[0-9a-f]{64}$' OR p_operator IN ('postgres','service_role','anon','authenticated')
    OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=p_operator AND rolcanlogin
      AND NOT rolsuper AND NOT rolcreaterole AND NOT rolreplication)
    OR EXISTS (SELECT 1 FROM pg_class c WHERE c.oid IN (
      'public.media_source'::regclass,'public.media_asset'::regclass,
      'public.content_calendar'::regclass,'public.portal_action_receipt'::regclass,
      'public.portal_swap_guard'::regclass,'public.media_asset_review_event'::regclass,
      'swift_maintenance_20261008.fence'::regclass,'swift_maintenance_20261008.permit'::regclass,
      'swift_maintenance_20261008.receipt'::regclass) AND pg_has_role(p_operator,c.relowner,'MEMBER'))
    OR EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname='swift_maintenance_20261008'
      AND pg_has_role(p_operator,n.nspowner,'MEMBER'))
    OR EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
      WHERE n.nspname='swift_maintenance_20261008' AND pg_has_role(p_operator,p.proowner,'MEMBER')) THEN
    RAISE EXCEPTION 'Swift one-candidate binding unavailable / unsafe operator';
  END IF;
  UPDATE swift_maintenance_20261008.fence SET operation_id=p_operation,operator_login=p_operator,
    candidate_manifest_sha256=p_manifest_sha256,bound_at=clock_timestamp() WHERE singleton;
  EXECUTE format('GRANT USAGE ON SCHEMA swift_maintenance_20261008 TO %I',p_operator);
  EXECUTE format('GRANT EXECUTE ON FUNCTION swift_maintenance_20261008.repair(uuid,jsonb) TO %I',p_operator);
  EXECUTE format('GRANT EXECUTE ON FUNCTION swift_maintenance_20261008.release(uuid,text) TO %I',p_operator);
END $$;

CREATE FUNCTION swift_maintenance_20261008.repair(p_operation uuid,p_manifest jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE f swift_maintenance_20261008.fence%ROWTYPE; b jsonb; a jsonb; s jsonb;
  cal jsonb; ident text; expected_stage timestamptz; digest text; result jsonb;
BEGIN
  -- Shared lock throughout repair; never an exclusive fence lock while waiting
  -- on a legacy RPC's already-held calendar/asset row locks.
  SELECT * INTO STRICT f FROM swift_maintenance_20261008.fence WHERE singleton FOR SHARE;
  digest := encode(sha256(convert_to(p_manifest::text,'UTF8')),'hex');
  IF session_user IS DISTINCT FROM f.operator_login OR p_operation IS DISTINCT FROM f.operation_id
    OR digest IS DISTINCT FROM f.candidate_manifest_sha256 OR NOT f.active
    OR EXISTS (SELECT 1 FROM swift_maintenance_20261008.receipt WHERE operation_id=p_operation) THEN
    RAISE EXCEPTION 'Swift operator/binding/replay denied';
  END IF;
  IF jsonb_typeof(p_manifest) IS DISTINCT FROM 'object'
    OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(p_manifest) k)
      IS DISTINCT FROM ARRAY['asset_after','asset_before','calendar_before','ledger_sha256','source_before']::text[]
    OR jsonb_typeof(p_manifest->'calendar_before') IS DISTINCT FROM 'array'
    OR coalesce(p_manifest->>'ledger_sha256','') !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'Swift exact candidate manifest required';
  END IF;
  ident := p_manifest->'asset_before'->>'id';
  expected_stage := CASE ident
    WHEN '1PhOFVHpEm8_ye2a546WT2VGwpRtCXcFr' THEN '2026-10-07T01:05:48.303394+00:00'::timestamptz
    WHEN '1esVxnCv-EU-6N5R_-QYUNxCg9oypjk4s' THEN '2026-10-07T02:02:42.327208+00:00'::timestamptz
    WHEN '1Vqo3q7OdbQcO5txCh_VrRIuFJryrTOlP' THEN '2026-10-07T01:21:54.057128+00:00'::timestamptz END;
  IF expected_stage IS NULL THEN RAISE EXCEPTION 'Swift candidate is not an authorized orphan identity'; END IF;
  SELECT to_jsonb(m) INTO b FROM public.media_asset m WHERE id=ident FOR UPDATE;
  SELECT to_jsonb(m) INTO s FROM public.media_source m WHERE id=b->>'source_id' FOR SHARE;
  SELECT coalesce(jsonb_agg(to_jsonb(c) ORDER BY c.id),'[]'::jsonb) INTO cal
    FROM public.content_calendar c WHERE swift_maintenance_20261008.row_is_swift('content_calendar',to_jsonb(c));
  a := b || jsonb_build_object('used_count',0,'last_used_at',NULL);
  IF b IS NULL OR s IS NULL OR b IS DISTINCT FROM p_manifest->'asset_before'
    OR s IS DISTINCT FROM p_manifest->'source_before' OR a IS DISTINCT FROM p_manifest->'asset_after'
    OR cal IS DISTINCT FROM p_manifest->'calendar_before'
    OR b->>'gym_id' IS DISTINCT FROM 'swiftrivercrossfite5c9db'
    OR s->>'gym_id' IS DISTINCT FROM 'swiftrivercrossfite5c9db' OR s->>'active' IS DISTINCT FROM 'true'
    OR b->>'used_count' IS DISTINCT FROM '1' OR (b->>'last_used_at')::timestamptz IS DISTINCT FROM expected_stage
    OR coalesce(b->>'content_hash','')='' OR b->>'drive_modified' IS NULL
    OR b->>'review_status' IS DISTINCT FROM 'approved' OR b->>'eligible' IS DISTINCT FROM 'true'
    OR b->>'excluded_by_coach' IS DISTINCT FROM 'false'
    OR EXISTS (SELECT 1 FROM public.content_calendar c WHERE c.source_media_asset_id=ident)
    OR EXISTS (SELECT 1 FROM public.portal_action_receipt r
      WHERE r.selected_asset->>'asset_id'=ident OR (swift_maintenance_20261008.row_is_swift('portal_action_receipt',to_jsonb(r))
        AND r.status NOT IN ('succeeded','failed')))
    OR EXISTS (SELECT 1 FROM public.portal_swap_guard g WHERE
      swift_maintenance_20261008.row_is_swift('portal_swap_guard',to_jsonb(g))
      AND g.status='active') THEN RAISE EXCEPTION 'Swift exact orphan/source/calendar/receipt preconditions changed'; END IF;
  INSERT INTO swift_maintenance_20261008.permit
    (operation_id,backend_pid,transaction_id,operator_login,asset_id,before_row,after_row)
    VALUES(p_operation,pg_backend_pid(),txid_current(),session_user,ident,b,a);
  UPDATE public.media_asset SET used_count=0,last_used_at=NULL WHERE id=ident;
  SELECT to_jsonb(m) INTO result FROM public.media_asset m WHERE id=ident;
  IF result IS DISTINCT FROM a OR NOT EXISTS (SELECT 1 FROM swift_maintenance_20261008.permit
    WHERE operation_id=p_operation AND consumed) THEN RAISE EXCEPTION 'Swift post-write drift / permit unconsumed'; END IF;
  INSERT INTO swift_maintenance_20261008.receipt
    (operation_id,operator_login,candidate_manifest_sha256,manifest,before_row,after_row,transaction_id)
    VALUES(p_operation,session_user,digest,p_manifest,b,result,txid_current());
  DELETE FROM swift_maintenance_20261008.permit WHERE operation_id=p_operation;
  RETURN jsonb_build_object('operation_id',p_operation,'asset_id',ident,'before',b,'after',result,
    'postgres_repair_only',true,'fence_retained',true);
END $$;

-- An independent reviewer must read back the committed repair receipt AND the
-- SQLite/provider/owner evidence before the operator calls this function.
-- This narrowly checks the actual SQL receipt digest; it does not certify that
-- external review or any external outcome. A receipt is SQL-counter-only.
CREATE FUNCTION swift_maintenance_20261008.release(p_operation uuid,p_expected_receipt_sha256 text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE f swift_maintenance_20261008.fence%ROWTYPE; r jsonb; actual_digest text;
BEGIN
  SELECT * INTO STRICT f FROM swift_maintenance_20261008.fence WHERE singleton FOR UPDATE;
  SELECT to_jsonb(t) INTO r FROM swift_maintenance_20261008.receipt t WHERE operation_id=p_operation;
  actual_digest := encode(sha256(convert_to(r::text,'UTF8')),'hex');
  IF NOT f.active OR session_user IS DISTINCT FROM f.operator_login
    OR p_operation IS DISTINCT FROM f.operation_id OR r IS NULL
    OR (r->>'transaction_id')::bigint = txid_current()
    OR p_expected_receipt_sha256 IS NULL OR p_expected_receipt_sha256 !~ '^[0-9a-f]{64}$'
    OR p_expected_receipt_sha256 IS DISTINCT FROM actual_digest
    OR EXISTS (SELECT 1 FROM swift_maintenance_20261008.permit) THEN
    RAISE EXCEPTION 'Swift release operation/operator/receipt readback denied';
  END IF;
  UPDATE swift_maintenance_20261008.fence SET active=false,
    release_receipt_sha256=actual_digest,released_at=clock_timestamp() WHERE singleton;
  RETURN jsonb_build_object('operation_id',p_operation,'released',true,'receipt_sha256',actual_digest);
END $$;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA swift_maintenance_20261008 FROM PUBLIC, anon, authenticated, service_role;
-- bind remains installation-owner only; repair is granted only by exact binding.
COMMIT;
