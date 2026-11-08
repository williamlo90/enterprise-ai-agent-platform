-- DATA01: exact owning database content erasure under a durable fence.
-- External completeness assertions are private producer evidence, never inputs
-- accepted from a browser/model. Empty registry keeps every request held.
ALTER TABLE {schema}.conversation_requests ADD COLUMN privacy_created_at timestamptz NOT NULL DEFAULT clock_timestamp();
ALTER TABLE {schema}.assisted_requests ADD COLUMN privacy_created_at timestamptz NOT NULL DEFAULT clock_timestamp();
CREATE TABLE {schema}.privacy_delete_clearances(
 tenant_id uuid NOT NULL,actor_id uuid NOT NULL,fence_key uuid NOT NULL,store text NOT NULL CHECK(store IN ('backup','provider','holds')),
 policy_hash text NOT NULL CHECK(policy_hash ~ '^[a-f0-9]{{64}}$'),source_digest text NOT NULL CHECK(source_digest ~ '^[a-f0-9]{{64}}$'),
 region text NOT NULL,checked_at timestamptz NOT NULL,expires_at timestamptz NOT NULL,
 evidence_sha256 text NOT NULL CHECK(evidence_sha256 ~ '^[a-f0-9]{{64}}$'),environment text NOT NULL CHECK(environment IN ('production','fictional_fixture')),
 PRIMARY KEY(tenant_id,actor_id,fence_key,store),CHECK(expires_at>checked_at),CHECK(expires_at<=checked_at+interval '60 seconds'),
 FOREIGN KEY(tenant_id,actor_id,fence_key) REFERENCES {schema}.privacy_conversation_fences(tenant_id,actor_id,key)
);
CREATE TABLE {schema}.privacy_delete_results(
 tenant_id uuid NOT NULL,actor_id uuid NOT NULL,fence_key uuid NOT NULL,key uuid NOT NULL,result jsonb NOT NULL,
 PRIMARY KEY(tenant_id,actor_id,fence_key),UNIQUE(tenant_id,actor_id,key),
 FOREIGN KEY(tenant_id,actor_id,fence_key) REFERENCES {schema}.privacy_conversation_fences(tenant_id,actor_id,key)
);
CREATE TABLE {schema}.privacy_erasure_contexts(
 backend_pid integer NOT NULL,transaction_id text NOT NULL,tenant_id uuid NOT NULL,conversation_id uuid NOT NULL,
 PRIMARY KEY(backend_pid,transaction_id,tenant_id,conversation_id)
);
ALTER TABLE {schema}.privacy_delete_clearances ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.privacy_delete_clearances FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.privacy_delete_results ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.privacy_delete_results FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.privacy_erasure_contexts ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.privacy_erasure_contexts FORCE ROW LEVEL SECURITY;
CREATE POLICY maintenance ON {schema}.privacy_delete_clearances TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.privacy_delete_results TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.privacy_erasure_contexts TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.privacy_delete_clearances,{schema}.privacy_delete_results,{schema}.privacy_erasure_contexts FROM PUBLIC,{access_role};
CREATE TRIGGER privacy_delete_result_immutable BEFORE UPDATE OR DELETE ON {schema}.privacy_delete_results FOR EACH ROW EXECUTE FUNCTION {schema}.reject_snapshot_mutation();
CREATE TRIGGER privacy_delete_result_no_truncate BEFORE TRUNCATE ON {schema}.privacy_delete_results FOR EACH STATEMENT EXECUTE FUNCTION {schema}.reject_snapshot_mutation();

