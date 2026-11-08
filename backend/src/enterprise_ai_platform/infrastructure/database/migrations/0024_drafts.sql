-- DRAFT-01: editable versions and deterministic preview; no consent/outbox.
CREATE TABLE {schema}.drafts (
 tenant_id uuid NOT NULL,id uuid NOT NULL,owner_principal_id uuid NOT NULL,
 conversation_id uuid,kind text NOT NULL CHECK(kind IN ('submission','information_update')),
 trip_ref uuid NOT NULL,claim_ref uuid,claim_version bigint,claim_revision bigint,locked_policy jsonb,
 state text NOT NULL DEFAULT 'editable' CHECK(state IN ('editable','frozen','committed','not_committed')),
 current_version bigint NOT NULL DEFAULT 1 CHECK(current_version>0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,id),FOREIGN KEY(tenant_id,owner_principal_id) REFERENCES {schema}.memberships(tenant_id,principal_id),
 FOREIGN KEY(tenant_id,conversation_id) REFERENCES {schema}.conversations(tenant_id,id),
 CHECK((kind='submission' AND claim_ref IS NULL AND claim_version IS NULL AND claim_revision IS NULL AND locked_policy IS NULL)
   OR (kind='information_update' AND claim_ref IS NOT NULL AND claim_version IS NOT NULL AND claim_revision IS NOT NULL
    AND claim_version>0 AND claim_revision>0 AND locked_policy IS NOT NULL AND jsonb_typeof(locked_policy)='object'))
);
CREATE TABLE {schema}.draft_versions (
 tenant_id uuid NOT NULL,draft_id uuid NOT NULL,version bigint NOT NULL CHECK(version>0),
 content jsonb NOT NULL CHECK(jsonb_typeof(content)='object' AND octet_length(content::text)<=65536),
 content_hash text NOT NULL CHECK(content_hash ~ '^[a-f0-9]{{64}}$'),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,draft_id,version),FOREIGN KEY(tenant_id,draft_id) REFERENCES {schema}.drafts(tenant_id,id) DEFERRABLE INITIALLY DEFERRED
);
ALTER TABLE {schema}.drafts ADD FOREIGN KEY(tenant_id,id,current_version) REFERENCES {schema}.draft_versions(tenant_id,draft_id,version) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE {schema}.draft_requests (
 tenant_id uuid NOT NULL,actor_id uuid NOT NULL,key uuid NOT NULL,operation text NOT NULL CHECK(operation IN ('create','replace')),
 request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{{64}}$'),draft_id uuid NOT NULL,version bigint NOT NULL,
 PRIMARY KEY(tenant_id,actor_id,key),FOREIGN KEY(tenant_id,draft_id,version) REFERENCES {schema}.draft_versions(tenant_id,draft_id,version)
);
CREATE TABLE {schema}.draft_previews (
 tenant_id uuid NOT NULL,draft_id uuid NOT NULL,version bigint NOT NULL,material_hash text NOT NULL CHECK(material_hash ~ '^[a-f0-9]{{64}}$'),
 material jsonb NOT NULL CHECK(jsonb_typeof(material)='object' AND octet_length(material::text)<=131072),
 source_digest text NOT NULL CHECK(source_digest ~ '^[a-f0-9]{{64}}$'),authority_expires_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,draft_id,version,material_hash),FOREIGN KEY(tenant_id,draft_id,version) REFERENCES {schema}.draft_versions(tenant_id,draft_id,version)
);
ALTER TABLE {schema}.drafts ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.drafts FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.draft_versions ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.draft_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.draft_requests ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.draft_requests FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.draft_previews ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.draft_previews FORCE ROW LEVEL SECURITY;
CREATE POLICY maintenance ON {schema}.drafts TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.draft_versions TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.draft_requests TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.draft_previews TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.drafts,{schema}.draft_versions,{schema}.draft_requests,{schema}.draft_previews FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._draft_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Draft history is immutable' USING ERRCODE='23514';END IF;
 IF TG_TABLE_NAME IN ('draft_versions','draft_requests') THEN RAISE EXCEPTION 'Draft history is immutable' USING ERRCODE='23514';END IF;
 IF TG_TABLE_NAME='draft_previews' THEN
  IF ROW(NEW.tenant_id,NEW.draft_id,NEW.version,NEW.material_hash,NEW.material,NEW.created_at)
   IS DISTINCT FROM ROW(OLD.tenant_id,OLD.draft_id,OLD.version,OLD.material_hash,OLD.material,OLD.created_at)
   THEN RAISE EXCEPTION 'Preview material is immutable' USING ERRCODE='23514';END IF;
  RETURN NEW;
 END IF;
 IF ROW(NEW.tenant_id,NEW.id,NEW.owner_principal_id,NEW.conversation_id,NEW.kind,NEW.trip_ref,NEW.claim_ref,NEW.claim_version,NEW.claim_revision,NEW.locked_policy,NEW.created_at)
  IS DISTINCT FROM ROW(OLD.tenant_id,OLD.id,OLD.owner_principal_id,OLD.conversation_id,OLD.kind,OLD.trip_ref,OLD.claim_ref,OLD.claim_version,OLD.claim_revision,OLD.locked_policy,OLD.created_at)
  OR NOT ((OLD.state='editable' AND NEW.state='editable' AND NEW.current_version=OLD.current_version+1)
   OR (OLD.state='editable' AND NEW.state='frozen' AND NEW.current_version=OLD.current_version)
   OR (OLD.state='frozen' AND NEW.state IN ('committed','not_committed') AND NEW.current_version=OLD.current_version))
  THEN RAISE EXCEPTION 'Invalid draft transition' USING ERRCODE='23514';END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER draft_header_guard BEFORE UPDATE OR DELETE ON {schema}.drafts FOR EACH ROW EXECUTE FUNCTION {schema}._draft_guard();
