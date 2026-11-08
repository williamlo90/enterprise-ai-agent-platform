-- FLOW-01: local source binding + leased dispatch and authenticated settlement.
ALTER TABLE {schema}.outbox_jobs DROP CONSTRAINT outbox_jobs_error_code_check;
ALTER TABLE {schema}.outbox_jobs ADD CHECK(error_code IN ('transient','rate_limited','invalid','forbidden','unknown_commit','lease_expired','authority_changed','service_rejected'));
CREATE TABLE {schema}.action_claim_bindings (
 tenant_id uuid NOT NULL,operation_id uuid NOT NULL,canonical_hash text NOT NULL,
 policy jsonb NOT NULL,trip_date date NOT NULL,claim_id uuid,claim_version bigint,revision bigint,stage bigint,
 PRIMARY KEY(tenant_id,operation_id),FOREIGN KEY(tenant_id,operation_id) REFERENCES {schema}.actions(tenant_id,id)
);
CREATE TABLE {schema}.flow_results (
 tenant_id uuid NOT NULL,operation_id uuid NOT NULL,terminal jsonb NOT NULL,terminal_digest text NOT NULL,
 authority_expires_at timestamptz NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,operation_id),FOREIGN KEY(tenant_id,operation_id) REFERENCES {schema}.actions(tenant_id,id)
);
ALTER TABLE {schema}.action_claim_bindings ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.action_claim_bindings FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.flow_results ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.flow_results FORCE ROW LEVEL SECURITY;
CREATE POLICY maintenance ON {schema}.action_claim_bindings TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.flow_results TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.action_claim_bindings,{schema}.flow_results FROM PUBLIC,{access_role};
-- A dedicated immutable guard also covers tables not known to the draft guard.
CREATE FUNCTION {schema}._flow_immutable() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN RAISE EXCEPTION 'Immutable workflow record' USING ERRCODE='23514';END $$;
CREATE TRIGGER binding_immutable BEFORE UPDATE OR DELETE ON {schema}.action_claim_bindings FOR EACH ROW EXECUTE FUNCTION {schema}._flow_immutable();
CREATE TRIGGER flow_result_immutable BEFORE UPDATE OR DELETE ON {schema}.flow_results FOR EACH ROW EXECUTE FUNCTION {schema}._flow_immutable();
CREATE FUNCTION {schema}._flow_expiry() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN IF clock_timestamp()>=NEW.authority_expires_at THEN RAISE EXCEPTION 'Flow proof expired' USING ERRCODE='40001';END IF;RETURN NULL;END $$;
CREATE CONSTRAINT TRIGGER flow_result_expiry AFTER INSERT ON {schema}.flow_results DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {schema}._flow_expiry();
REVOKE ALL ON FUNCTION {schema}._flow_immutable(),{schema}._flow_expiry() FROM PUBLIC,{access_role};

ALTER TABLE {schema}.actions ADD COLUMN result_ref uuid,ADD COLUMN result_version bigint,ADD COLUMN result_code text;
ALTER TABLE {schema}.actions ADD CHECK((result_ref IS NULL)=(result_version IS NULL));
CREATE OR REPLACE FUNCTION {schema}._action_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF TG_OP='DELETE' OR TG_TABLE_NAME IN ('consents','action_audit') THEN RAISE EXCEPTION 'Immutable action history' USING ERRCODE='23514';END IF;
 IF TG_TABLE_NAME='human_action_receipts' THEN
  IF (to_jsonb(NEW)-'revoked') IS DISTINCT FROM (to_jsonb(OLD)-'revoked') OR OLD.revoked THEN RAISE EXCEPTION 'Immutable human receipt' USING ERRCODE='23514';END IF;RETURN NEW;
 END IF;
 IF (to_jsonb(NEW)-'state'-'updated_at'-'result_ref'-'result_version'-'result_code') IS DISTINCT FROM (to_jsonb(OLD)-'state'-'updated_at'-'result_ref'-'result_version'-'result_code')
  OR OLD.state IN ('committed','not_committed') OR NOT ((OLD.state IN ('queued','unknown') AND NEW.state='sending') OR (OLD.state='sending' AND NEW.state IN ('unknown','committed','not_committed')))
  OR (NEW.state NOT IN ('committed','not_committed') AND (NEW.result_ref IS NOT NULL OR NEW.result_version IS NOT NULL OR NEW.result_code IS NOT NULL))
  THEN RAISE EXCEPTION 'Invalid action transition' USING ERRCODE='23514';END IF;RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION {schema}._action_json(a {schema}.actions) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('operation_id',a.id,'kind',a.kind,'state',a.state,'draft_id',a.draft_id,'draft_version',a.draft_version,'claim_id',a.claim_ref,
  'claim_version',a.claim_version,'material_hash',a.material_hash,'request_hash',a.canonical_hash,'human_receipt_id',a.human_receipt_id,
  'result_ref',a.result_ref,'result_version',a.result_version,'result_code',a.result_code,
  'delivery_state',(SELECT state FROM {schema}.outbox_jobs j WHERE j.tenant_id=a.tenant_id AND j.id=a.id),
  'last_error_code',(SELECT error_code FROM {schema}.outbox_jobs j WHERE j.tenant_id=a.tenant_id AND j.id=a.id));
$$;