CREATE FUNCTION {schema}._privacy_erasure_active(t uuid,cid uuid) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT EXISTS(SELECT 1 FROM {schema}.privacy_erasure_contexts WHERE backend_pid=pg_backend_pid() AND transaction_id=pg_current_xact_id()::text AND tenant_id=t AND conversation_id=cid);
$$;
REVOKE ALL ON FUNCTION {schema}._privacy_erasure_active(uuid,uuid) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.privacy_conversation_delete_call(token text,operation text,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
#variable_conflict use_column
DECLARE person uuid;t uuid;target uuid;request_key uuid;target_key uuid;auth jsonb;i jsonb;deadline numeric;
 f {schema}.privacy_conversation_fences%ROWTYPE;prior {schema}.privacy_delete_results%ROWTYPE;s {schema}.sessions%ROWTYPE;
 approval jsonb;p jsonb;inventory jsonb;rule jsonb;audit_rule jsonb;clearance {schema}.privacy_delete_clearances%ROWTYPE;
 loc {schema}.privacy_store_locations%ROWTYPE;store_name text;copy_class text;latest timestamptz;minimum numeric;moment timestamptz;bound timestamptz;
 result jsonb;projection text;metadata_expiry timestamptz;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Privacy capability rejected';END IF;
 IF operation IS NULL OR operation NOT IN ('lock','delete') OR jsonb_typeof(payload) IS DISTINCT FROM 'object' OR octet_length(payload::text)>4096
  OR (SELECT count(*) FROM jsonb_object_keys(payload))<>4 OR NOT payload ?& ARRAY['id','key','fence_key','csrf_hash']
  OR EXISTS(SELECT 1 FROM jsonb_each(payload) WHERE jsonb_typeof(value)<>'string') THEN RETURN jsonb_build_object('status','invalid');END IF;
 target:=(payload->>'id')::uuid;request_key:=(payload->>'key')::uuid;target_key:=(payload->>'fence_key')::uuid;
 IF request_key='00000000-0000-0000-0000-000000000000'::uuid OR target_key='00000000-0000-0000-0000-000000000000'::uuid THEN RETURN jsonb_build_object('status','invalid');END IF;
 SELECT principal_id,active_tenant_id INTO person,t FROM {schema}.sessions WHERE token_hash=token;
 IF person IS NULL OR t IS NULL THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('authorization-role:'||t::text,0));PERFORM id FROM {schema}.principals WHERE id=person FOR UPDATE;
 auth:={schema}.authorization_call(token,'context',jsonb_build_object('csrf_hash',payload->>'csrf_hash'));
 IF auth->>'status' IS DISTINCT FROM 'ok' THEN RETURN auth;END IF;i:=auth->'identity';
 IF i->>'active_tenant_id' IS DISTINCT FROM t::text OR i->>'principal_id' IS DISTINCT FROM person::text THEN RETURN jsonb_build_object('status','conflict');END IF;
 IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(auth->'grants') g WHERE g->>'role'='employee' AND g->>'scope_kind'='tenant' AND g->>'scope_ref'=t::text) THEN RETURN jsonb_build_object('status','forbidden');END IF;
 PERFORM id FROM {schema}.conversations WHERE tenant_id=t AND id=target AND owner_principal_id=person FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
 SELECT * INTO s FROM {schema}.sessions WHERE token_hash=token;deadline:=extract(epoch FROM least(s.expires_at,s.last_seen_at+interval '1800 seconds'));
 IF operation='lock' THEN RETURN jsonb_build_object('status','ok','authority_expires_at',deadline);END IF;
 SELECT * INTO f FROM {schema}.privacy_conversation_fences WHERE tenant_id=t AND actor_id=person AND conversation_id=target AND key=target_key;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
 SELECT * INTO prior FROM {schema}.privacy_delete_results WHERE tenant_id=t AND actor_id=person AND fence_key=target_key;
 IF FOUND THEN
  IF prior.key<>request_key THEN RETURN jsonb_build_object('status','conflict');END IF;
  RETURN prior.result||jsonb_build_object('status','ok','replayed',true,'authority_expires_at',deadline);
 END IF;
 IF f.original_session_id IS DISTINCT FROM s.id OR f.original_membership_id IS DISTINCT FROM (i->>'active_membership_id')::uuid OR f.original_access_revision IS DISTINCT FROM (i->>'access_revision')::bigint THEN RETURN jsonb_build_object('status','conflict');END IF;
 IF EXISTS(SELECT 1 FROM {schema}.privacy_fence_releases WHERE tenant_id=t AND actor_id=person AND fence_key=target_key)
  OR EXISTS(SELECT 1 FROM {schema}.privacy_conversation_effect_intents WHERE tenant_id=t AND actor_id=person AND fence_key=target_key) THEN RETURN jsonb_build_object('status','conflict');END IF;
 approval:={schema}.privacy_effect_policy_call(token,'effective','{{}}'::jsonb);
 IF approval->>'status' IS DISTINCT FROM 'ok' THEN RETURN approval;END IF;p:=approval->'policy';
 IF encode(sha256(convert_to(p::text,'UTF8')),'hex') IS DISTINCT FROM f.policy_hash THEN RETURN jsonb_build_object('status','conflict');END IF;
 inventory:={schema}.privacy_conversation_inventory(token,jsonb_build_object('id',target));
 IF inventory->>'status' IS DISTINCT FROM 'ok' THEN RETURN inventory;END IF;
 IF inventory->>'content_digest' IS DISTINCT FROM f.source_digest THEN RETURN jsonb_build_object('status','conflict');END IF;
 IF (inventory->>'unknown_provider_attempts')::integer<>0 OR (inventory->>'financial_dependencies')::integer<>0 THEN RETURN jsonb_build_object('status','held');END IF;
 SELECT * INTO loc FROM {schema}.privacy_store_locations WHERE runtime_role=session_user::regrole::oid AND store='db' AND verified_at<=clock_timestamp() AND expires_at>clock_timestamp();
 IF loc.region IS NULL OR loc.environment='fictional_fixture' AND (SELECT n.nspname FROM pg_catalog.pg_namespace n JOIN pg_catalog.pg_class c ON c.relnamespace=n.oid WHERE c.oid='{schema}.privacy_store_locations'::regclass) !~ '^db03_probe_[a-f0-9]+$' THEN RETURN jsonb_build_object('status','held');END IF;
 SELECT value INTO audit_rule FROM jsonb_array_elements(p->'copy_rules') WHERE value->>'category'='audit' AND value->>'store'='db' AND value->>'copy_class'='audit_metadata';
 IF audit_rule IS NULL OR NOT (audit_rule->'regions') ? loc.region THEN RETURN jsonb_build_object('status','held');END IF;
 moment:=clock_timestamp();bound:=least(to_timestamp(deadline::double precision),to_timestamp((p->>'expires_at')::double precision),loc.expires_at);
 -- The source classifier is fixed by the owning schema, not user/model input.
 FOREACH store_name IN ARRAY ARRAY['db','cache','checkpoint','backup','provider','holds'] LOOP
  IF store_name IN ('backup','provider','holds') THEN
   SELECT * INTO clearance FROM {schema}.privacy_delete_clearances WHERE tenant_id=t AND actor_id=person AND fence_key=target_key AND store=store_name
    AND policy_hash=f.policy_hash AND source_digest=f.source_digest AND checked_at>=f.created_at AND checked_at<=moment AND expires_at>moment;
   IF NOT FOUND OR clearance.environment='fictional_fixture' AND (SELECT n.nspname FROM pg_catalog.pg_namespace n JOIN pg_catalog.pg_class c ON c.relnamespace=n.oid WHERE c.oid='{schema}.privacy_delete_clearances'::regclass) !~ '^db03_probe_[a-f0-9]+$' THEN RETURN jsonb_build_object('status','held');END IF;
   bound:=least(bound,clearance.expires_at);
   IF store_name='holds' THEN CONTINUE;END IF;
   copy_class:=CASE store_name WHEN 'backup' THEN 'backup_snapshot' ELSE 'provider_copy' END;
   SELECT value INTO rule FROM jsonb_array_elements(p->'copy_rules') WHERE value->>'category'='conversations' AND value->>'store'=store_name AND value->>'copy_class'=copy_class;
   IF rule IS NULL OR NOT (rule->'regions') ? clearance.region THEN RETURN jsonb_build_object('status','held');END IF;
   CONTINUE;
  END IF;
  copy_class:=CASE store_name WHEN 'db' THEN 'content' WHEN 'cache' THEN 'derived_cache' ELSE 'checkpoint_state' END;
  SELECT value INTO rule FROM jsonb_array_elements(p->'copy_rules') WHERE value->>'category'='conversations' AND value->>'store'=store_name AND value->>'copy_class'=copy_class;
  IF rule IS NULL OR NOT (rule->'regions') ? loc.region THEN RETURN jsonb_build_object('status','held');END IF;
  minimum:=CASE WHEN rule->>'min_retain_seconds'='not_applicable' THEN 0 ELSE (rule->>'min_retain_seconds')::numeric END;
  IF store_name='db' THEN
   SELECT max(value) INTO latest FROM (SELECT updated_at value FROM {schema}.conversations WHERE tenant_id=t AND id=target UNION ALL SELECT created_at FROM {schema}.messages WHERE tenant_id=t AND conversation_id=target UNION ALL SELECT updated_at FROM {schema}.feedback WHERE tenant_id=t AND conversation_id=target) q;
  ELSIF store_name='cache' THEN
   SELECT max(value) INTO latest FROM (SELECT privacy_created_at value FROM {schema}.conversation_requests WHERE tenant_id=t AND conversation_id=target UNION ALL SELECT privacy_created_at FROM {schema}.assisted_requests WHERE tenant_id=t AND conversation_id=target) q;
  ELSE
   SELECT max(value) INTO latest FROM (SELECT cp.privacy_created_at value FROM {schema}.checkpoints cp JOIN {schema}.runs r ON r.tenant_id=cp.tenant_id AND r.id=cp.run_id WHERE r.tenant_id=t AND r.conversation_id=target UNION ALL SELECT cp.privacy_created_at FROM {schema}.checkpoint_writes cp JOIN {schema}.runs r ON r.tenant_id=cp.tenant_id AND r.id=cp.run_id WHERE r.tenant_id=t AND r.conversation_id=target) q;
  END IF;
  IF latest IS NOT NULL AND extract(epoch FROM latest)+minimum>extract(epoch FROM moment) THEN RETURN jsonb_build_object('status','held');END IF;
 END LOOP;
 IF bound<=clock_timestamp() THEN RETURN jsonb_build_object('status','held');END IF;
 metadata_expiry:=moment+make_interval(secs=>(audit_rule->>'expire_after_seconds')::double precision);
 INSERT INTO {schema}.privacy_conversation_effect_intents(tenant_id,actor_id,fence_key,effect_key) VALUES(t,person,target_key,request_key);
 INSERT INTO {schema}.privacy_erasure_contexts VALUES(pg_backend_pid(),pg_current_xact_id()::text,t,target);
 DELETE FROM {schema}.checkpoint_writes cp USING {schema}.runs r WHERE cp.tenant_id=r.tenant_id AND cp.run_id=r.id AND r.tenant_id=t AND r.conversation_id=target;
 DELETE FROM {schema}.checkpoints cp USING {schema}.runs r WHERE cp.tenant_id=r.tenant_id AND cp.run_id=r.id AND r.tenant_id=t AND r.conversation_id=target;
 DELETE FROM {schema}.rag_citations WHERE tenant_id=t AND conversation_id=target;
 DELETE FROM {schema}.feedback WHERE tenant_id=t AND conversation_id=target;
 DELETE FROM {schema}.assisted_requests WHERE tenant_id=t AND conversation_id=target;
 UPDATE {schema}.messages SET body='[redacted]' WHERE tenant_id=t AND conversation_id=target;
 UPDATE {schema}.conversation_requests SET response=jsonb_build_object('id',target,'privacy_redacted',true) WHERE tenant_id=t AND conversation_id=target;
 UPDATE {schema}.conversations SET manual_name=NULL,version=version+1,updated_at=clock_timestamp() WHERE tenant_id=t AND id=target;
 DELETE FROM {schema}.privacy_erasure_contexts WHERE backend_pid=pg_backend_pid() AND transaction_id=pg_current_xact_id()::text AND tenant_id=t AND conversation_id=target;
 -- Crossing an authority boundary rolls back both effect and receipt, not a
 -- misleading successful half-erasure. The effect is entirely one database UoW.
 IF bound<=clock_timestamp() THEN RAISE EXCEPTION 'Privacy authority expired' USING ERRCODE='23514';END IF;
 result:=jsonb_build_object('id',target,'fence_key',target_key,'key',request_key,'content_deleted',true,'erased',false,'execution_state','completed_with_retention','projection','conversation-content-v1',
  'retained','minimal audit/replay identifiers and source business records; not anonymous','metadata_expires_at',metadata_expiry,'policy_hash',f.policy_hash,'source_digest',f.source_digest);
 INSERT INTO {schema}.privacy_delete_results VALUES(t,person,target_key,request_key,result);
 RETURN result||jsonb_build_object('status','ok','replayed',false,'authority_expires_at',extract(epoch FROM bound));