CREATE TRIGGER draft_version_guard BEFORE UPDATE OR DELETE ON {schema}.draft_versions FOR EACH ROW EXECUTE FUNCTION {schema}._draft_guard();
CREATE TRIGGER draft_request_guard BEFORE UPDATE OR DELETE ON {schema}.draft_requests FOR EACH ROW EXECUTE FUNCTION {schema}._draft_guard();
CREATE TRIGGER draft_preview_guard BEFORE UPDATE OR DELETE ON {schema}.draft_previews FOR EACH ROW EXECUTE FUNCTION {schema}._draft_guard();
CREATE FUNCTION {schema}._draft_preview_expiry() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF clock_timestamp()>=NEW.authority_expires_at THEN RAISE EXCEPTION 'Draft source expired' USING ERRCODE='40001';END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER draft_preview_expiry AFTER INSERT OR UPDATE ON {schema}.draft_previews DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {schema}._draft_preview_expiry();
REVOKE ALL ON FUNCTION {schema}._draft_guard(),{schema}._draft_preview_expiry() FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._draft_json(d {schema}.drafts) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('id',d.id,'kind',d.kind,'trip_id',d.trip_ref,'claim_id',d.claim_ref,'claim_version',d.claim_version,'claim_revision',d.claim_revision,
  'conversation_id',d.conversation_id,'state',d.state,'version',d.current_version,'content',v.content,'content_hash',v.content_hash)
 FROM {schema}.draft_versions v WHERE v.tenant_id=d.tenant_id AND v.draft_id=d.id AND v.version=d.current_version;
