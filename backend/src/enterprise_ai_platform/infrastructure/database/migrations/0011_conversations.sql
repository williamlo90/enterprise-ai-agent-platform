-- CONV-01: owner-only browser admission, queued run foundation, private provenance.
CREATE TABLE {schema}.conversations (
 tenant_id uuid NOT NULL,id uuid NOT NULL,owner_principal_id uuid NOT NULL,language text NOT NULL CHECK(language IN ('id','en')),
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),next_sequence bigint NOT NULL DEFAULT 1 CHECK(next_sequence BETWEEN 1 AND 201),active_run_id uuid,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,id,owner_principal_id),FOREIGN KEY(tenant_id,owner_principal_id) REFERENCES {schema}.memberships(tenant_id,principal_id)
);
CREATE INDEX conversation_owner_page ON {schema}.conversations(tenant_id,owner_principal_id,created_at DESC,id DESC);
CREATE TABLE {schema}.runs (
 tenant_id uuid NOT NULL,id uuid NOT NULL,conversation_id uuid NOT NULL,actor_id uuid NOT NULL,membership_id uuid NOT NULL,session_id uuid NOT NULL REFERENCES {schema}.sessions(id),
 access_revision bigint NOT NULL CHECK(access_revision>0),state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','waiting','completed','failed','held')),
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),writer_generation bigint NOT NULL DEFAULT 0 CHECK(writer_generation>=0),lease_until timestamptz,
 model_ref text,prompt_version text,config_version bigint CHECK(config_version>0),input_message_id uuid NOT NULL,job_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,id,conversation_id),
 FOREIGN KEY(tenant_id,conversation_id,actor_id) REFERENCES {schema}.conversations(tenant_id,id,owner_principal_id),
 FOREIGN KEY(tenant_id,membership_id,actor_id) REFERENCES {schema}.memberships(tenant_id,id,principal_id),
 FOREIGN KEY(tenant_id,job_id) REFERENCES {schema}.outbox_jobs(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 CHECK(state<>'running' OR (model_ref IS NOT NULL AND prompt_version IS NOT NULL AND config_version IS NOT NULL))
);
CREATE UNIQUE INDEX conversation_pending_run ON {schema}.runs(tenant_id,conversation_id) WHERE state IN ('queued','running','waiting','held');
ALTER TABLE {schema}.conversations ADD FOREIGN KEY(tenant_id,active_run_id,id) REFERENCES {schema}.runs(tenant_id,id,conversation_id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE {schema}.messages (
 tenant_id uuid NOT NULL,id uuid NOT NULL,conversation_id uuid NOT NULL,author_kind text NOT NULL CHECK(author_kind IN ('human','model','system')),
 author_principal_id uuid,body text NOT NULL CHECK(length(body) BETWEEN 1 AND 4096 AND octet_length(body)<=8192),
 sequence bigint NOT NULL CHECK(sequence BETWEEN 1 AND 200),client_message_id uuid,run_id uuid NOT NULL,
 output_kind text CHECK(output_kind IN ('answer','clarification','insufficient_evidence','source_conflict','service_unavailable')),
 provenance_mode text NOT NULL DEFAULT 'unverified' CHECK(provenance_mode IN ('source_free','dependent','unverified')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,id,conversation_id),UNIQUE(tenant_id,conversation_id,sequence),
 FOREIGN KEY(tenant_id,conversation_id) REFERENCES {schema}.conversations(tenant_id,id),
 FOREIGN KEY(tenant_id,conversation_id,author_principal_id) REFERENCES {schema}.conversations(tenant_id,id,owner_principal_id),
 FOREIGN KEY(tenant_id,run_id,conversation_id) REFERENCES {schema}.runs(tenant_id,id,conversation_id),
 CHECK((author_kind='human' AND author_principal_id IS NOT NULL AND client_message_id IS NOT NULL AND output_kind IS NULL AND provenance_mode='source_free') OR
       (author_kind IN ('model','system') AND author_principal_id IS NULL AND client_message_id IS NULL AND output_kind IS NOT NULL))
);
CREATE UNIQUE INDEX message_client_dedup ON {schema}.messages(tenant_id,conversation_id,client_message_id) WHERE client_message_id IS NOT NULL;
ALTER TABLE {schema}.runs ADD FOREIGN KEY(tenant_id,input_message_id,conversation_id) REFERENCES {schema}.messages(tenant_id,id,conversation_id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE {schema}.source_dependencies (
 tenant_id uuid NOT NULL,id uuid NOT NULL,conversation_id uuid NOT NULL,output_message_id uuid,run_id uuid,
 source_system text NOT NULL,resource_type text NOT NULL,resource_id uuid NOT NULL,resource_version text NOT NULL CHECK(resource_version ~ '^[A-Za-z0-9_.:-]{{1,128}}$'),
 required_relation text NOT NULL,observed_revision text NOT NULL CHECK(observed_revision ~ '^[A-Za-z0-9_.:-]{{1,128}}$'),
 page integer CHECK(page BETWEEN 1 AND 100),chunk integer CHECK(chunk BETWEEN 0 AND 31),platform_document_version_id uuid,platform_file_id uuid,
 PRIMARY KEY(tenant_id,id),CHECK((output_message_id IS NOT NULL)::integer+(run_id IS NOT NULL)::integer=1),
 FOREIGN KEY(tenant_id,output_message_id,conversation_id) REFERENCES {schema}.messages(tenant_id,id,conversation_id),
 FOREIGN KEY(tenant_id,run_id,conversation_id) REFERENCES {schema}.runs(tenant_id,id,conversation_id),
 FOREIGN KEY(tenant_id,platform_document_version_id) REFERENCES {schema}.document_versions(tenant_id,id),
 FOREIGN KEY(tenant_id,platform_file_id,resource_version) REFERENCES {schema}.files(tenant_id,id,digest),
 CHECK((source_system='platform' AND resource_type='document_version' AND required_relation='read' AND platform_document_version_id IS NOT NULL AND platform_document_version_id=resource_id AND platform_file_id IS NULL) OR
       (source_system='platform' AND resource_type='file' AND required_relation='read' AND platform_file_id IS NOT NULL AND platform_file_id=resource_id AND platform_document_version_id IS NULL) OR
       (source_system='service' AND platform_document_version_id IS NULL AND platform_file_id IS NULL AND
          ((resource_type='trip' AND required_relation='owner') OR (resource_type IN ('claim','receipt') AND required_relation IN ('owner','active_reviewer')))))
);
CREATE INDEX sources_by_message ON {schema}.source_dependencies(tenant_id,output_message_id);
CREATE INDEX sources_by_run ON {schema}.source_dependencies(tenant_id,run_id);
CREATE INDEX sources_by_resource ON {schema}.source_dependencies(tenant_id,source_system,resource_type,resource_id);
CREATE TABLE {schema}.feedback (
 tenant_id uuid NOT NULL,message_id uuid NOT NULL,conversation_id uuid NOT NULL,actor_id uuid NOT NULL,rating text NOT NULL CHECK(rating IN ('up','down')),
 reason text CHECK(length(reason)<=1000 AND octet_length(reason)<=4096),version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(tenant_id,message_id,actor_id),
 FOREIGN KEY(tenant_id,message_id,conversation_id) REFERENCES {schema}.messages(tenant_id,id,conversation_id),
 FOREIGN KEY(tenant_id,conversation_id,actor_id) REFERENCES {schema}.conversations(tenant_id,id,owner_principal_id)
);
CREATE TABLE {schema}.conversation_requests (
 tenant_id uuid NOT NULL,actor_id uuid NOT NULL,key uuid NOT NULL,operation text NOT NULL CHECK(operation IN ('create','feedback')),
 request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{{64}}$'),conversation_id uuid NOT NULL,message_id uuid,response jsonb NOT NULL,
 PRIMARY KEY(tenant_id,actor_id,key),FOREIGN KEY(tenant_id,conversation_id,actor_id) REFERENCES {schema}.conversations(tenant_id,id,owner_principal_id),
 FOREIGN KEY(tenant_id,message_id,conversation_id) REFERENCES {schema}.messages(tenant_id,id,conversation_id),
 CHECK((operation='create' AND message_id IS NULL) OR (operation='feedback' AND message_id IS NOT NULL))
);
ALTER TABLE {schema}.conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.conversations FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.runs FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.messages FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.source_dependencies ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.source_dependencies FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.feedback FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.conversation_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.conversation_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_maintenance ON {schema}.conversations TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.runs TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.messages TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.source_dependencies TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.feedback TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.conversation_requests TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.conversations,{schema}.runs,{schema}.messages,{schema}.source_dependencies,{schema}.feedback,{schema}.conversation_requests FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._conversation_source_insert() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE author text;count_rows integer;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Source binding is immutable.' USING ERRCODE='23514'; END IF;
 IF NEW.output_message_id IS NOT NULL THEN
  SELECT author_kind INTO author FROM {schema}.messages WHERE tenant_id=NEW.tenant_id AND id=NEW.output_message_id AND conversation_id=NEW.conversation_id FOR UPDATE;
  IF NOT FOUND OR author='human' THEN RAISE EXCEPTION 'Source message rejected.' USING ERRCODE='23514'; END IF;
  SELECT count(*) INTO count_rows FROM {schema}.source_dependencies WHERE tenant_id=NEW.tenant_id AND output_message_id=NEW.output_message_id;
 ELSE
  PERFORM id FROM {schema}.runs WHERE tenant_id=NEW.tenant_id AND id=NEW.run_id AND conversation_id=NEW.conversation_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Source run rejected.' USING ERRCODE='23514'; END IF;
  SELECT count(*) INTO count_rows FROM {schema}.source_dependencies WHERE tenant_id=NEW.tenant_id AND run_id=NEW.run_id;
 END IF;
 IF count_rows>=8 THEN RAISE EXCEPTION 'Source limit exceeded.' USING ERRCODE='23514'; END IF;
 IF NEW.source_system='platform' AND NEW.resource_type='document_version' AND NOT EXISTS(SELECT 1 FROM {schema}.document_versions
  WHERE tenant_id=NEW.tenant_id AND id=NEW.resource_id AND version_no::text=NEW.resource_version) THEN RAISE EXCEPTION 'Source version rejected.' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION {schema}._conversation_source_insert() FROM PUBLIC,{access_role};
CREATE TRIGGER conversation_sources_immutable BEFORE INSERT OR UPDATE OR DELETE ON {schema}.source_dependencies FOR EACH ROW EXECUTE FUNCTION {schema}._conversation_source_insert();

CREATE FUNCTION {schema}._conversation_run_check() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE t uuid;cid uuid;c {schema}.conversations%ROWTYPE;pending integer;
BEGIN
 IF TG_TABLE_NAME='conversations' THEN
  IF TG_OP='DELETE' THEN t:=OLD.tenant_id;cid:=OLD.id;ELSE t:=NEW.tenant_id;cid:=NEW.id;END IF;
 ELSE
  IF TG_OP='DELETE' THEN t:=OLD.tenant_id;cid:=OLD.conversation_id;ELSE t:=NEW.tenant_id;cid:=NEW.conversation_id;END IF;
 END IF;
 SELECT * INTO c FROM {schema}.conversations WHERE tenant_id=t AND id=cid;
 IF NOT FOUND THEN RETURN NULL; END IF;
 SELECT count(*) INTO pending FROM {schema}.runs WHERE tenant_id=t AND conversation_id=cid AND state IN ('queued','running','waiting','held');
 IF (c.active_run_id IS NULL AND pending<>0) OR (c.active_run_id IS NOT NULL AND (pending<>1 OR NOT EXISTS(SELECT 1 FROM {schema}.runs r WHERE r.tenant_id=t AND r.id=c.active_run_id AND r.conversation_id=cid AND r.state IN ('queued','running','waiting','held'))))
 THEN RAISE EXCEPTION 'Conversation active run rejected.' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM {schema}.runs r LEFT JOIN {schema}.messages m ON m.tenant_id=r.tenant_id AND m.id=r.input_message_id AND m.conversation_id=r.conversation_id
  WHERE r.tenant_id=t AND r.conversation_id=cid AND (m.id IS NULL OR m.author_kind<>'human' OR m.run_id<>r.id OR m.author_principal_id<>r.actor_id)) THEN RAISE EXCEPTION 'Run input rejected.' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION {schema}._conversation_run_check() FROM PUBLIC,{access_role};
CREATE CONSTRAINT TRIGGER conversation_active_pointer AFTER INSERT OR UPDATE OR DELETE ON {schema}.conversations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {schema}._conversation_run_check();
CREATE CONSTRAINT TRIGGER conversation_run_input AFTER INSERT OR UPDATE OR DELETE ON {schema}.runs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {schema}._conversation_run_check();
CREATE CONSTRAINT TRIGGER conversation_message_input AFTER INSERT OR UPDATE OR DELETE ON {schema}.messages DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {schema}._conversation_run_check();

CREATE FUNCTION {schema}._conversation_json(c {schema}.conversations) RETURNS jsonb LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('id',c.id,'language',c.language,'version',c.version,'active_run_id',c.active_run_id,'created_at',c.created_at,'updated_at',c.updated_at);
$$;
REVOKE ALL ON FUNCTION {schema}._conversation_json({schema}.conversations) FROM PUBLIC,{access_role};
CREATE FUNCTION {schema}._conversation_message(m {schema}.messages) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 WITH value AS (SELECT jsonb_build_object('id',m.id,'sequence',m.sequence,'author_kind',m.author_kind,'body',m.body,'run_id',m.run_id,'output_kind',m.output_kind,'provenance_mode',m.provenance_mode,'created_at',m.created_at,
  'feedback',(SELECT jsonb_build_object('rating',f.rating,'reason',f.reason,'version',f.version) FROM {schema}.feedback f WHERE f.tenant_id=m.tenant_id AND f.message_id=m.id),
  '_sources',coalesce((SELECT jsonb_agg(jsonb_build_object('system',d.source_system,'kind',d.resource_type,'resource_id',d.resource_id,'version',d.resource_version,'relation',d.required_relation,'observed_revision',d.observed_revision,'page',d.page,'chunk',d.chunk) ORDER BY d.id)
    FROM {schema}.source_dependencies d WHERE d.tenant_id=m.tenant_id AND d.conversation_id=m.conversation_id AND (d.output_message_id=m.id OR (m.author_kind<>'human' AND d.run_id=m.run_id))),'[]'::jsonb)) AS item)
 SELECT item||jsonb_build_object('_snapshot',encode(sha256(convert_to(item::text,'UTF8')),'hex')) FROM value;
$$;
REVOKE ALL ON FUNCTION {schema}._conversation_message({schema}.messages) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.conversation_call(token text,operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE context jsonb;identity jsonb;t uuid;p uuid;member uuid;sid uuid;c {schema}.conversations%ROWTYPE;a {schema}.conversations%ROWTYPE;
 m {schema}.messages%ROWTYPE;r {schema}.runs%ROWTYPE;e {schema}.conversation_requests%ROWTYPE;f {schema}.feedback%ROWTYPE;
 result jsonb;items jsonb;item jsonb;entry jsonb;request_hash text;more boolean;cursor_id uuid;next_after bigint;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Conversation capability rejected.'; END IF;
 IF operation IS NULL OR operation NOT IN ('create','list','append','history','history_recheck','feedback') OR payload IS NULL OR jsonb_typeof(payload)<>'object' OR octet_length(payload::text)>32768 THEN RETURN jsonb_build_object('status','invalid'); END IF;
 context:={schema}.authorization_call(token,'context',CASE WHEN operation IN ('create','append','feedback') THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RETURN context; END IF;
 identity:=context->'identity';t:=(identity->>'active_tenant_id')::uuid;p:=(identity->>'principal_id')::uuid;member:=(identity->>'active_membership_id')::uuid;
 SELECT id INTO sid FROM {schema}.sessions WHERE token_hash=token;
 IF NOT EXISTS(SELECT 1 FROM {schema}.membership_roles g WHERE g.tenant_id=t AND g.membership_id=member AND g.role='employee' AND g.scope_kind='tenant' AND g.scope_ref=t::text) THEN
  RETURN jsonb_build_object('status',CASE WHEN operation IN ('create','list') THEN 'forbidden' ELSE 'missing' END);
 END IF;
 IF operation='create' THEN
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>4 OR NOT payload ?& ARRAY['id','language','key','csrf_hash'] OR jsonb_typeof(payload->'language') IS DISTINCT FROM 'string' OR payload->>'language' NOT IN ('id','en')
   OR jsonb_typeof(payload->'key') IS DISTINCT FROM 'string' OR jsonb_typeof(payload->'id') IS DISTINCT FROM 'string' THEN RETURN jsonb_build_object('status','invalid'); END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('eai:conversation-key:'||t::text||':'||p::text||':'||((payload->>'key')::uuid)::text,0));
  request_hash:=encode(sha256(convert_to((payload-'id'-'csrf_hash')::text,'UTF8')),'hex');
  SELECT * INTO e FROM {schema}.conversation_requests WHERE tenant_id=t AND actor_id=p AND key=(payload->>'key')::uuid;
  IF FOUND THEN
   IF e.operation<>'create' OR e.request_hash<>request_hash THEN RETURN jsonb_build_object('status','conflict'); END IF;
   SELECT * INTO c FROM {schema}.conversations WHERE tenant_id=t AND id=e.conversation_id AND owner_principal_id=p FOR SHARE;
  ELSE
   PERFORM singleton FROM {schema}.job_scheduler FOR UPDATE;
   IF (SELECT count(*) FROM {schema}.conversations WHERE tenant_id=t AND owner_principal_id=p)>=100 OR (SELECT count(*) FROM {schema}.conversations WHERE tenant_id=t)>=1000 OR (SELECT count(*) FROM {schema}.conversations)>=10000 THEN RETURN jsonb_build_object('status','capacity'); END IF;
   INSERT INTO {schema}.conversations(tenant_id,id,owner_principal_id,language) VALUES(t,(payload->>'id')::uuid,p,payload->>'language') RETURNING * INTO c;
   INSERT INTO {schema}.conversation_requests(tenant_id,actor_id,key,operation,request_hash,conversation_id,response) VALUES(t,p,(payload->>'key')::uuid,'create',request_hash,c.id,{schema}._conversation_json(c));
  END IF;
  result:={schema}._conversation_json(c);
 ELSIF operation='list' THEN
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>2 OR NOT payload ?& ARRAY['after','limit'] OR jsonb_typeof(payload->'limit') IS DISTINCT FROM 'number' OR payload->>'limit' !~ '^(20|1[0-9]|[1-9])$'
   OR (payload->'after'<>'null'::jsonb AND jsonb_typeof(payload->'after') IS DISTINCT FROM 'string') THEN RETURN jsonb_build_object('status','invalid'); END IF;
  IF payload->'after'<>'null'::jsonb THEN
   SELECT * INTO a FROM {schema}.conversations WHERE tenant_id=t AND id=(payload->>'after')::uuid AND owner_principal_id=p;
   IF NOT FOUND THEN RETURN jsonb_build_object('status','missing'); END IF;
  END IF;
  SELECT coalesce(jsonb_agg(value ORDER BY created_at DESC,id DESC),'[]'::jsonb) INTO items FROM (
   SELECT {schema}._conversation_json(cc) AS value,cc.created_at,cc.id FROM {schema}.conversations cc WHERE cc.tenant_id=t AND cc.owner_principal_id=p
    AND (a.id IS NULL OR ROW(cc.created_at,cc.id)<ROW(a.created_at,a.id)) ORDER BY cc.created_at DESC,cc.id DESC LIMIT (payload->>'limit')::integer+1) q;
  more:=jsonb_array_length(items)>(payload->>'limit')::integer;
  IF more THEN items:=items-((payload->>'limit')::integer);cursor_id:=(items->(jsonb_array_length(items)-1)->>'id')::uuid; END IF;
  result:=jsonb_build_object('items',items,'next_after',cursor_id);
 ELSE
  IF operation='feedback' THEN
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>5 OR NOT payload ?& ARRAY['id','key','rating','reason','csrf_hash'] OR jsonb_typeof(payload->'key') IS DISTINCT FROM 'string'
    OR jsonb_typeof(payload->'rating') IS DISTINCT FROM 'string' OR payload->>'rating' NOT IN ('up','down') OR (payload->'reason'<>'null'::jsonb AND (jsonb_typeof(payload->'reason') IS DISTINCT FROM 'string' OR length(payload->>'reason')>1000 OR octet_length(payload->>'reason')>4096)) THEN RETURN jsonb_build_object('status','invalid'); END IF;
   PERFORM pg_advisory_xact_lock(hashtextextended('eai:conversation-key:'||t::text||':'||p::text||':'||((payload->>'key')::uuid)::text,0));
   SELECT * INTO m FROM {schema}.messages WHERE tenant_id=t AND id=(payload->>'id')::uuid AND author_kind='model';
   SELECT * INTO c FROM {schema}.conversations WHERE tenant_id=t AND id=m.conversation_id AND owner_principal_id=p FOR UPDATE;
  ELSE
   IF NOT payload ? 'id' OR jsonb_typeof(payload->'id') IS DISTINCT FROM 'string' THEN RETURN jsonb_build_object('status','invalid'); END IF;
   IF operation='append' THEN SELECT * INTO c FROM {schema}.conversations WHERE tenant_id=t AND id=(payload->>'id')::uuid AND owner_principal_id=p FOR UPDATE;
   ELSE SELECT * INTO c FROM {schema}.conversations WHERE tenant_id=t AND id=(payload->>'id')::uuid AND owner_principal_id=p FOR SHARE; END IF;
  END IF;
  IF c.id IS NULL THEN RETURN jsonb_build_object('status','missing'); END IF;
  IF operation='append' THEN
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>6 OR NOT payload ?& ARRAY['id','message_id','run_id','client_message_id','text','csrf_hash']
    OR jsonb_typeof(payload->'text') IS DISTINCT FROM 'string' OR length(btrim(payload->>'text')) NOT BETWEEN 1 AND 4096 OR octet_length(payload->>'text')>8192
    OR translate(payload->>'text',E'\r\n\t','') ~ '[[:cntrl:]]' OR jsonb_typeof(payload->'client_message_id') IS DISTINCT FROM 'string'
    OR jsonb_typeof(payload->'message_id') IS DISTINCT FROM 'string' OR jsonb_typeof(payload->'run_id') IS DISTINCT FROM 'string' THEN RETURN jsonb_build_object('status','invalid'); END IF;
   SELECT * INTO m FROM {schema}.messages WHERE tenant_id=t AND conversation_id=c.id AND client_message_id=(payload->>'client_message_id')::uuid;
   IF FOUND THEN
    IF m.body IS DISTINCT FROM payload->>'text' THEN RETURN jsonb_build_object('status','conflict'); END IF;
   ELSE
    IF c.active_run_id IS NOT NULL THEN RETURN jsonb_build_object('status','conflict'); END IF;
    IF c.next_sequence>=200 THEN RETURN jsonb_build_object('status','capacity'); END IF;
    PERFORM singleton FROM {schema}.job_scheduler FOR UPDATE;
    IF (SELECT count(*) FROM {schema}.outbox_jobs WHERE tenant_id=t AND state IN ('queued','retry','running'))>=100 OR (SELECT count(*) FROM {schema}.outbox_jobs WHERE state IN ('queued','retry','running'))>=1000 THEN RETURN jsonb_build_object('status','capacity'); END IF;
    INSERT INTO {schema}.job_tenants(tenant_id) VALUES(t) ON CONFLICT DO NOTHING;
    INSERT INTO {schema}.outbox_jobs(tenant_id,id,job_type,business_key,target_id,expected_version,actor_id,membership_id,session_id,access_revision,retry_limit)
     VALUES(t,(payload->>'run_id')::uuid,'agent.resume',(payload->>'run_id')::uuid,(payload->>'run_id')::uuid,1,p,member,sid,(identity->>'access_revision')::bigint,3);
    INSERT INTO {schema}.runs(tenant_id,id,conversation_id,actor_id,membership_id,session_id,access_revision,input_message_id,job_id)
     VALUES(t,(payload->>'run_id')::uuid,c.id,p,member,sid,(identity->>'access_revision')::bigint,(payload->>'message_id')::uuid,(payload->>'run_id')::uuid) RETURNING * INTO r;
    INSERT INTO {schema}.messages(tenant_id,id,conversation_id,author_kind,author_principal_id,body,sequence,client_message_id,run_id,provenance_mode)
     VALUES(t,(payload->>'message_id')::uuid,c.id,'human',p,payload->>'text',c.next_sequence,(payload->>'client_message_id')::uuid,r.id,'source_free') RETURNING * INTO m;
    UPDATE {schema}.conversations SET next_sequence=next_sequence+1,version=version+1,active_run_id=r.id,updated_at=clock_timestamp() WHERE tenant_id=t AND id=c.id RETURNING * INTO c;
   END IF;
   SELECT * INTO r FROM {schema}.runs WHERE tenant_id=t AND id=m.run_id;
   result:=jsonb_build_object('conversation_id',c.id,'message_id',m.id,'sequence',m.sequence,'client_message_id',m.client_message_id,'run_id',r.id,'run_state',r.state,'conversation_version',c.version);
  ELSIF operation='feedback' THEN
   request_hash:=encode(sha256(convert_to((payload-'csrf_hash')::text,'UTF8')),'hex');
   SELECT * INTO e FROM {schema}.conversation_requests WHERE tenant_id=t AND actor_id=p AND key=(payload->>'key')::uuid;
   IF FOUND THEN
    IF e.operation<>'feedback' OR e.request_hash<>request_hash OR e.message_id<>m.id THEN RETURN jsonb_build_object('status','conflict'); END IF;
   ELSE
    INSERT INTO {schema}.feedback AS existing_feedback(tenant_id,message_id,conversation_id,actor_id,rating,reason) VALUES(t,m.id,c.id,p,payload->>'rating',payload->>'reason')
     ON CONFLICT(tenant_id,message_id,actor_id) DO UPDATE SET rating=EXCLUDED.rating,reason=EXCLUDED.reason,
      version=existing_feedback.version+CASE WHEN ROW(existing_feedback.rating,existing_feedback.reason) IS DISTINCT FROM ROW(EXCLUDED.rating,EXCLUDED.reason) THEN 1 ELSE 0 END,updated_at=clock_timestamp() RETURNING * INTO f;
    INSERT INTO {schema}.conversation_requests(tenant_id,actor_id,key,operation,request_hash,conversation_id,message_id,response)
     VALUES(t,p,(payload->>'key')::uuid,'feedback',request_hash,c.id,m.id,jsonb_build_object('version',f.version,'rating',f.rating)) RETURNING * INTO e;
   END IF;
   SELECT * INTO f FROM {schema}.feedback WHERE tenant_id=t AND message_id=m.id AND actor_id=p;
   result:=jsonb_build_object('message_id',m.id,'feedback_version',f.version,'current_rating',f.rating,'result_version',e.response->'version','accepted_rating',e.response->'rating');
  ELSE
   IF operation='history' THEN
    IF (SELECT count(*) FROM jsonb_object_keys(payload))<>3 OR NOT payload ?& ARRAY['id','after','limit'] OR jsonb_typeof(payload->'after') IS DISTINCT FROM 'number' OR payload->>'after' !~ '^(0|[1-9][0-9]{{0,17}})$'
     OR jsonb_typeof(payload->'limit') IS DISTINCT FROM 'number' OR payload->>'limit' !~ '^(20|1[0-9]|[1-9])$' THEN RETURN jsonb_build_object('status','invalid'); END IF;
    PERFORM mm.id FROM {schema}.messages mm WHERE mm.tenant_id=t AND mm.conversation_id=c.id AND mm.sequence>(payload->>'after')::bigint ORDER BY mm.sequence LIMIT (payload->>'limit')::integer+1 FOR SHARE;
    PERFORM rr.id FROM {schema}.runs rr WHERE rr.tenant_id=t AND rr.conversation_id=c.id ORDER BY rr.id FOR SHARE;
    SELECT coalesce(jsonb_agg(value ORDER BY sequence),'[]'::jsonb) INTO items FROM (
     SELECT {schema}._conversation_message(mm) AS value,mm.sequence FROM {schema}.messages mm WHERE mm.tenant_id=t AND mm.conversation_id=c.id AND mm.sequence>(payload->>'after')::bigint ORDER BY mm.sequence LIMIT (payload->>'limit')::integer+1) q;
    more:=jsonb_array_length(items)>(payload->>'limit')::integer;
    IF more THEN items:=items-((payload->>'limit')::integer);next_after:=(items->(jsonb_array_length(items)-1)->>'sequence')::bigint;END IF;
   ELSE
    IF (SELECT count(*) FROM jsonb_object_keys(payload))<>3 OR NOT payload ?& ARRAY['id','expected_version','bindings'] OR jsonb_typeof(payload->'expected_version') IS DISTINCT FROM 'number'
     OR payload->>'expected_version' !~ '^[1-9][0-9]{{0,17}}$' OR jsonb_typeof(payload->'bindings') IS DISTINCT FROM 'array' THEN RETURN jsonb_build_object('status','invalid'); END IF;
    IF c.version<>(payload->>'expected_version')::bigint OR jsonb_array_length(payload->'bindings')>20 THEN RETURN jsonb_build_object('status','conflict'); END IF;
    items:='[]'::jsonb;
    FOR item IN SELECT value FROM jsonb_array_elements(payload->'bindings') LOOP
     IF jsonb_typeof(item) IS DISTINCT FROM 'object' THEN RETURN jsonb_build_object('status','invalid'); END IF;
     IF (SELECT count(*) FROM jsonb_object_keys(item))<>3 OR NOT item ?& ARRAY['id','sequence','snapshot'] OR jsonb_typeof(item->'id') IS DISTINCT FROM 'string'
      OR jsonb_typeof(item->'sequence') IS DISTINCT FROM 'number' OR jsonb_typeof(item->'snapshot') IS DISTINCT FROM 'string' THEN RETURN jsonb_build_object('status','invalid'); END IF;
     SELECT * INTO m FROM {schema}.messages WHERE tenant_id=t AND conversation_id=c.id AND id=(item->>'id')::uuid AND sequence=(item->>'sequence')::bigint FOR SHARE;
     IF NOT FOUND THEN RETURN jsonb_build_object('status','conflict'); END IF;
     entry:={schema}._conversation_message(m);
     IF entry->>'_snapshot' IS DISTINCT FROM item->>'snapshot' THEN RETURN jsonb_build_object('status','conflict'); END IF;
     items:=items||jsonb_build_array(entry);
    END LOOP;
   END IF;
   result:=jsonb_build_object('conversation',{schema}._conversation_json(c),'messages',items,'next_after',next_after);
  END IF;
 END IF;
 context:={schema}.authorization_call(token,'finish',CASE WHEN operation IN ('create','append','feedback') THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'Conversation authority expired.' USING ERRCODE='40001'; END IF;
 RETURN result||jsonb_build_object('status','ok','tenant_id',t,'principal_id',p,'authority_expires_at',
  (SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) FROM {schema}.sessions WHERE token_hash=token));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR not_null_violation OR check_violation OR unique_violation OR foreign_key_violation OR invalid_parameter_value THEN RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}.conversation_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.conversation_call(text,text,jsonb) TO {access_role};

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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','conversations','runs','messages','source_dependencies','feedback','conversation_requests',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','conversations','runs','messages','source_dependencies','feedback','conversation_requests','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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
                        AND NOT (f.proname='identity_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='operation text, payload jsonb')
                        AND NOT (f.proname='authorization_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
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