EXCEPTION WHEN data_exception THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.privacy_conversation_delete_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.privacy_conversation_delete_call(text,text,jsonb) TO {access_role};

CREATE OR REPLACE FUNCTION {schema}._privacy_conversation_growth_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE row_value jsonb;t uuid;cid uuid;rid uuid;
BEGIN
 row_value:=CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
 IF TG_OP='UPDATE' AND (to_jsonb(OLD)->'tenant_id' IS DISTINCT FROM to_jsonb(NEW)->'tenant_id' OR
  to_jsonb(OLD)->'conversation_id' IS DISTINCT FROM to_jsonb(NEW)->'conversation_id' OR
  to_jsonb(OLD)->'run_id' IS DISTINCT FROM to_jsonb(NEW)->'run_id') THEN
  RAISE EXCEPTION 'Content scope is immutable' USING ERRCODE='23514';END IF;
 t:=(row_value->>'tenant_id')::uuid;
 IF TG_TABLE_NAME='conversations' THEN cid:=(row_value->>'id')::uuid;
 ELSIF TG_TABLE_NAME IN ('checkpoints','checkpoint_writes','rag_attempts') THEN
  rid:=(row_value->>'run_id')::uuid;
  SELECT conversation_id INTO cid FROM {schema}.runs WHERE tenant_id=t AND id=rid;
 ELSE cid:=(row_value->>'conversation_id')::uuid;END IF;
 IF TG_OP IN ('UPDATE','DELETE') AND {schema}._privacy_erasure_active(t,cid) THEN IF TG_OP='DELETE' THEN RETURN OLD;END IF;RETURN NEW;END IF;
 PERFORM id FROM {schema}.conversations WHERE tenant_id=t AND id=cid FOR UPDATE;
 IF EXISTS(SELECT 1 FROM {schema}.privacy_conversation_fences f WHERE f.tenant_id=t AND f.conversation_id=cid AND NOT EXISTS(SELECT 1 FROM {schema}.privacy_fence_releases e WHERE e.tenant_id=f.tenant_id AND e.actor_id=f.actor_id AND e.fence_key=f.key)) THEN
  RAISE EXCEPTION 'Conversation growth is fenced' USING ERRCODE='23514';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD;END IF;RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION {schema}._privacy_conversation_growth_guard() FROM PUBLIC,{access_role};