$$;
REVOKE ALL ON FUNCTION {schema}._draft_json({schema}.drafts) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._draft_source(context jsonb,envelope jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE material jsonb:=envelope->'material';k {schema}.receipt_receiver_keys%ROWTYPE;c {schema}.internal_caller_credentials%ROWTYPE;expiry numeric;
BEGIN
 IF jsonb_typeof(envelope) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(envelope))<>4 OR NOT envelope ?& ARRAY['credential_hash','key_id','material','proof']
  OR envelope->>'proof' !~ '^[a-f0-9]{{64}}$' OR jsonb_typeof(material) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(material))<>6
  OR NOT material ?& ARRAY['schema_version','context','trip','claim','checked_at','expires_at'] OR material->'schema_version' IS DISTINCT FROM '1'::jsonb
  OR material->'context' IS DISTINCT FROM context OR jsonb_typeof(material->'trip') IS DISTINCT FROM 'object'
  OR material->'trip'->>'state' IS DISTINCT FROM 'completed' OR jsonb_typeof(material->'trip'->'items') IS DISTINCT FROM 'array'
  OR jsonb_typeof(material->'checked_at') IS DISTINCT FROM 'number' OR material->>'checked_at' !~ '^[1-9][0-9]*$'
  OR jsonb_typeof(material->'expires_at') IS DISTINCT FROM 'number' OR material->>'expires_at' !~ '^[1-9][0-9]*$'
  THEN RETURN NULL;END IF;
 expiry:=(material->>'expires_at')::numeric;
 IF (material->>'checked_at')::numeric>extract(epoch FROM clock_timestamp())+5 OR expiry<=(material->>'checked_at')::numeric OR expiry>(material->>'checked_at')::numeric+60 OR expiry<=extract(epoch FROM clock_timestamp()) THEN RETURN NULL;END IF;
 SELECT * INTO c FROM {schema}.internal_caller_credentials WHERE credential_hash=envelope->>'credential_hash' FOR SHARE;
 IF NOT FOUND OR NOT c.active OR c.audience<>'reference-reimbursement' OR c.delegation_caller<>'platform-dispatcher'
  OR NOT c.allowed_actions ? (CASE WHEN material->'claim'='null'::jsonb THEN 'trip.read' ELSE 'claim.read' END)
  OR clock_timestamp()>=c.expires_at THEN RETURN NULL;END IF;
 SELECT * INTO k FROM {schema}.receipt_receiver_keys WHERE credential_hash=c.credential_hash AND key_id=envelope->>'key_id' FOR SHARE;
 IF NOT FOUND OR NOT k.active OR clock_timestamp()<k.not_before OR clock_timestamp()>=k.not_after
  OR expiry>extract(epoch FROM least(k.not_after,c.expires_at))
  OR envelope->>'proof' IS DISTINCT FROM encode({schema}._policy_hmac(k.key_material,convert_to('eai:draft-source:v1:'||{schema}._policy_canonical(material),'UTF8')),'hex') THEN RETURN NULL;END IF;
 RETURN material;
