-- ACT-01 durable human intent. No dispatch worker or remote effect runs here.
CREATE TABLE {schema}.actions (
 tenant_id uuid NOT NULL,id uuid NOT NULL UNIQUE,actor_id uuid NOT NULL,membership_id uuid NOT NULL,session_id uuid NOT NULL,
 access_revision bigint NOT NULL CHECK(access_revision>0),kind text NOT NULL CHECK(kind IN ('claim.submit','claim.information','claim.decide')),
 key uuid NOT NULL,request_identity_hash text NOT NULL CHECK(request_identity_hash ~ '^[a-f0-9]{{64}}$'),
 draft_id uuid,draft_version bigint,material_hash text,claim_ref uuid,claim_version bigint,
 human_receipt_id uuid NOT NULL UNIQUE,payload jsonb NOT NULL,canonical_hash text NOT NULL CHECK(canonical_hash ~ '^[a-f0-9]{{64}}$'),
 state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','sending','unknown','committed','not_committed')),
 confirmation_expires_at timestamptz NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,actor_id,key),UNIQUE(tenant_id,human_receipt_id),
 FOREIGN KEY(tenant_id,membership_id,actor_id) REFERENCES {schema}.memberships(tenant_id,id,principal_id),
 FOREIGN KEY(session_id) REFERENCES {schema}.sessions(id),
 FOREIGN KEY(tenant_id,draft_id,draft_version) REFERENCES {schema}.draft_versions(tenant_id,draft_id,version),
 CHECK((kind='claim.decide' AND draft_id IS NULL AND draft_version IS NULL AND material_hash IS NULL AND claim_ref IS NOT NULL AND claim_version IS NOT NULL AND claim_version>0)
  OR (kind IN ('claim.submit','claim.information') AND draft_id IS NOT NULL AND draft_version IS NOT NULL AND draft_version>0 AND material_hash IS NOT NULL AND material_hash ~ '^[a-f0-9]{{64}}$')),
 CHECK((kind='claim.submit' AND claim_ref IS NULL AND claim_version IS NULL) OR (kind IN ('claim.information','claim.decide') AND claim_ref IS NOT NULL AND claim_version IS NOT NULL AND claim_version>0)),
 CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=131072)
);
CREATE UNIQUE INDEX action_one_draft_version ON {schema}.actions(tenant_id,draft_id,draft_version) WHERE draft_id IS NOT NULL;
CREATE UNIQUE INDEX action_one_claim_decision_version ON {schema}.actions(tenant_id,claim_ref,claim_version) WHERE kind='claim.decide' AND state<>'not_committed';
CREATE TABLE {schema}.human_action_receipts (
 id uuid PRIMARY KEY,tenant_id uuid NOT NULL,actor_id uuid NOT NULL,session_id uuid NOT NULL,operation_id uuid NOT NULL,
 action text NOT NULL CHECK(action IN ('claim.submit','claim.information','claim.decide')),object_kind text NOT NULL CHECK(object_kind IN ('draft','claim')),
 object_id uuid NOT NULL,object_version bigint NOT NULL CHECK(object_version>0),request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{{64}}$'),
 human_action_time bigint NOT NULL CHECK(human_action_time>0),revoked boolean NOT NULL DEFAULT false,
 UNIQUE(tenant_id,id),UNIQUE(tenant_id,operation_id),FOREIGN KEY(tenant_id,operation_id) REFERENCES {schema}.actions(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(session_id) REFERENCES {schema}.sessions(id),
 CHECK((action='claim.submit' AND object_kind='draft') OR (action IN ('claim.information','claim.decide') AND object_kind='claim'))
);
ALTER TABLE {schema}.actions ADD FOREIGN KEY(tenant_id,human_receipt_id) REFERENCES {schema}.human_action_receipts(tenant_id,id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE {schema}.consents (
 tenant_id uuid NOT NULL,draft_id uuid NOT NULL,draft_version bigint NOT NULL,operation_id uuid NOT NULL UNIQUE,
 actor_id uuid NOT NULL,session_id uuid NOT NULL,material_hash text NOT NULL CHECK(material_hash ~ '^[a-f0-9]{{64}}$'),
 human_receipt_id uuid NOT NULL UNIQUE,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,draft_id,draft_version),FOREIGN KEY(tenant_id,draft_id,draft_version) REFERENCES {schema}.draft_versions(tenant_id,draft_id,version),
 FOREIGN KEY(tenant_id,operation_id) REFERENCES {schema}.actions(tenant_id,id),
 FOREIGN KEY(tenant_id,human_receipt_id) REFERENCES {schema}.human_action_receipts(tenant_id,id)
);
CREATE TABLE {schema}.action_audit (
 tenant_id uuid NOT NULL,operation_id uuid NOT NULL,actor_id uuid NOT NULL,kind text NOT NULL,canonical_hash text NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(tenant_id,operation_id),
 FOREIGN KEY(tenant_id,operation_id) REFERENCES {schema}.actions(tenant_id,id)
);
ALTER TABLE {schema}.actions ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.actions FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.human_action_receipts ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.human_action_receipts FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.consents ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.consents FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.action_audit ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.action_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY maintenance ON {schema}.actions TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.human_action_receipts TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.consents TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.action_audit TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.actions,{schema}.human_action_receipts,{schema}.consents,{schema}.action_audit FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._action_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF TG_OP='DELETE' OR TG_TABLE_NAME IN ('consents','action_audit') THEN RAISE EXCEPTION 'Immutable action history' USING ERRCODE='23514';END IF;
 IF TG_TABLE_NAME='human_action_receipts' THEN
  IF (to_jsonb(NEW)-'revoked') IS DISTINCT FROM (to_jsonb(OLD)-'revoked') OR OLD.revoked THEN RAISE EXCEPTION 'Immutable human receipt' USING ERRCODE='23514';END IF;
  RETURN NEW;
 END IF;
 IF (to_jsonb(NEW)-'state'-'updated_at') IS DISTINCT FROM (to_jsonb(OLD)-'state'-'updated_at') OR OLD.state IN ('committed','not_committed')
  OR NOT ((OLD.state IN ('queued','unknown') AND NEW.state='sending') OR (OLD.state='sending' AND NEW.state IN ('unknown','committed','not_committed')))
  THEN RAISE EXCEPTION 'Invalid action transition' USING ERRCODE='23514';END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER action_guard BEFORE UPDATE OR DELETE ON {schema}.actions FOR EACH ROW EXECUTE FUNCTION {schema}._action_guard();
CREATE TRIGGER receipt_guard BEFORE UPDATE OR DELETE ON {schema}.human_action_receipts FOR EACH ROW EXECUTE FUNCTION {schema}._action_guard();
CREATE TRIGGER consent_guard BEFORE UPDATE OR DELETE ON {schema}.consents FOR EACH ROW EXECUTE FUNCTION {schema}._action_guard();
CREATE TRIGGER action_audit_guard BEFORE UPDATE OR DELETE ON {schema}.action_audit FOR EACH ROW EXECUTE FUNCTION {schema}._action_guard();
CREATE FUNCTION {schema}._action_expiry() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN IF clock_timestamp()>=NEW.confirmation_expires_at THEN RAISE EXCEPTION 'Action authority expired' USING ERRCODE='40001';END IF;RETURN NULL;END $$;
CREATE CONSTRAINT TRIGGER action_expiry AFTER INSERT ON {schema}.actions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {schema}._action_expiry();
REVOKE ALL ON FUNCTION {schema}._action_guard(),{schema}._action_expiry() FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._action_json(a {schema}.actions) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('operation_id',a.id,'kind',a.kind,'state',a.state,'draft_id',a.draft_id,'draft_version',a.draft_version,
  'claim_id',a.claim_ref,'claim_version',a.claim_version,'material_hash',a.material_hash,'request_hash',a.canonical_hash,'human_receipt_id',a.human_receipt_id);
$$;
REVOKE ALL ON FUNCTION {schema}._action_json({schema}.actions) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._decision_source(context jsonb,envelope jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE material jsonb:=envelope->'material';k {schema}.receipt_receiver_keys%ROWTYPE;c {schema}.internal_caller_credentials%ROWTYPE;expiry numeric;
BEGIN
 IF jsonb_typeof(envelope) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(envelope))<>4 OR NOT envelope ?& ARRAY['credential_hash','key_id','material','proof']
  OR envelope->>'proof' !~ '^[a-f0-9]{{64}}$' OR jsonb_typeof(material) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(material))<>5
  OR NOT material ?& ARRAY['schema_version','context','claim','checked_at','expires_at'] OR material->'schema_version' IS DISTINCT FROM '1'::jsonb
  OR material->'context' IS DISTINCT FROM context OR jsonb_typeof(material->'claim') IS DISTINCT FROM 'object'
  OR jsonb_typeof(material->'checked_at') IS DISTINCT FROM 'number' OR material->>'checked_at' !~ '^[1-9][0-9]*$'
  OR jsonb_typeof(material->'expires_at') IS DISTINCT FROM 'number' OR material->>'expires_at' !~ '^[1-9][0-9]*$' THEN RETURN NULL;END IF;
 expiry:=(material->>'expires_at')::numeric;
 IF (material->>'checked_at')::numeric>extract(epoch FROM clock_timestamp())+5 OR expiry<=(material->>'checked_at')::numeric OR expiry>(material->>'checked_at')::numeric+60 OR expiry<=extract(epoch FROM clock_timestamp()) THEN RETURN NULL;END IF;
 SELECT * INTO c FROM {schema}.internal_caller_credentials WHERE credential_hash=envelope->>'credential_hash' FOR SHARE;
 IF NOT FOUND OR NOT c.active OR c.audience<>'reference-reimbursement' OR c.delegation_caller<>'platform-dispatcher' OR NOT c.allowed_actions ? 'claim.read' OR clock_timestamp()>=c.expires_at THEN RETURN NULL;END IF;
 SELECT * INTO k FROM {schema}.receipt_receiver_keys WHERE credential_hash=c.credential_hash AND key_id=envelope->>'key_id' FOR SHARE;
 IF NOT FOUND OR NOT k.active OR clock_timestamp()<k.not_before OR clock_timestamp()>=k.not_after OR expiry>extract(epoch FROM least(k.not_after,c.expires_at))
  OR envelope->>'proof' IS DISTINCT FROM encode({schema}._policy_hmac(k.key_material,convert_to('eai:decision-source:v1:'||{schema}._policy_canonical(material),'UTF8')),'hex') THEN RETURN NULL;END IF;
 RETURN material;