CREATE OR REPLACE FUNCTION {schema}._assisted_immutable() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF TG_OP='DELETE' AND {schema}._privacy_erasure_active(OLD.tenant_id,OLD.conversation_id) THEN RETURN OLD;END IF;
 IF TG_OP='DELETE' OR (to_jsonb(NEW)-ARRAY['proposal','draft_id']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['proposal','draft_id'])
  OR (OLD.proposal IS NOT NULL AND NEW.proposal IS DISTINCT FROM OLD.proposal)
  OR (OLD.draft_id IS NOT NULL AND NEW.draft_id IS DISTINCT FROM OLD.draft_id)
 THEN RAISE EXCEPTION 'Assisted identity is immutable' USING ERRCODE='23514';END IF;RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION {schema}._assisted_immutable() FROM PUBLIC,{access_role};

CREATE OR REPLACE FUNCTION {schema}.privacy_conversation_fence_call(token text,operation text,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
#variable_conflict use_column
DECLARE auth jsonb;i jsonb;t uuid;person uuid;target uuid;key_value uuid;writing boolean;c {schema}.conversations%ROWTYPE;
 prior {schema}.privacy_conversation_fences%ROWTYPE;hash text;policy jsonb;policy_result jsonb;inventory jsonb;deadline numeric;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Privacy capability rejected';END IF;
 IF operation IS NULL OR operation NOT IN ('lock','fence','fence_status') OR jsonb_typeof(payload) IS DISTINCT FROM 'object' OR octet_length(payload::text)>4096 THEN RETURN jsonb_build_object('status','invalid');END IF;
 writing:=operation IN ('lock','fence');target:=(payload->>'id')::uuid;
 IF target IS NULL OR jsonb_typeof(payload->'id') IS DISTINCT FROM 'string' THEN RETURN jsonb_build_object('status','invalid');END IF;
 IF writing THEN
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>6 OR NOT payload ?& ARRAY['id','key','csrf_hash','version','content_digest','policy_hash']
   OR jsonb_typeof(payload->'key') IS DISTINCT FROM 'string' OR (payload->>'key')::uuid='00000000-0000-0000-0000-000000000000'::uuid
   OR jsonb_typeof(payload->'version') IS DISTINCT FROM 'number' OR (payload->>'version') !~ '^[0-9]+$' OR (payload->>'version')::numeric NOT BETWEEN 1 AND 999999999999999999
   OR jsonb_typeof(payload->'content_digest') IS DISTINCT FROM 'string' OR (payload->>'content_digest') !~ '^[a-f0-9]{{64}}$'
   OR jsonb_typeof(payload->'policy_hash') IS DISTINCT FROM 'string' OR (payload->>'policy_hash') !~ '^[a-f0-9]{{64}}$' THEN RETURN jsonb_build_object('status','invalid');END IF;
  key_value:=(payload->>'key')::uuid;
 ELSIF (SELECT count(*) FROM jsonb_object_keys(payload))<>1 THEN RETURN jsonb_build_object('status','invalid');END IF;
 SELECT principal_id,active_tenant_id INTO person,t FROM {schema}.sessions WHERE token_hash=token;
 IF person IS NULL OR t IS NULL THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
 IF writing THEN
  PERFORM pg_advisory_xact_lock(hashtextextended('authorization-role:'||t::text,0));
  PERFORM id FROM {schema}.principals WHERE id=person FOR UPDATE;
 END IF;
 auth:={schema}.authorization_call(token,'context',CASE WHEN writing THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
 IF auth->>'status' IS DISTINCT FROM 'ok' THEN RETURN auth;END IF;
 i:=auth->'identity';
 IF i->>'active_tenant_id' IS DISTINCT FROM t::text OR i->>'principal_id' IS DISTINCT FROM person::text THEN RETURN jsonb_build_object('status','conflict');END IF;
 IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(auth->'grants') g WHERE g->>'role'='employee' AND g->>'scope_kind'='tenant' AND g->>'scope_ref'=t::text) THEN RETURN jsonb_build_object('status','forbidden');END IF;
 SELECT * INTO c FROM {schema}.conversations WHERE tenant_id=t AND id=target AND owner_principal_id=person FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
 SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) INTO deadline FROM {schema}.sessions WHERE token_hash=token;
 IF operation='lock' THEN RETURN jsonb_build_object('status','ok','authority_expires_at',deadline);END IF;
 SELECT * INTO prior FROM {schema}.privacy_conversation_fences f WHERE f.tenant_id=t AND f.conversation_id=target AND NOT EXISTS(SELECT 1 FROM {schema}.privacy_fence_releases e WHERE e.tenant_id=f.tenant_id AND e.actor_id=f.actor_id AND e.fence_key=f.key) ORDER BY f.created_at DESC,f.key DESC LIMIT 1;
 IF NOT writing THEN
  IF prior.key IS NOT NULL AND EXISTS(SELECT 1 FROM {schema}.privacy_delete_results WHERE tenant_id=t AND actor_id=person AND fence_key=prior.key) THEN
   RETURN (SELECT result FROM {schema}.privacy_delete_results WHERE tenant_id=t AND actor_id=person AND fence_key=prior.key)||jsonb_build_object('status','ok','fenced',true,'authority_expires_at',deadline);
  END IF;
  RETURN jsonb_build_object('status','ok','id',target,'fenced',prior.conversation_id IS NOT NULL,'execution_state','held','erased',false,'reason',CASE WHEN prior.conversation_id IS NULL THEN 'not_requested' ELSE 'complete_store_proofs_required' END,'authority_expires_at',deadline);
 END IF;
 hash:=encode(sha256(convert_to((payload-'csrf_hash')::text,'UTF8')),'hex');
 -- A released request remains replayable as released, never rearms old writers.
 IF EXISTS(SELECT 1 FROM {schema}.privacy_conversation_fences f WHERE f.tenant_id=t AND f.actor_id=person AND f.key=key_value
  AND EXISTS(SELECT 1 FROM {schema}.privacy_fence_releases e WHERE e.tenant_id=t AND e.actor_id=person AND e.fence_key=f.key)) THEN
  IF NOT EXISTS(SELECT 1 FROM {schema}.privacy_conversation_fences f WHERE f.tenant_id=t AND f.actor_id=person AND f.key=key_value AND f.conversation_id=target AND f.request_hash=hash) THEN RETURN jsonb_build_object('status','conflict');END IF;
  RETURN jsonb_build_object('status','ok','id',target,'key',key_value,'fenced',prior.conversation_id IS NOT NULL,'released',true,'replayed',true,'execution_state','cancelled','erased',false,'authority_expires_at',deadline);
 END IF;
 IF prior.conversation_id IS NOT NULL THEN
  IF prior.key IS DISTINCT FROM key_value OR prior.request_hash IS DISTINCT FROM hash THEN RETURN jsonb_build_object('status','conflict');END IF;
  RETURN jsonb_build_object('status','ok','id',target,'key',prior.key,'fenced',true,'replayed',true,'execution_state','held','erased',false,'reason','complete_store_proofs_required','authority_expires_at',deadline);
 END IF;
 IF EXISTS(SELECT 1 FROM {schema}.privacy_conversation_fences WHERE tenant_id=t AND actor_id=person AND key=key_value) THEN RETURN jsonb_build_object('status','conflict');END IF;
 policy_result:={schema}.privacy_effect_policy_call(token,'effective','{{}}'::jsonb);
 IF policy_result->>'status' IS DISTINCT FROM 'ok' THEN RETURN policy_result;END IF;
 policy:=policy_result->'policy';
 IF encode(sha256(convert_to(policy::text,'UTF8')),'hex') IS DISTINCT FROM payload->>'policy_hash' THEN RETURN jsonb_build_object('status','conflict');END IF;
 inventory:={schema}.privacy_conversation_inventory(token,jsonb_build_object('id',target));
 IF inventory->>'status' IS DISTINCT FROM 'ok' THEN RETURN inventory;END IF;
 IF c.version IS DISTINCT FROM (payload->>'version')::bigint OR inventory->>'content_digest' IS DISTINCT FROM payload->>'content_digest' THEN RETURN jsonb_build_object('status','conflict');END IF;
 -- Close the admitted run and invalidate every existing writer epoch before the
 -- immutable fence is inserted. Financial/provider unknowns remain recorded.
 UPDATE {schema}.runs SET state='failed',writer_generation=writer_generation+1,version=version+1,lease_until=NULL WHERE tenant_id=t AND conversation_id=target AND state IN ('queued','running','waiting','held');
 UPDATE {schema}.conversations SET active_run_id=NULL,version=version+1,updated_at=clock_timestamp() WHERE tenant_id=t AND id=target;
 -- Recheck the finite authority/policy boundary after all waits and work.
 IF least(deadline,(policy->>'expires_at')::numeric)<=extract(epoch FROM clock_timestamp()) THEN RAISE EXCEPTION 'Privacy authority expired' USING ERRCODE='23514';END IF;
 INSERT INTO {schema}.privacy_conversation_fences(tenant_id,conversation_id,actor_id,key,request_hash,policy_hash,policy_revision,source_digest,source_version,original_session_id,original_membership_id,original_access_revision)
 VALUES(t,target,person,key_value,hash,payload->>'policy_hash',(policy->>'revision')::bigint,payload->>'content_digest',c.version,(SELECT id FROM {schema}.sessions WHERE token_hash=token),(i->>'active_membership_id')::uuid,(i->>'access_revision')::bigint);
 RETURN jsonb_build_object('status','ok','id',target,'key',key_value,'fenced',true,'replayed',false,'execution_state','held','erased',false,'reason','complete_store_proofs_required','authority_expires_at',least(deadline,(policy->>'expires_at')::numeric));