-- Keep ACT25 immutable. Capture only the already verified source of a newly
-- confirmed action; never take a free claim binding from a browser or callback.
ALTER FUNCTION {schema}.action_call(text,text,jsonb) RENAME TO _flow_action_base;
REVOKE ALL ON FUNCTION {schema}._flow_action_base(text,text,jsonb) FROM PUBLIC,{access_role};
CREATE OR REPLACE FUNCTION {schema}._flow_action_base(token text,operation text,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
#variable_conflict use_column
<<intent>>
DECLARE auth jsonb;identity jsonb;context jsonb;source jsonb;preview jsonb;result jsonb;data jsonb;canonical jsonb;claim jsonb;
 a {schema}.actions%ROWTYPE;d {schema}.drafts%ROWTYPE;s {schema}.sessions%ROWTYPE;pkg {schema}.policy_packages%ROWTYPE;
 t uuid;person uuid;key uuid;op uuid;receipt uuid;hash text;canonical_hash text;kind text;deadline numeric;epoch bigint;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Action capability rejected';END IF;
 IF operation IS NULL OR operation NOT IN ('confirm','decision','read') OR jsonb_typeof(payload) IS DISTINCT FROM 'object' OR octet_length(payload::text)>131072 THEN RETURN jsonb_build_object('status','invalid');END IF;
 auth:={schema}.authorization_call(token,'context','{{}}'::jsonb);
 IF auth->>'status' IS DISTINCT FROM 'ok' THEN RETURN auth;END IF;
 identity:=auth->'identity';t:=(identity->>'active_tenant_id')::uuid;person:=(identity->>'principal_id')::uuid;
 IF operation<>'read' THEN
  auth:={schema}.authorization_call(token,'finish',jsonb_build_object('csrf_hash',payload->>'csrf_hash'));
  IF auth->>'status' IS DISTINCT FROM 'ok' THEN RETURN auth;END IF;
 END IF;
 SELECT * INTO s FROM {schema}.sessions WHERE token_hash=token FOR SHARE;
 context:=jsonb_build_object('tenant_id',t,'actor_id',person,'membership_id',identity->>'active_membership_id','session_id',s.id,'access_revision',identity->'access_revision');
 deadline:=extract(epoch FROM least(s.expires_at,s.last_seen_at+interval '1800 seconds'));
 IF operation='read' THEN
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>1 OR NOT payload ? 'id' THEN RETURN jsonb_build_object('status','invalid');END IF;
  SELECT * INTO a FROM {schema}.actions WHERE tenant_id=t AND id=(_flow_action_base.payload->>'id')::uuid AND actor_id=person FOR SHARE;
  IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
 ELSE
  IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(auth->'grants') g WHERE
   (operation='confirm' AND g->>'role'='employee' AND g->>'scope_kind'='tenant' AND g->>'scope_ref'=t::text)
   OR (operation='decision' AND g->>'role'='approver' AND g->>'scope_kind'='process' AND g->>'scope_ref'='reimbursement')) THEN RETURN jsonb_build_object('status','forbidden');END IF;
  IF operation='confirm' THEN
   IF (SELECT count(*) FROM jsonb_object_keys(payload)) NOT IN (6,8) OR NOT payload ?& ARRAY['id','expected_version','material_hash','key','csrf_hash','source']
    OR jsonb_typeof(payload->'expected_version') IS DISTINCT FROM 'number' OR payload->>'expected_version' !~ '^[1-9][0-9]*$'
    OR payload->>'material_hash' !~ '^[a-f0-9]{{64}}$' THEN RETURN jsonb_build_object('status','invalid');END IF;
  ELSE
   IF (SELECT count(*) FROM jsonb_object_keys(payload)) NOT IN (9,11) OR NOT payload ?& ARRAY['id','expected_version','revision','stage','decision','reason','key','csrf_hash','source']
    OR jsonb_typeof(payload->'expected_version') IS DISTINCT FROM 'number' OR payload->>'expected_version' !~ '^[1-9][0-9]*$'
    OR jsonb_typeof(payload->'revision') IS DISTINCT FROM 'number' OR payload->>'revision' !~ '^[1-9][0-9]*$'
    OR jsonb_typeof(payload->'stage') IS DISTINCT FROM 'number' OR payload->>'stage' !~ '^([1-9]|10)$'
    OR payload->>'decision' IS NULL OR payload->>'decision' NOT IN ('approve','reject','request_information')
    OR jsonb_typeof(payload->'reason') IS DISTINCT FROM 'string' OR octet_length(payload->>'reason')>4096
    OR (payload->>'decision'<>'approve' AND btrim(payload->>'reason',U&'\0009\000A\000B\000C\000D\001C\001D\001E\001F\0020\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000')='') THEN RETURN jsonb_build_object('status','invalid');END IF;
  END IF;
  key:=(payload->>'key')::uuid;
  IF key IS NULL OR key='00000000-0000-0000-0000-000000000000'::uuid OR (payload->>'id')::uuid IS NULL THEN RETURN jsonb_build_object('status','invalid');END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('action-key:'||t::text||':'||person::text||':'||key::text,0));
  hash:=encode(sha256(convert_to('eai:action-request:v1:'||operation||':'||{schema}._policy_canonical(payload-'source'-'csrf_hash'-'operation_id'-'receipt_id'),'UTF8')),'hex');
  SELECT * INTO a FROM {schema}.actions WHERE tenant_id=t AND actor_id=person AND actions.key=intent.key FOR SHARE;
  IF FOUND THEN
   IF a.request_identity_hash<>hash THEN RETURN jsonb_build_object('status','conflict');END IF;
  ELSE
   IF operation='confirm' THEN
    SELECT * INTO d FROM {schema}.drafts WHERE tenant_id=t AND id=(payload->>'id')::uuid AND owner_principal_id=person FOR UPDATE;
    IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
    IF d.state<>'editable' OR d.current_version<>(payload->>'expected_version')::bigint THEN RETURN jsonb_build_object('status','conflict');END IF;
   END IF;
   IF payload->'source'='null'::jsonb THEN
    RETURN jsonb_build_object('status','ok','action',NULL,'draft',CASE WHEN operation='confirm' THEN {schema}._draft_json(d) ELSE NULL END,'context',context||jsonb_build_object('authority_expires_at',deadline),'authority_expires_at',deadline);
   END IF;
   op:=(payload->>'operation_id')::uuid;receipt:=(payload->>'receipt_id')::uuid;
   IF op IS NULL OR receipt IS NULL OR op='00000000-0000-0000-0000-000000000000'::uuid OR receipt='00000000-0000-0000-0000-000000000000'::uuid THEN RETURN jsonb_build_object('status','invalid');END IF;
   IF operation='confirm' THEN
    result:={schema}.draft_call(token,'preview',jsonb_build_object('id',d.id,'expected_version',d.current_version,'csrf_hash',payload->>'csrf_hash','source',payload->'source'));
    IF result->>'status' IS DISTINCT FROM 'ok' THEN RETURN result;END IF;
    preview:=result->'preview';
    IF preview->>'material_hash' IS DISTINCT FROM payload->>'material_hash' THEN RETURN jsonb_build_object('status','conflict');END IF;
    deadline:=least(deadline,(result->>'authority_expires_at')::numeric);
    kind:=CASE WHEN d.kind='submission' THEN 'claim.submit' ELSE 'claim.information' END;data:=preview->'data';
    -- Match the canonical v1 financial contract even for structurally valid
    -- draft content saved through the restricted SQL capability directly.
    data:=jsonb_set(data,'{{items}}',(SELECT jsonb_agg(jsonb_set(entry,'{{receipt_ids}}',
      (SELECT coalesce(jsonb_agg(ref ORDER BY ref),'[]'::jsonb) FROM jsonb_array_elements(entry->'receipt_ids') refs(ref))) ORDER BY entry->>'id')
      FROM jsonb_array_elements(data->'items') items(entry)));
    canonical:=jsonb_build_object('schema_version',1,'action',kind,'tenant_id',t,'actor_id',person,'operation_id',op,
     'object',jsonb_build_object('kind',CASE WHEN kind='claim.submit' THEN 'draft' ELSE 'claim' END,'id',CASE WHEN kind='claim.submit' THEN d.id ELSE d.claim_ref END,'version',CASE WHEN kind='claim.submit' THEN d.current_version ELSE d.claim_version END),'data',data);
   ELSE
    source:={schema}._decision_source(context,payload->'source');
    IF source IS NULL THEN RETURN jsonb_build_object('status','unavailable');END IF;
    claim:=source->'claim';
    IF claim->>'id' IS DISTINCT FROM payload->>'id' OR claim->>'version' IS DISTINCT FROM payload->>'expected_version' OR claim->>'revision' IS DISTINCT FROM payload->>'revision'
     OR claim->>'active_stage' IS DISTINCT FROM payload->>'stage' OR claim->>'state' IS DISTINCT FROM 'pending_approval' THEN RETURN jsonb_build_object('status','conflict');END IF;
    IF claim->>'reviewer_id' IS DISTINCT FROM person::text OR claim->>'owner_id'=person::text THEN RETURN jsonb_build_object('status','forbidden');END IF;
    PERFORM tenant_id FROM {schema}.policy_coordination WHERE tenant_id=t FOR SHARE;
    SELECT * INTO pkg FROM {schema}.policy_packages WHERE tenant_id=t AND id=(claim->'policy'->>'id')::uuid AND package_version=(claim->'policy'->>'version')::bigint FOR SHARE;
    IF NOT FOUND OR pkg.publication_state<>'active' OR NOT pkg.allowed_for_locked_claims OR pkg.digest IS DISTINCT FROM claim->'policy'->>'digest'
     OR (claim->>'trip_date')::date<pkg.valid_trip_end_from OR (pkg.valid_trip_end_to IS NOT NULL AND (claim->>'trip_date')::date>=pkg.valid_trip_end_to)
     OR NOT {schema}._knowledge_policy_sources(t,person,pkg.document_refs,false,false)
     OR NOT EXISTS(SELECT 1 FROM jsonb_array_elements(pkg.rules_json->'approval_route') r WHERE r->>'stage'=payload->>'stage' AND r->>'approver_id'=person::text)
     THEN RETURN jsonb_build_object('status','incomplete');END IF;
    deadline:=least(deadline,(source->>'expires_at')::numeric);kind:='claim.decide';
    canonical:=jsonb_build_object('schema_version',1,'action',kind,'tenant_id',t,'actor_id',person,'operation_id',op,'object',jsonb_build_object('kind','claim','id',payload->>'id','version',(payload->>'expected_version')::bigint),
     'data',jsonb_build_object('revision',(payload->>'revision')::bigint,'stage',(payload->>'stage')::bigint,'decision',payload->>'decision','reason',payload->>'reason'));
   END IF;
   IF octet_length(convert_to({schema}._policy_canonical(canonical),'UTF8'))>65536 THEN RETURN jsonb_build_object('status','invalid');END IF;
   canonical_hash:=encode(sha256(convert_to('eai:human-action:v1:'||{schema}._policy_canonical(canonical),'UTF8')),'hex');
   epoch:=floor(extract(epoch FROM clock_timestamp()))::bigint;
   INSERT INTO {schema}.human_action_receipts VALUES(receipt,t,person,s.id,op,kind,canonical->'object'->>'kind',(canonical->'object'->>'id')::uuid,(canonical->'object'->>'version')::bigint,canonical_hash,epoch,false);
   INSERT INTO {schema}.actions(tenant_id,id,actor_id,membership_id,session_id,access_revision,kind,key,request_identity_hash,draft_id,draft_version,material_hash,claim_ref,claim_version,human_receipt_id,payload,canonical_hash,confirmation_expires_at)
    VALUES(t,op,person,(identity->>'active_membership_id')::uuid,s.id,(identity->>'access_revision')::bigint,kind,key,hash,
     CASE WHEN operation='confirm' THEN d.id ELSE NULL END,CASE WHEN operation='confirm' THEN d.current_version ELSE NULL END,CASE WHEN operation='confirm' THEN preview->>'material_hash' ELSE NULL END,
     CASE WHEN operation='confirm' THEN d.claim_ref ELSE (payload->>'id')::uuid END,CASE WHEN operation='confirm' THEN d.claim_version ELSE (payload->>'expected_version')::bigint END,
     receipt,canonical,canonical_hash,to_timestamp(deadline::double precision)) RETURNING * INTO a;
   IF operation='confirm' THEN
    INSERT INTO {schema}.consents(tenant_id,draft_id,draft_version,operation_id,actor_id,session_id,material_hash,human_receipt_id) VALUES(t,d.id,d.current_version,op,person,s.id,a.material_hash,receipt);
    UPDATE {schema}.drafts SET state='frozen',updated_at=clock_timestamp() WHERE tenant_id=t AND id=d.id;
   END IF;
   INSERT INTO {schema}.job_tenants(tenant_id) VALUES(t) ON CONFLICT DO NOTHING;
   INSERT INTO {schema}.outbox_jobs(tenant_id,id,job_type,business_key,target_id,expected_version,actor_id,membership_id,session_id,access_revision)
    VALUES(t,op,'action.dispatch',op,op,1,person,a.membership_id,s.id,a.access_revision);
   INSERT INTO {schema}.action_audit(tenant_id,operation_id,actor_id,kind,canonical_hash) VALUES(t,op,person,kind,canonical_hash);
  END IF;
 END IF;
 auth:={schema}.authorization_call(token,CASE WHEN operation='read' THEN 'context' ELSE 'finish' END,CASE WHEN operation='read' THEN '{{}}'::jsonb ELSE jsonb_build_object('csrf_hash',payload->>'csrf_hash') END);
 IF auth->>'status' IS DISTINCT FROM 'ok' OR extract(epoch FROM clock_timestamp())>=deadline THEN RAISE EXCEPTION 'Action authority expired' USING ERRCODE='40001';END IF;
 RETURN jsonb_build_object('status','ok','action',{schema}._action_json(a),'authority_expires_at',deadline);