EXCEPTION WHEN data_exception THEN RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION {schema}._draft_source(jsonb,jsonb) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._draft_content_valid(value jsonb) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
DECLARE item jsonb;r jsonb;ref text;ids jsonb;
BEGIN
 IF jsonb_typeof(value) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(value))<>3 OR NOT value ?& ARRAY['currency','items','receipts']
  OR value->>'currency' IS NULL OR value->>'currency' NOT IN ('IDR','SGD','USD') OR octet_length(value::text)>65536
  OR jsonb_typeof(value->'items') IS DISTINCT FROM 'array' OR jsonb_array_length(value->'items')>100
  OR jsonb_typeof(value->'receipts') IS DISTINCT FROM 'array' OR jsonb_array_length(value->'receipts')>10 THEN RETURN false;END IF;
 FOR item IN SELECT v FROM jsonb_array_elements(value->'items') e(v) LOOP
  IF jsonb_typeof(item) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(item))<>4 OR NOT item ?& ARRAY['id','trip_item_id','amount_minor','receipt_ids']
   OR jsonb_typeof(item->'id') IS DISTINCT FROM 'string' OR jsonb_typeof(item->'trip_item_id') IS DISTINCT FROM 'string'
   OR (item->>'id')::uuid::text IS DISTINCT FROM item->>'id' OR (item->>'trip_item_id')::uuid::text IS DISTINCT FROM item->>'trip_item_id'
   OR (item->>'id')::uuid='00000000-0000-0000-0000-000000000000'::uuid OR (item->>'trip_item_id')::uuid='00000000-0000-0000-0000-000000000000'::uuid
   OR jsonb_typeof(item->'amount_minor') IS DISTINCT FROM 'number' OR item->>'amount_minor' !~ '^[1-9][0-9]*$' OR (item->>'amount_minor')::numeric>9223372036854775807
   OR jsonb_typeof(item->'receipt_ids') IS DISTINCT FROM 'array' OR jsonb_array_length(item->'receipt_ids')>10 THEN RETURN false;END IF;
  FOR ref IN SELECT jsonb_array_elements_text(item->'receipt_ids') LOOP
   IF ref IS NULL OR ref::uuid::text IS DISTINCT FROM ref OR ref::uuid='00000000-0000-0000-0000-000000000000'::uuid THEN RETURN false;END IF;
  END LOOP;
  IF jsonb_array_length(item->'receipt_ids')<>(SELECT count(DISTINCT v) FROM jsonb_array_elements_text(item->'receipt_ids') e(v)) THEN RETURN false;END IF;
 END LOOP;
 IF jsonb_array_length(value->'items')<>(SELECT count(DISTINCT v->>'id') FROM jsonb_array_elements(value->'items') e(v))
  OR jsonb_array_length(value->'items')<>(SELECT count(DISTINCT v->>'trip_item_id') FROM jsonb_array_elements(value->'items') e(v))
  OR (SELECT coalesce(sum((v->>'amount_minor')::numeric),0) FROM jsonb_array_elements(value->'items') e(v))>9223372036854775807 THEN RETURN false;END IF;
 FOR r IN SELECT v FROM jsonb_array_elements(value->'receipts') e(v) LOOP
  IF jsonb_typeof(r) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(r))<>2 OR NOT r ?& ARRAY['id','version']
   OR jsonb_typeof(r->'id') IS DISTINCT FROM 'string' OR (r->>'id')::uuid::text IS DISTINCT FROM r->>'id'
   OR (r->>'id')::uuid='00000000-0000-0000-0000-000000000000'::uuid OR jsonb_typeof(r->'version') IS DISTINCT FROM 'number'
   OR r->>'version' !~ '^[1-9][0-9]*$' OR (r->>'version')::numeric>9223372036854775807 THEN RETURN false;END IF;
 END LOOP;
 IF jsonb_array_length(value->'receipts')<>(SELECT count(DISTINCT v->>'id') FROM jsonb_array_elements(value->'receipts') e(v)) THEN RETURN false;END IF;
 SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb) INTO ids FROM (SELECT DISTINCT jsonb_array_elements_text(v->'receipt_ids') id FROM jsonb_array_elements(value->'items') e(v)) u;
 RETURN ids=(SELECT coalesce(jsonb_agg(v->>'id' ORDER BY v->>'id'),'[]'::jsonb) FROM jsonb_array_elements(value->'receipts') e(v));