EXCEPTION WHEN data_exception THEN RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION {schema}._decision_source(jsonb,jsonb) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.human_receipt_get(receipt uuid) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE r {schema}.human_action_receipts%ROWTYPE;a {schema}.actions%ROWTYPE;s {schema}.sessions%ROWTYPE;m {schema}.memberships%ROWTYPE;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Receipt capability rejected';END IF;
 SELECT * INTO r FROM {schema}.human_action_receipts WHERE id=receipt FOR SHARE;
 IF NOT FOUND OR r.revoked THEN RETURN NULL;END IF;
 SELECT * INTO a FROM {schema}.actions WHERE tenant_id=r.tenant_id AND id=r.operation_id FOR SHARE;
 IF NOT FOUND OR a.human_receipt_id<>r.id THEN RETURN NULL;END IF;
 SELECT * INTO s FROM {schema}.sessions WHERE id=r.session_id FOR SHARE;
 IF NOT FOUND OR s.revoked_at IS NOT NULL OR s.principal_id<>r.actor_id OR s.active_tenant_id IS DISTINCT FROM r.tenant_id OR s.membership_revision<>a.access_revision
  OR clock_timestamp()>=least(s.expires_at,s.last_seen_at+interval '1800 seconds') THEN RETURN NULL;END IF;
 SELECT * INTO m FROM {schema}.memberships WHERE tenant_id=r.tenant_id AND principal_id=r.actor_id AND id=a.membership_id FOR SHARE;
 IF NOT FOUND OR NOT m.active OR m.access_revision<>a.access_revision OR NOT EXISTS(SELECT 1 FROM {schema}.tenants WHERE id=r.tenant_id AND active)
  OR NOT EXISTS(SELECT 1 FROM {schema}.principals WHERE id=r.actor_id AND active) THEN RETURN NULL;END IF;
 RETURN to_jsonb(r)-'tenant_id'||jsonb_build_object('tenant_id',r.tenant_id);