EXCEPTION WHEN unique_violation THEN RETURN jsonb_build_object('status','conflict');
 WHEN data_exception OR check_violation THEN RETURN jsonb_build_object('status','invalid');
END $$;
CREATE FUNCTION {schema}.action_call(token text,operation text,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE result jsonb;a {schema}.actions%ROWTYPE;material jsonb;claim jsonb;policy jsonb;day date;context jsonb;
BEGIN
 result:={schema}._flow_action_base(token,operation,payload);
 IF result->>'status'='ok' AND operation IN ('confirm','decision') AND result->'action'<>'null'::jsonb AND payload->'source'<>'null'::jsonb THEN
  SELECT * INTO a FROM {schema}.actions WHERE id=(result->'action'->>'operation_id')::uuid;
  IF a.id IS NOT NULL AND a.created_at>=statement_timestamp() AND NOT EXISTS(SELECT 1 FROM {schema}.action_claim_bindings WHERE tenant_id=a.tenant_id AND operation_id=a.id) THEN
   context:=jsonb_build_object('tenant_id',a.tenant_id,'actor_id',a.actor_id,'membership_id',a.membership_id,'session_id',a.session_id,'access_revision',a.access_revision);
   material:=CASE WHEN operation='decision' THEN {schema}._decision_source(context,payload->'source') ELSE {schema}._draft_source(context,payload->'source') END;
   IF material IS NULL THEN RAISE EXCEPTION 'Binding source not verified' USING ERRCODE='40001';END IF;
   claim:=material->'claim';
   IF operation='decision' THEN policy:=claim->'policy';day:=(claim->>'trip_date')::date;
   ELSE policy:=a.payload->'data'->'policy';day:=(material->'trip'->>'end_date')::date;END IF;
   INSERT INTO {schema}.action_claim_bindings VALUES(a.tenant_id,a.id,a.canonical_hash,policy,day,a.claim_ref,a.claim_version,
    CASE WHEN a.kind='claim.submit' THEN NULL ELSE (a.payload->'data'->>'revision')::bigint END,
    CASE WHEN a.kind='claim.decide' THEN (a.payload->'data'->>'stage')::bigint ELSE NULL END);
  END IF;
 END IF;
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION {schema}.action_call(text,text,jsonb) FROM PUBLIC;GRANT EXECUTE ON FUNCTION {schema}.action_call(text,text,jsonb) TO {access_role};

CREATE FUNCTION {schema}.flow_locked_call(actor jsonb,payload jsonb,policy jsonb,trip_date date,claim_id uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE a {schema}.actions%ROWTYPE;b {schema}.action_claim_bindings%ROWTYPE;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Flow capability rejected';END IF;
 SELECT * INTO a FROM {schema}.actions WHERE tenant_id=(actor->>'tenant_id')::uuid AND id=(actor->>'operation_id')::uuid FOR SHARE;
 IF NOT FOUND OR a.actor_id::text IS DISTINCT FROM actor->>'actor_id' OR a.membership_id::text IS DISTINCT FROM actor->>'membership_id'
  OR a.access_revision::text IS DISTINCT FROM actor->>'access_revision' OR a.canonical_hash IS DISTINCT FROM actor->>'request_hash'
  OR a.payload IS DISTINCT FROM payload OR a.kind NOT IN ('claim.information','claim.decide') OR a.state NOT IN ('queued','sending','unknown')
  OR {schema}.human_receipt_get(a.human_receipt_id) IS NULL THEN RETURN false;END IF;
 SELECT * INTO b FROM {schema}.action_claim_bindings WHERE tenant_id=a.tenant_id AND operation_id=a.id FOR SHARE;
 RETURN b.operation_id IS NOT NULL AND b.canonical_hash=a.canonical_hash AND b.claim_id=flow_locked_call.claim_id
  AND b.claim_version=(payload->'object'->>'version')::bigint AND b.policy=flow_locked_call.policy AND b.trip_date=flow_locked_call.trip_date
  AND b.revision=(payload->'data'->>'revision')::bigint AND (a.kind<>'claim.decide' OR b.stage=(payload->'data'->>'stage')::bigint);
EXCEPTION WHEN data_exception THEN RETURN false;
END $$;
REVOKE ALL ON FUNCTION {schema}.flow_locked_call(jsonb,jsonb,jsonb,date,uuid) FROM PUBLIC;GRANT EXECUTE ON FUNCTION {schema}.flow_locked_call(jsonb,jsonb,jsonb,date,uuid) TO {access_role};

CREATE OR REPLACE FUNCTION {schema}._claim_policy_binding_valid(claims jsonb,request jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF {schema}._receipt_human_current(claims,request->'payload') IS NOT TRUE THEN RETURN false;END IF;
 RETURN {schema}.flow_locked_call(jsonb_build_object('tenant_id',claims->>'tenant_id','actor_id',claims->>'sub',
  'membership_id',claims->>'membership_id','access_revision',claims->'access_revision','operation_id',claims->>'operation_id','request_hash',claims->>'request_hash'),
  request->'payload',request->'policy',(request->>'trip_date')::date,(request->>'claim_ref')::uuid);
EXCEPTION WHEN data_exception THEN RETURN false;
END $$;
REVOKE ALL ON FUNCTION {schema}._claim_policy_binding_valid(jsonb,jsonb) FROM PUBLIC,{access_role};

ALTER FUNCTION {schema}._receipt_human_current(jsonb,jsonb) RENAME TO _flow_human_base;
REVOKE ALL ON FUNCTION {schema}._flow_human_base(jsonb,jsonb) FROM PUBLIC,{access_role};
CREATE FUNCTION {schema}._receipt_human_current(claims jsonb,payload jsonb) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE state text;BEGIN
 SELECT a.state INTO state FROM {schema}.actions a WHERE a.tenant_id=(claims->>'tenant_id')::uuid AND a.id=(claims->>'operation_id')::uuid FOR SHARE;
 RETURN state IN ('queued','sending','unknown') AND {schema}._flow_human_base(claims,payload);
EXCEPTION WHEN data_exception THEN RETURN false;END $$;
REVOKE ALL ON FUNCTION {schema}._receipt_human_current(jsonb,jsonb) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._flow_identity(a {schema}.actions) RETURNS jsonb LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('tenant_id',a.tenant_id,'operation_id',a.id,'actor_id',a.actor_id,'caller','platform-dispatcher','kind',a.kind,'key',a.key,
  'request_hash',a.canonical_hash,'object_kind',a.payload->'object'->>'kind','object_id',a.payload->'object'->>'id','object_version',a.payload->'object'->'version');
$$;
REVOKE ALL ON FUNCTION {schema}._flow_identity({schema}.actions) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.flow_call(operation text,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
#variable_conflict use_column
DECLARE ref jsonb;a {schema}.actions%ROWTYPE;j {schema}.outbox_jobs%ROWTYPE;c {schema}.job_callers%ROWTYPE;r {schema}.human_action_receipts%ROWTYPE;
 k {schema}.receipt_receiver_keys%ROWTYPE;recipient {schema}.internal_caller_credentials%ROWTYPE;batch {schema}.receipt_pin_batches%ROWTYPE;
 context jsonb;identity jsonb;material jsonb;term jsonb;result jsonb;settlement jsonb;deadline numeric;mode text;target text;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Flow capability rejected';END IF;
 IF operation NOT IN ('prepare','pins','uncertain','hold','settle') OR operation IS NULL OR jsonb_typeof(payload) IS DISTINCT FROM 'object' OR octet_length(payload::text)>131072
  OR NOT payload ?& ARRAY['credential_hash','id','generation'] THEN RETURN jsonb_build_object('status','invalid');END IF;
 ref:={schema}.jobs_call('reference',jsonb_build_object('credential_hash',payload->>'credential_hash','id',payload->>'id','generation',payload->'generation'));
 IF ref->>'status' IS DISTINCT FROM 'ok' THEN RETURN ref;END IF;
 IF ref->>'job_type' NOT IN ('action.dispatch','action.reconcile') THEN RETURN jsonb_build_object('status','forbidden');END IF;
 SELECT * INTO c FROM {schema}.job_callers WHERE credential_hash=payload->>'credential_hash' FOR SHARE;
 SELECT * INTO j FROM {schema}.outbox_jobs WHERE tenant_id=c.tenant_id AND id=(payload->>'id')::uuid;
 SELECT * INTO a FROM {schema}.actions WHERE tenant_id=j.tenant_id AND id=j.target_id FOR UPDATE;
 IF NOT FOUND OR a.actor_id<>j.actor_id OR a.session_id<>j.session_id OR a.membership_id<>j.membership_id OR a.access_revision<>j.access_revision OR j.expected_version<>1
  THEN RETURN jsonb_build_object('status','forbidden');END IF;
 identity:={schema}._flow_identity(a);
 IF a.state IN ('committed','not_committed') THEN RETURN jsonb_build_object('status','conflict');END IF;
 context:=jsonb_build_object('tenant_id',a.tenant_id,'actor_id',a.actor_id,'membership_id',a.membership_id,'session_id',a.session_id,'access_revision',a.access_revision);
 deadline:=floor(extract(epoch FROM least(j.lease_until,j.attempt_deadline,c.expires_at)));
 IF operation='prepare' THEN
  mode:=CASE WHEN a.state='queued' THEN 'dispatch' ELSE 'recover' END;
  IF mode='dispatch' AND {schema}.human_receipt_get(a.human_receipt_id) IS NULL THEN RETURN jsonb_build_object('status','forbidden');END IF;
  IF NOT EXISTS(SELECT 1 FROM {schema}.membership_roles role WHERE role.tenant_id=a.tenant_id AND role.membership_id=a.membership_id AND
   ((a.kind IN ('claim.submit','claim.information') AND role.role='employee' AND role.scope_kind='tenant' AND role.scope_ref=a.tenant_id::text)
    OR (a.kind='claim.decide' AND role.role='approver' AND role.scope_kind='process' AND role.scope_ref='reimbursement')))
   THEN RETURN jsonb_build_object('status','forbidden');END IF;
  SELECT * INTO r FROM {schema}.human_action_receipts WHERE id=a.human_receipt_id FOR SHARE;
  IF a.state<>'sending' THEN UPDATE {schema}.actions SET state='sending',updated_at=clock_timestamp() WHERE tenant_id=a.tenant_id AND id=a.id;END IF;
  RETURN jsonb_build_object('status','ok','mode',mode,'payload',a.payload,'identity',identity,'context',context,'receipt',to_jsonb(r),'expires_at',deadline);
 ELSIF operation='pins' THEN
  SELECT * INTO batch FROM {schema}.receipt_pin_batches WHERE tenant_id=a.tenant_id AND operation_id=a.id FOR SHARE;
  RETURN jsonb_build_object('status','ok','batch',CASE WHEN FOUND THEN batch.batch ELSE NULL END,'expires_at',deadline);
 ELSIF operation IN ('uncertain','hold') THEN
  IF a.state='sending' THEN UPDATE {schema}.actions SET state='unknown',updated_at=clock_timestamp() WHERE tenant_id=a.tenant_id AND id=a.id;END IF;
  UPDATE {schema}.outbox_jobs SET state=CASE WHEN operation='hold' OR attempts>=retry_limit THEN 'held' ELSE 'retry' END,
   job_type='action.reconcile',worker_id=NULL,lease_until=NULL,attempt_deadline=NULL,error_code=CASE WHEN operation='hold' THEN 'service_rejected' ELSE 'unknown_commit' END,
   next_attempt_at=clock_timestamp()+interval '2 seconds',updated_at=clock_timestamp() WHERE tenant_id=j.tenant_id AND id=j.id;
  RETURN jsonb_build_object('status','ok','state','unknown');
 END IF;
 -- Settlement material can only be minted after exact signature verification.
 settlement:=payload->'settlement';material:=settlement->'material';term:=material->'terminal';
 IF jsonb_typeof(settlement) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(settlement))<>4 OR NOT settlement ?& ARRAY['credential_hash','key_id','material','proof']
  OR jsonb_typeof(material) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(material))<>5 OR NOT material ?& ARRAY['schema_version','identity','terminal','terminal_digest','expires_at']
  OR material->'schema_version' IS DISTINCT FROM '1'::jsonb OR material->'identity' IS DISTINCT FROM identity OR term->'identity' IS DISTINCT FROM identity
  OR term->>'state' NOT IN ('committed','sealed_no_commit') OR term->>'state' IS NULL OR jsonb_typeof(material->'expires_at') IS DISTINCT FROM 'number'
  OR material->>'expires_at' !~ '^[1-9][0-9]*$' THEN RETURN jsonb_build_object('status','invalid');END IF;
 SELECT * INTO recipient FROM {schema}.internal_caller_credentials WHERE credential_hash=settlement->>'credential_hash' FOR SHARE;
 SELECT * INTO k FROM {schema}.receipt_receiver_keys WHERE credential_hash=recipient.credential_hash AND key_id=settlement->>'key_id' FOR SHARE;
 IF k.key_id IS NULL OR NOT k.active OR NOT recipient.active OR recipient.audience<>'reference-reimbursement' OR NOT recipient.allowed_actions ? a.kind
  OR clock_timestamp()<k.not_before OR clock_timestamp()>=least(k.not_after,recipient.expires_at)
  OR settlement->>'proof' IS DISTINCT FROM encode({schema}._policy_hmac(k.key_material,convert_to('eai:flow-settlement:v1:'||{schema}._policy_canonical(material),'UTF8')),'hex')
  THEN RETURN jsonb_build_object('status','forbidden');END IF;
 deadline:=least(deadline,(material->>'expires_at')::numeric,extract(epoch FROM least(k.not_after,recipient.expires_at)));
 IF extract(epoch FROM clock_timestamp())>=deadline OR (material->>'expires_at')::numeric>extract(epoch FROM clock_timestamp())+65
  OR material->>'terminal_digest' IS DISTINCT FROM encode(sha256(convert_to('eai:receipt-terminal:v1:'||{schema}._policy_canonical(term),'UTF8')),'hex')
  OR term->>'result_digest' IS DISTINCT FROM encode(sha256(convert_to('reimbursement:operation-result:v1:'||{schema}._policy_canonical(term->'result'),'UTF8')),'hex')
  THEN RETURN jsonb_build_object('status','invalid');END IF;
 target:=CASE WHEN term->>'state'='committed' THEN 'committed' ELSE 'not_committed' END;
 IF target='committed' THEN
  IF (term->'result'->>'version')::bigint<>(CASE WHEN a.kind='claim.submit' THEN 1 ELSE (a.payload->'object'->>'version')::bigint+1 END)
   OR (a.kind<>'claim.submit' AND term->'result'->>'ref' IS DISTINCT FROM a.payload->'object'->>'id') OR term->'result'->>'ref' IS NULL
   THEN RETURN jsonb_build_object('status','invalid');END IF;
 ELSE
  IF term->'result' IS DISTINCT FROM jsonb_build_object('ref',NULL,'version',NULL,'code','sealed_no_commit') THEN RETURN jsonb_build_object('status','invalid');END IF;
 END IF;
 SELECT * INTO batch FROM {schema}.receipt_pin_batches WHERE tenant_id=a.tenant_id AND operation_id=a.id FOR UPDATE;
 IF FOUND AND a.kind<>'claim.decide' THEN
  IF payload->'receipt_settlement'->'material'->'terminal'->>'terminal_digest' IS DISTINCT FROM material->>'terminal_digest'
   OR payload->'receipt_settlement'->'material'->'batch'->'identity' IS DISTINCT FROM identity THEN RETURN jsonb_build_object('status','invalid');END IF;
  result:={schema}.receipt_pin_finalise(recipient.credential_hash,payload->'receipt_settlement');
  IF result->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'Pin settlement failed' USING ERRCODE='40001';END IF;
 ELSIF a.kind<>'claim.decide' AND target='committed' THEN RETURN jsonb_build_object('status','conflict');END IF;
 INSERT INTO {schema}.flow_results(tenant_id,operation_id,terminal,terminal_digest,authority_expires_at) VALUES(a.tenant_id,a.id,term,material->>'terminal_digest',to_timestamp(deadline::double precision));
 UPDATE {schema}.actions SET state=target,result_ref=(term->'result'->>'ref')::uuid,result_version=(term->'result'->>'version')::bigint,result_code=term->'result'->>'code',updated_at=clock_timestamp()
  WHERE tenant_id=a.tenant_id AND id=a.id;
 IF a.draft_id IS NOT NULL THEN UPDATE {schema}.drafts SET state=target,updated_at=clock_timestamp() WHERE tenant_id=a.tenant_id AND id=a.draft_id AND state='frozen';END IF;
 result:={schema}.jobs_call('complete',jsonb_build_object('credential_hash',payload->>'credential_hash','id',payload->>'id','generation',payload->'generation'));
 IF result->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'Lease expired during settlement' USING ERRCODE='40001';END IF;
 IF extract(epoch FROM clock_timestamp())>=deadline THEN RAISE EXCEPTION 'Flow proof expired' USING ERRCODE='40001';END IF;
 RETURN jsonb_build_object('status','ok','state',target,'result',term->'result','expires_at',deadline);
EXCEPTION WHEN data_exception OR check_violation THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.flow_call(text,jsonb) FROM PUBLIC;GRANT EXECUTE ON FUNCTION {schema}.flow_call(text,jsonb) TO {access_role};

-- Preserve existing private boundaries and grant only the draft capability.
CREATE OR REPLACE FUNCTION {schema}._runtime_role_is_valid(candidate oid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles r
        JOIN {schema}.runtime_role_registry b ON b.role_oid=r.oid AND b.role_name=r.rolname
        CROSS JOIN {schema}.runtime_access_role_registry binding
        JOIN pg_catalog.pg_roles a ON a.oid=binding.role_oid AND a.rolname=binding.role_name
        WHERE r.oid=candidate AND r.rolcanlogin
          AND NOT (r.rolsuper OR r.rolbypassrls OR r.rolcreatedb OR r.rolcreaterole
                   OR r.rolreplication OR r.rolinherit)
          AND a.oid='{access_role}'::regrole AND NOT a.rolcanlogin
          AND NOT (a.rolsuper OR a.rolbypassrls OR a.rolcreatedb OR a.rolcreaterole OR a.rolreplication OR a.rolinherit)
          AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members am WHERE am.member=a.oid)
          AND (SELECT count(*) FROM pg_catalog.pg_auth_members am WHERE am.member=r.oid)=1
          AND EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members am WHERE am.member=r.oid
                      AND am.roleid=a.oid AND am.inherit_option AND NOT am.set_option AND NOT am.admin_option)
          AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class c WHERE c.relowner IN (r.oid,a.oid))
          AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspowner IN (r.oid,a.oid))
          AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_database d WHERE d.datdba IN (r.oid,a.oid))
          AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_proc f WHERE f.proowner IN (r.oid,a.oid))
          AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_type t WHERE t.typowner IN (r.oid,a.oid))
          AND NOT has_database_privilege(r.oid,current_database(),'CREATE')
          AND NOT has_database_privilege(r.oid,current_database(),'CONNECT WITH GRANT OPTION')
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_namespace n
              WHERE n.nspname !~ '^pg_' AND has_schema_privilege(r.oid,n.oid,'CREATE')
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('principals','tenants','memberships','membership_roles','action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND has_any_column_privilege(r.oid,c.oid,'SELECT')
          )
          AND has_schema_privilege(r.oid, (SELECT relnamespace FROM pg_catalog.pg_class
              WHERE oid='{schema}.runtime_role_registry'::regclass), 'USAGE')
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('principals','tenants','memberships','membership_roles','tenant_configs','timezone_catalog')
                AND NOT has_table_privilege(r.oid,c.oid,'SELECT')
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c,
                  LATERAL pg_catalog.aclexplode(c.relacl) acl
              WHERE c.relnamespace=(SELECT relnamespace FROM pg_catalog.pg_class
                    WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND acl.grantee IN (r.oid,a.oid,0) AND acl.is_grantable
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_attribute att
              JOIN pg_catalog.pg_class c ON c.oid=att.attrelid,
                  LATERAL pg_catalog.aclexplode(att.attacl) acl
              WHERE c.relnamespace=(SELECT relnamespace FROM pg_catalog.pg_class
                    WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND acl.grantee IN (r.oid,a.oid,0) AND acl.is_grantable
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_proc f
              WHERE f.pronamespace=(SELECT relnamespace FROM pg_catalog.pg_class
                    WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND (has_function_privilege(r.oid,f.oid,'EXECUTE WITH GRANT OPTION')
                    OR (f.proname NOT IN ('context_actor_id','context_tenant_id','has_active_context','runtime_role_is_valid')
                        AND NOT (f.proname='browser_context_guard' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, expected text')
                        AND NOT (f.proname='trip_catalog_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text')
                        AND NOT (f.proname='policy_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='policy_eligibility_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='auth jsonb, request jsonb')
                        AND NOT (f.proname='receipt_pin_prepare' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='auth jsonb, request jsonb')
                        AND NOT (f.proname='receipt_file_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='flow_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='operation text, payload jsonb')
                        AND NOT (f.proname='flow_locked_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='actor jsonb, payload jsonb, policy jsonb, trip_date date, claim_id uuid')
                        AND NOT (f.proname='action_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='human_receipt_get' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='receipt uuid')
                        AND NOT (f.proname='draft_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='receipt_pin_lookup' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='credential text, tenant uuid, operation uuid')
                        AND NOT (f.proname='receipt_pin_finalise' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='credential text, settlement jsonb')
                        AND NOT (f.proname='organization_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='identity_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='operation text, payload jsonb')
                        AND NOT (f.proname='authorization_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='checkpoint_owner_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='document_sources_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='auth jsonb, refs jsonb, history boolean')
                        AND NOT (f.proname='rag_runtime_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='operation text, payload jsonb')
                        AND NOT (f.proname='rag_usage_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='payload jsonb')
                        AND NOT (f.proname='rag_owner_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='rag_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='operation text, payload jsonb')
                        AND NOT (f.proname='checkpoint_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='operation text, payload jsonb')
                        AND NOT (f.proname='conversation_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='knowledge_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='files_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='jobs_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='operation text, payload jsonb')
                        AND NOT (f.proname='delegation_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='operation text, payload jsonb')
                        AND has_function_privilege(r.oid,f.oid,'EXECUTE')))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_sequence seq JOIN pg_catalog.pg_class c ON c.oid=seq.seqrelid
              WHERE c.relnamespace=(SELECT relnamespace FROM pg_catalog.pg_class
                    WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND has_sequence_privilege(r.oid,seq.seqrelid,'USAGE,SELECT,UPDATE')
          )
          AND NOT has_schema_privilege(r.oid,(SELECT relnamespace FROM pg_catalog.pg_class
              WHERE oid='{schema}.runtime_role_registry'::regclass),'USAGE WITH GRANT OPTION')
    );
$$;