EXCEPTION WHEN data_exception THEN RETURN false;
END $$;
REVOKE ALL ON FUNCTION {schema}._draft_content_valid(jsonb) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.draft_call(token text,operation text,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
#variable_conflict use_column
<<draft>>
DECLARE auth jsonb;identity jsonb;context jsonb;source jsonb;locked jsonb;content jsonb;source_item jsonb;category jsonb;entry jsonb;
 d {schema}.drafts%ROWTYPE;v {schema}.draft_versions%ROWTYPE;request {schema}.draft_requests%ROWTYPE;
 pkg {schema}.policy_packages%ROWTYPE;f {schema}.files%ROWTYPE;s {schema}.sessions%ROWTYPE;
 t uuid;person uuid;key uuid;id uuid;hash text;body jsonb;receipts jsonb;material jsonb;preview jsonb;day date;
 deadline numeric;total numeric;expected bigint;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Draft capability rejected';END IF;
 IF operation IS NULL OR operation NOT IN ('create','read','replace','preview') OR jsonb_typeof(payload) IS DISTINCT FROM 'object'
  OR octet_length(payload::text)>131072 THEN RETURN jsonb_build_object('status','invalid');END IF;
 auth:={schema}.authorization_call(token,'context','{{}}'::jsonb);
 IF auth->>'status' IS DISTINCT FROM 'ok' THEN RETURN auth;END IF;
 identity:=auth->'identity';t:=(identity->>'active_tenant_id')::uuid;person:=(identity->>'principal_id')::uuid;
 IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(auth->'grants') g WHERE g->>'role'='employee' AND g->>'scope_kind'='tenant' AND g->>'scope_ref'=t::text)
  THEN RETURN jsonb_build_object('status','forbidden');END IF;
 IF operation<>'read' THEN
  auth:={schema}.authorization_call(token,'finish',jsonb_build_object('csrf_hash',payload->>'csrf_hash'));
  IF auth->>'status' IS DISTINCT FROM 'ok' THEN RETURN auth;END IF;
 END IF;
 SELECT * INTO s FROM {schema}.sessions WHERE token_hash=token FOR SHARE;
 context:=jsonb_build_object('tenant_id',t,'actor_id',person,'membership_id',identity->>'active_membership_id','session_id',s.id,'access_revision',identity->'access_revision');
 deadline:=extract(epoch FROM least(s.expires_at,s.last_seen_at+interval '1800 seconds'));
 IF operation IN ('create','replace') THEN
  key:=(payload->>'key')::uuid;
  IF key IS NULL OR key='00000000-0000-0000-0000-000000000000'::uuid THEN RETURN jsonb_build_object('status','invalid');END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('draft-key:'||t::text||':'||person::text||':'||key::text,0));
  hash:=encode(sha256(convert_to('eai:draft-request:v1:'||operation||':'||{schema}._policy_canonical(payload-'source'-'csrf_hash'-(CASE WHEN operation='create' THEN 'id' ELSE '_unused' END)),'UTF8')),'hex');
  SELECT * INTO request FROM {schema}.draft_requests WHERE tenant_id=t AND actor_id=person AND draft_requests.key=draft.key;
  IF FOUND THEN
   IF request.operation<>operation OR request.request_hash<>hash THEN RETURN jsonb_build_object('status','conflict');END IF;
   SELECT * INTO d FROM {schema}.drafts WHERE tenant_id=t AND drafts.id=request.draft_id AND owner_principal_id=person FOR SHARE;
   IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
   IF extract(epoch FROM clock_timestamp())>=deadline THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
   RETURN jsonb_build_object('status','ok','draft',{schema}._draft_json(d),'context',context||jsonb_build_object('authority_expires_at',deadline),'authority_expires_at',deadline);
  END IF;
 END IF;
 IF operation='create' THEN
  IF (SELECT count(*) FROM jsonb_object_keys(payload)) NOT IN (7,8) OR NOT payload ?& ARRAY['kind','trip_id','claim_id','conversation_id','key','csrf_hash','source']
   OR payload->>'kind' IS NULL OR payload->>'kind' NOT IN ('submission','information_update')
   OR (payload->>'trip_id')::uuid IS NULL OR (payload->>'kind'='submission' AND payload->'claim_id'<>'null'::jsonb)
   OR (payload->>'kind'='information_update' AND (payload->>'claim_id')::uuid IS NULL) THEN RETURN jsonb_build_object('status','invalid');END IF;
  IF payload->'source'='null'::jsonb THEN
   RETURN jsonb_build_object('status','ok','draft',NULL,'context',context||jsonb_build_object('authority_expires_at',deadline),'authority_expires_at',deadline);
  END IF;
  source:={schema}._draft_source(context,payload->'source');
  IF source IS NULL THEN RETURN jsonb_build_object('status','unavailable');END IF;
  IF source->'trip'->>'id' IS DISTINCT FROM payload->>'trip_id' THEN RETURN jsonb_build_object('status','forbidden');END IF;
  locked:=source->'claim';
  IF payload->>'kind'='submission' THEN
   IF locked<>'null'::jsonb THEN RETURN jsonb_build_object('status','forbidden');END IF;
  ELSIF locked->>'id' IS DISTINCT FROM payload->>'claim_id' OR locked->>'owner_id' IS DISTINCT FROM person::text OR locked->>'trip_id' IS DISTINCT FROM payload->>'trip_id'
   OR locked->>'state' IS DISTINCT FROM 'needs_information' THEN RETURN jsonb_build_object('status','conflict');END IF;
  IF payload->>'conversation_id' IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {schema}.conversations c WHERE c.tenant_id=t AND c.id=(payload->>'conversation_id')::uuid AND c.owner_principal_id=person)
   THEN RETURN jsonb_build_object('status','missing');END IF;
  content:=jsonb_build_object('currency',source->'trip'->'items'->0->'currency','items','[]'::jsonb,'receipts','[]'::jsonb);
  IF NOT {schema}._draft_content_valid(content) THEN RETURN jsonb_build_object('status','invalid');END IF;
  id:=(payload->>'id')::uuid;
  IF id IS NULL OR id='00000000-0000-0000-0000-000000000000'::uuid THEN RETURN jsonb_build_object('status','invalid');END IF;
  INSERT INTO {schema}.drafts(tenant_id,id,owner_principal_id,conversation_id,kind,trip_ref,claim_ref,claim_version,claim_revision,locked_policy)
   VALUES(t,id,person,(payload->>'conversation_id')::uuid,payload->>'kind',(payload->>'trip_id')::uuid,(payload->>'claim_id')::uuid,
    (locked->>'version')::bigint,(locked->>'revision')::bigint,CASE WHEN locked='null'::jsonb THEN NULL ELSE locked->'policy' END) RETURNING * INTO d;
  INSERT INTO {schema}.draft_versions VALUES(t,id,1,content,encode(sha256(convert_to('eai:draft-content:v1:'||{schema}._policy_canonical(content),'UTF8')),'hex'),clock_timestamp());
  INSERT INTO {schema}.draft_requests VALUES(t,person,key,operation,hash,id,1);
  deadline:=least(deadline,(source->>'expires_at')::numeric);
 ELSE
  id:=(payload->>'id')::uuid;
  IF operation='read' THEN
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>1 THEN RETURN jsonb_build_object('status','invalid');END IF;
   SELECT * INTO d FROM {schema}.drafts WHERE tenant_id=t AND drafts.id=draft.id AND owner_principal_id=person FOR SHARE;
  ELSE
   SELECT * INTO d FROM {schema}.drafts WHERE tenant_id=t AND drafts.id=draft.id AND owner_principal_id=person FOR UPDATE;
  END IF;
  IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
  IF operation IN ('replace','preview') THEN
   IF jsonb_typeof(payload->'expected_version') IS DISTINCT FROM 'number' OR payload->>'expected_version' !~ '^[1-9][0-9]*$' THEN RETURN jsonb_build_object('status','invalid');END IF;
   expected:=(payload->>'expected_version')::bigint;
   IF d.state<>'editable' OR d.current_version<>expected THEN RETURN jsonb_build_object('status','conflict');END IF;
  END IF;
  IF operation='replace' THEN
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>5 OR NOT payload ?& ARRAY['id','expected_version','key','content','csrf_hash'] OR NOT {schema}._draft_content_valid(payload->'content')
    THEN RETURN jsonb_build_object('status','invalid');END IF;
   content:=payload->'content';
   INSERT INTO {schema}.draft_versions VALUES(t,d.id,d.current_version+1,content,encode(sha256(convert_to('eai:draft-content:v1:'||{schema}._policy_canonical(content),'UTF8')),'hex'),clock_timestamp());
   UPDATE {schema}.drafts SET current_version=current_version+1,updated_at=clock_timestamp() WHERE tenant_id=t AND drafts.id=d.id RETURNING * INTO d;
   INSERT INTO {schema}.draft_requests VALUES(t,person,key,operation,hash,d.id,d.current_version);
  ELSIF operation='preview' THEN
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>4 OR NOT payload ?& ARRAY['id','expected_version','source','csrf_hash'] THEN RETURN jsonb_build_object('status','invalid');END IF;
   source:={schema}._draft_source(context,payload->'source');
   IF source IS NULL THEN RETURN jsonb_build_object('status','unavailable');END IF;
   IF source->'trip'->>'id' IS DISTINCT FROM d.trip_ref::text THEN RETURN jsonb_build_object('status','forbidden');END IF;
   IF d.kind='information_update' AND (source->'claim'->>'id' IS DISTINCT FROM d.claim_ref::text OR source->'claim'->>'owner_id' IS DISTINCT FROM person::text
    OR source->'claim'->>'state' IS DISTINCT FROM 'needs_information' OR source->'claim'->>'version' IS DISTINCT FROM d.claim_version::text
    OR source->'claim'->>'revision' IS DISTINCT FROM d.claim_revision::text OR source->'claim'->'policy' IS DISTINCT FROM d.locked_policy)
    THEN RETURN jsonb_build_object('status','conflict');END IF;
   day:=(source->'trip'->>'end_date')::date;
   PERFORM tenant_id FROM {schema}.policy_coordination WHERE tenant_id=t FOR SHARE;
   IF d.kind='submission' THEN
    SELECT p.* INTO pkg FROM {schema}.policy_packages p JOIN {schema}.policy_windows w ON w.tenant_id=p.tenant_id AND w.package_id=p.id AND w.package_version=p.package_version
     WHERE p.tenant_id=t AND day>=w.starts_on AND (w.ends_on IS NULL OR day<w.ends_on) FOR SHARE OF p,w;
   ELSE
    SELECT * INTO pkg FROM {schema}.policy_packages WHERE tenant_id=t AND policy_packages.id=(d.locked_policy->>'id')::uuid AND package_version=(d.locked_policy->>'version')::bigint FOR SHARE;
   END IF;
   IF pkg.id IS NULL OR pkg.publication_state<>'active' OR (d.kind='submission' AND NOT pkg.allowed_for_new) OR (d.kind='information_update' AND NOT pkg.allowed_for_locked_claims)
    OR day<pkg.valid_trip_end_from OR (pkg.valid_trip_end_to IS NOT NULL AND day>=pkg.valid_trip_end_to)
    OR (d.kind='information_update' AND pkg.digest IS DISTINCT FROM d.locked_policy->>'digest')
    OR NOT {schema}._knowledge_policy_sources(t,person,pkg.document_refs,false,false) THEN RETURN jsonb_build_object('status','incomplete');END IF;
   IF d.kind='submission' AND clock_timestamp()>=((day+((pkg.rules_json->>'submission_deadline_days')::integer+1))::timestamp AT TIME ZONE pkg.timezone)
    THEN RETURN jsonb_build_object('status','incomplete');END IF;
   SELECT * INTO v FROM {schema}.draft_versions WHERE tenant_id=t AND draft_id=d.id AND version=d.current_version;
   content:=v.content;
   IF NOT {schema}._draft_content_valid(content) OR jsonb_array_length(content->'items')=0 OR content->>'currency' IS DISTINCT FROM pkg.rules_json->>'currency' THEN RETURN jsonb_build_object('status','incomplete');END IF;
   total:=0;
   FOR entry IN SELECT value FROM jsonb_array_elements(content->'items') LOOP
    SELECT value INTO source_item FROM jsonb_array_elements(source->'trip'->'items') WHERE value->>'id'=entry->>'trip_item_id';
    IF source_item IS NULL OR source_item->>'currency' IS DISTINCT FROM content->>'currency' THEN RETURN jsonb_build_object('status','incomplete');END IF;
    SELECT value INTO category FROM jsonb_array_elements(pkg.rules_json->'categories') WHERE value->>'code'=source_item->>'category';
    IF category IS NULL OR ((category->>'receipt_required')::boolean AND jsonb_array_length(entry->'receipt_ids')=0)
     OR (jsonb_typeof(category->'max_amount_minor')='number' AND (entry->>'amount_minor')::numeric>(category->>'max_amount_minor')::numeric) THEN RETURN jsonb_build_object('status','incomplete');END IF;
    total:=total+(entry->>'amount_minor')::numeric;
   END LOOP;
   IF total>9223372036854775807 OR (jsonb_typeof(pkg.rules_json->'max_total_minor')='number' AND total>(pkg.rules_json->>'max_total_minor')::numeric) THEN RETURN jsonb_build_object('status','incomplete');END IF;
   receipts:='[]'::jsonb;
   FOR entry IN SELECT value FROM jsonb_array_elements(content->'receipts') ORDER BY value->>'id' LOOP
    SELECT * INTO f FROM {schema}.files WHERE tenant_id=t AND files.id=(entry->>'id')::uuid FOR SHARE;
    IF NOT FOUND OR f.owner_principal_id<>person OR f.purpose<>'receipt' OR f.scan_state<>'ready' OR f.deletion_state NOT IN ('active','retained') OR f.version<>(entry->>'version')::bigint THEN RETURN jsonb_build_object('status','incomplete');END IF;
    receipts:=receipts||jsonb_build_array(jsonb_build_object('id',f.id,'version',f.version,'digest',f.digest));
   END LOOP;
   FOR entry IN SELECT value FROM jsonb_array_elements(pkg.rules_json->'approval_route') LOOP
    IF entry->>'approver_id'=person::text OR NOT EXISTS(SELECT 1 FROM {schema}.memberships m JOIN {schema}.principals p ON p.id=m.principal_id JOIN {schema}.membership_roles r ON r.tenant_id=m.tenant_id AND r.membership_id=m.id
      WHERE m.tenant_id=t AND m.principal_id=(entry->>'approver_id')::uuid AND m.active AND p.active AND r.role='approver' AND r.scope_kind='process' AND r.scope_ref='reimbursement') THEN RETURN jsonb_build_object('status','incomplete');END IF;
   END LOOP;
   body:=jsonb_build_object('trip_id',d.trip_ref,'currency',content->>'currency','items',content->'items','total_minor',total::bigint,'receipts',receipts,
    'policy',jsonb_build_object('id',pkg.id,'version',pkg.package_version,'digest',pkg.digest),'approval_route',pkg.rules_json->'approval_route','destination','reference-reimbursement');
   IF d.kind='information_update' THEN body:=body||jsonb_build_object('revision',d.claim_revision,'draft',jsonb_build_object('id',d.id,'version',d.current_version));END IF;
   material:=jsonb_build_object('draft_id',d.id,'draft_version',d.current_version,'kind',d.kind,'claim_id',d.claim_ref,'claim_version',d.claim_version,'data',body);
   hash:=encode(sha256(convert_to('eai:draft-material:v1:'||{schema}._policy_canonical(material),'UTF8')),'hex');
   deadline:=least(deadline,(source->>'expires_at')::numeric);
   INSERT INTO {schema}.draft_previews(tenant_id,draft_id,version,material_hash,material,source_digest,authority_expires_at)
    VALUES(t,d.id,d.current_version,hash,material,encode(sha256(convert_to({schema}._policy_canonical(payload->'source'),'UTF8')),'hex'),to_timestamp(deadline::double precision))
    ON CONFLICT(tenant_id,draft_id,version,material_hash) DO UPDATE SET source_digest=EXCLUDED.source_digest,authority_expires_at=EXCLUDED.authority_expires_at;
   preview:=material||jsonb_build_object('preview',true,'material_hash',hash,'declared_amounts',true,'requires_confirmation',true);
  END IF;
 END IF;
 auth:={schema}.authorization_call(token,CASE WHEN operation='read' THEN 'context' ELSE 'finish' END,CASE WHEN operation='read' THEN '{{}}'::jsonb ELSE jsonb_build_object('csrf_hash',payload->>'csrf_hash') END);
 IF auth->>'status' IS DISTINCT FROM 'ok' OR extract(epoch FROM clock_timestamp())>=deadline THEN RAISE EXCEPTION 'Draft authority expired' USING ERRCODE='40001';END IF;
 RETURN jsonb_build_object('status','ok','draft',{schema}._draft_json(d),'preview',preview,'context',context||jsonb_build_object('authority_expires_at',deadline),'authority_expires_at',deadline);
EXCEPTION WHEN unique_violation THEN RETURN jsonb_build_object('status','conflict');
 WHEN data_exception OR check_violation THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.draft_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.draft_call(text,text,jsonb) TO {access_role};

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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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