END $$;
REVOKE ALL ON FUNCTION {schema}.human_receipt_get(uuid) FROM PUBLIC;GRANT EXECUTE ON FUNCTION {schema}.human_receipt_get(uuid) TO {access_role};

CREATE OR REPLACE FUNCTION {schema}._receipt_human_current(claims jsonb,payload jsonb) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE r jsonb;a {schema}.actions%ROWTYPE;
BEGIN
 r:={schema}.human_receipt_get((claims->>'human_receipt_id')::uuid);
 IF r IS NULL THEN RETURN false;END IF;
 SELECT * INTO a FROM {schema}.actions WHERE tenant_id=(r->>'tenant_id')::uuid AND id=(r->>'operation_id')::uuid FOR SHARE;
 RETURN a.id IS NOT NULL AND a.payload=payload AND a.canonical_hash=claims->>'request_hash'
  AND a.membership_id::text=claims->>'membership_id' AND a.access_revision::text=claims->>'access_revision'
  AND r->>'actor_id'=claims->>'sub' AND r->>'session_id'=claims->>'session_id' AND r->>'tenant_id'=claims->>'tenant_id'
  AND r->>'operation_id'=claims->>'operation_id' AND r->>'action'=claims->>'action' AND r->>'object_kind'=claims->>'object_kind'
  AND r->>'object_id'=claims->>'object_id' AND r->'object_version'=claims->'object_version'
  AND (r->>'human_action_time')::bigint<=(claims->>'iat')::bigint;
EXCEPTION WHEN data_exception THEN RETURN false;
END $$;
REVOKE ALL ON FUNCTION {schema}._receipt_human_current(jsonb,jsonb) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.action_call(token text,operation text,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
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
  SELECT * INTO a FROM {schema}.actions WHERE tenant_id=t AND id=(action_call.payload->>'id')::uuid AND actor_id=person FOR SHARE;
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
REVOKE ALL ON FUNCTION {schema}.action_call(text,text,jsonb) FROM PUBLIC;GRANT EXECUTE ON FUNCTION {schema}.action_call(text,text,jsonb) TO {access_role};

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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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