EXCEPTION WHEN data_exception THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.privacy_conversation_fence_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.privacy_conversation_fence_call(text,text,jsonb) TO {access_role};



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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','privacy_delete_clearances','privacy_delete_results','privacy_erasure_contexts','privacy_store_locations','privacy_export_receipts','privacy_fence_releases','privacy_conversation_effect_intents','privacy_conversation_fences','privacy_effect_policies','privacy_effect_decisions','privacy_policies','privacy_policy_decisions','registered_config_refs','admin_requests','admin_events','assisted_requests','action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('privacy_delete_clearances','privacy_delete_results','privacy_erasure_contexts','privacy_store_locations','privacy_export_receipts','privacy_fence_releases','privacy_conversation_effect_intents','privacy_conversation_fences','privacy_effect_policies','privacy_effect_decisions','privacy_policies','privacy_policy_decisions','registered_config_refs','admin_requests','admin_events','assisted_requests','action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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
                        AND NOT (f.proname='admin_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='privacy_policy_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='privacy_conversation_delete_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='privacy_conversation_export_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='privacy_conversation_release_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='privacy_conversation_fence_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='privacy_effect_policy_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='privacy_conversation_inventory' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, payload jsonb')
                        AND NOT (f.proname='workspace_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='assisted_owner_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, payload jsonb')
                        AND NOT (f.proname='assisted_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='operation text, payload jsonb')
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




