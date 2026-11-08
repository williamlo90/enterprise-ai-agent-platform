-- CHK-01: run-bound LangGraph storage. Runtime never owns setup or raw tables.
ALTER TABLE {schema}.runs ADD COLUMN checkpoint_job_id uuid, ADD COLUMN checkpoint_job_generation bigint;
CREATE TABLE {schema}.checkpoints (
 tenant_id uuid NOT NULL,run_id uuid NOT NULL,namespace text NOT NULL CHECK(octet_length(namespace)<=1024),id uuid NOT NULL,
 checkpoint_no bigint NOT NULL CHECK(checkpoint_no>0),generation bigint NOT NULL CHECK(generation>0),parent_id uuid,
 state_payload jsonb NOT NULL CHECK(jsonb_typeof(state_payload)='object' AND octet_length(state_payload::text)<=70000),schema_version integer NOT NULL DEFAULT 1 CHECK(schema_version=1),
 PRIMARY KEY(tenant_id,run_id,namespace,id),UNIQUE(tenant_id,run_id,namespace,checkpoint_no),
 FOREIGN KEY(tenant_id,run_id) REFERENCES {schema}.runs(tenant_id,id),
 FOREIGN KEY(tenant_id,run_id,namespace,parent_id) REFERENCES {schema}.checkpoints(tenant_id,run_id,namespace,id)
);
CREATE TABLE {schema}.checkpoint_writes (
 tenant_id uuid NOT NULL,run_id uuid NOT NULL,namespace text NOT NULL,checkpoint_id uuid NOT NULL,task_id text NOT NULL CHECK(length(task_id)<=128),
 write_index integer NOT NULL CHECK(write_index BETWEEN -4 AND 31),task_path text NOT NULL CHECK(length(task_path)<=256),channel text NOT NULL CHECK(length(channel) BETWEEN 1 AND 128),
 value jsonb NOT NULL CHECK(octet_length(value::text)<=65536),generation bigint NOT NULL CHECK(generation>0),
 PRIMARY KEY(tenant_id,run_id,namespace,checkpoint_id,task_id,write_index),
 FOREIGN KEY(tenant_id,run_id,namespace,checkpoint_id) REFERENCES {schema}.checkpoints(tenant_id,run_id,namespace,id)
);
ALTER TABLE {schema}.checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.checkpoints FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.checkpoint_writes ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.checkpoint_writes FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_maintenance ON {schema}.checkpoints TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.checkpoint_writes TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.checkpoints,{schema}.checkpoint_writes FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.checkpoint_call(operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE j {schema}.outbox_jobs%ROWTYPE;r {schema}.runs%ROWTYPE;c {schema}.conversations%ROWTYPE;
 cp {schema}.checkpoints%ROWTYPE;old {schema}.checkpoint_writes%ROWTYPE;
 reference jsonb;result jsonb;entry jsonb;refs jsonb;items jsonb;latest uuid;counter bigint;deadline timestamptz;
 caller {schema}.job_callers%ROWTYPE;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Checkpoint capability rejected.'; END IF;
 IF operation IS NULL OR operation NOT IN ('stage','start','get','list','put','writes','finish') OR jsonb_typeof(payload) IS DISTINCT FROM 'object' OR octet_length(payload::text)>131072
  OR NOT payload ?& ARRAY['credential_hash','tenant_id','run_id','job_id','generation','epoch','sources','namespace','checkpoint_id']
  OR jsonb_typeof(payload->'generation') IS DISTINCT FROM 'number' OR jsonb_typeof(payload->'epoch') IS DISTINCT FROM 'number'
  OR payload->>'generation' !~ '^[1-9][0-9]{{0,17}}$' OR payload->>'epoch' !~ '^(0|[1-9][0-9]{{0,17}})$'
  OR jsonb_typeof(payload->'sources') IS DISTINCT FROM 'array' OR jsonb_array_length(payload->'sources')>8
  OR jsonb_typeof(payload->'namespace') IS DISTINCT FROM 'string' OR length(payload->>'namespace')>256 THEN RETURN jsonb_build_object('status','invalid'); END IF;
 -- jobs_call locks principal -> session -> membership/tenant -> exact job NOWAIT.
 reference:={schema}.jobs_call('reference',jsonb_build_object('credential_hash',payload->>'credential_hash','id',payload->>'job_id','generation',payload->'generation'));
 IF reference->>'status' IS DISTINCT FROM 'ok' THEN RETURN reference; END IF;
 SELECT * INTO caller FROM {schema}.job_callers WHERE credential_hash=payload->>'credential_hash';
 IF caller.tenant_id IS DISTINCT FROM (payload->>'tenant_id')::uuid OR reference->>'job_type'<>'agent.resume' OR reference->>'target_id' IS DISTINCT FROM payload->>'run_id' THEN RETURN jsonb_build_object('status','forbidden'); END IF;
 SELECT * INTO j FROM {schema}.outbox_jobs WHERE tenant_id=caller.tenant_id AND id=(payload->>'job_id')::uuid;
 SELECT * INTO r FROM {schema}.runs WHERE tenant_id=j.tenant_id AND id=j.target_id;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','missing'); END IF;
 SELECT * INTO c FROM {schema}.conversations WHERE tenant_id=r.tenant_id AND id=r.conversation_id FOR UPDATE;
 SELECT * INTO r FROM {schema}.runs WHERE tenant_id=j.tenant_id AND id=j.target_id FOR UPDATE;
 IF r.job_id<>j.id OR r.actor_id<>j.actor_id OR r.membership_id<>j.membership_id OR r.session_id<>j.session_id OR r.access_revision<>j.access_revision
  OR c.owner_principal_id<>r.actor_id OR c.active_run_id IS DISTINCT FROM r.id OR r.state NOT IN ('queued','running','waiting') THEN RETURN jsonb_build_object('status','stale'); END IF;
 IF NOT EXISTS(SELECT 1 FROM {schema}.membership_roles WHERE tenant_id=r.tenant_id AND membership_id=r.membership_id AND role='employee' AND scope_kind='tenant' AND scope_ref=r.tenant_id::text) THEN RETURN jsonb_build_object('status','forbidden'); END IF;
 SELECT least(j.lease_until,j.attempt_deadline,caller.expires_at,s.expires_at,s.last_seen_at+interval '1800 seconds') INTO deadline FROM {schema}.sessions s WHERE s.id=r.session_id;
 IF clock_timestamp()>=deadline THEN RETURN jsonb_build_object('status','stale'); END IF;
 -- Union is monotonic over the entire run, including all parents/intermediates.
 SELECT coalesce(jsonb_agg(DISTINCT value),'[]'::jsonb) INTO refs FROM (
  SELECT jsonb_build_object('system',source_system,'kind',resource_type,'resource_id',resource_id,'version',resource_version,'relation',required_relation,'observed_revision',observed_revision,'page',page,'chunk',chunk) value
   FROM {schema}.source_dependencies WHERE tenant_id=r.tenant_id AND run_id=r.id
  UNION ALL SELECT value FROM jsonb_array_elements(payload->'sources')) q;
 IF jsonb_array_length(refs)>8 THEN RETURN jsonb_build_object('status','capacity'); END IF;
 IF operation='stage' THEN RETURN jsonb_build_object('status','ok','tenant_id',r.tenant_id,'principal_id',r.actor_id,'sources',refs,'authority_expires_at',extract(epoch FROM deadline)); END IF;
 IF operation='start' THEN
  IF r.state='waiting' THEN RETURN jsonb_build_object('status','conflict'); END IF;
  IF jsonb_typeof(payload->'model_ref') IS DISTINCT FROM 'string' OR length(payload->>'model_ref') NOT BETWEEN 1 AND 128 OR jsonb_typeof(payload->'prompt_version') IS DISTINCT FROM 'string' OR length(payload->>'prompt_version') NOT BETWEEN 1 AND 128 OR jsonb_typeof(payload->'config_version') IS DISTINCT FROM 'number' OR payload->>'config_version' !~ '^[1-9][0-9]{{0,17}}$' THEN RETURN jsonb_build_object('status','invalid'); END IF;
  IF r.model_ref IS NOT NULL AND (r.model_ref<>payload->>'model_ref' OR r.prompt_version<>payload->>'prompt_version' OR r.config_version<>(payload->>'config_version')::bigint) THEN RETURN jsonb_build_object('status','conflict'); END IF;
  IF r.checkpoint_job_id IS DISTINCT FROM j.id OR r.checkpoint_job_generation IS DISTINCT FROM j.generation THEN
   UPDATE {schema}.runs SET writer_generation=writer_generation+1,checkpoint_job_id=j.id,checkpoint_job_generation=j.generation,state='running',version=version+1,
    model_ref=payload->>'model_ref',prompt_version=payload->>'prompt_version',config_version=(payload->>'config_version')::bigint,lease_until=j.lease_until WHERE tenant_id=r.tenant_id AND id=r.id RETURNING * INTO r;
  END IF;
 ELSE
  IF r.writer_generation<>(payload->>'epoch')::bigint OR r.checkpoint_job_id IS DISTINCT FROM j.id OR r.checkpoint_job_generation IS DISTINCT FROM j.generation OR r.state<>'running' THEN RETURN jsonb_build_object('status','stale'); END IF;
 END IF;
 -- Persist typed dependencies only after the enclosing application source guard.
 FOR entry IN SELECT value FROM jsonb_array_elements(refs) LOOP
  IF NOT EXISTS(SELECT 1 FROM {schema}.source_dependencies WHERE tenant_id=r.tenant_id AND run_id=r.id AND source_system=entry->>'system' AND resource_type=entry->>'kind' AND resource_id=(entry->>'resource_id')::uuid AND resource_version=entry->>'version' AND required_relation=entry->>'relation' AND observed_revision=entry->>'observed_revision' AND page IS NOT DISTINCT FROM (entry->>'page')::integer AND chunk IS NOT DISTINCT FROM (entry->>'chunk')::integer) THEN
   INSERT INTO {schema}.source_dependencies(tenant_id,id,conversation_id,run_id,source_system,resource_type,resource_id,resource_version,required_relation,observed_revision,page,chunk,platform_document_version_id,platform_file_id)
    VALUES(r.tenant_id,gen_random_uuid(),r.conversation_id,r.id,entry->>'system',entry->>'kind',(entry->>'resource_id')::uuid,entry->>'version',entry->>'relation',entry->>'observed_revision',(entry->>'page')::integer,(entry->>'chunk')::integer,
     CASE WHEN entry->>'system'='platform' AND entry->>'kind'='document_version' THEN (entry->>'resource_id')::uuid END,CASE WHEN entry->>'system'='platform' AND entry->>'kind'='file' THEN (entry->>'resource_id')::uuid END);
  END IF;
 END LOOP;
 IF operation='put' THEN
  SELECT id,checkpoint_no INTO latest,counter FROM {schema}.checkpoints WHERE tenant_id=r.tenant_id AND run_id=r.id AND namespace=payload->>'namespace' ORDER BY checkpoint_no DESC LIMIT 1;
  SELECT * INTO cp FROM {schema}.checkpoints WHERE tenant_id=r.tenant_id AND run_id=r.id AND namespace=payload->>'namespace' AND id=(payload->>'new_id')::uuid;
  IF FOUND THEN
   IF cp.state_payload IS DISTINCT FROM payload->'payload' OR cp.parent_id IS DISTINCT FROM (payload->>'checkpoint_id')::uuid THEN RETURN jsonb_build_object('status','conflict'); END IF;
  ELSE
   IF latest IS DISTINCT FROM (payload->>'checkpoint_id')::uuid THEN RETURN jsonb_build_object('status','conflict'); END IF;
   IF (SELECT count(*) FROM {schema}.checkpoints WHERE tenant_id=r.tenant_id AND run_id=r.id)>=128 THEN RETURN jsonb_build_object('status','capacity'); END IF;
   IF jsonb_typeof(payload->'payload'->'checkpoint') IS DISTINCT FROM 'object' OR jsonb_typeof(payload->'payload'->'metadata') IS DISTINCT FROM 'object' OR payload->'payload'->'checkpoint'->>'id' IS DISTINCT FROM payload->>'new_id' THEN RETURN jsonb_build_object('status','invalid'); END IF;
   INSERT INTO {schema}.checkpoints(tenant_id,run_id,namespace,id,checkpoint_no,generation,parent_id,state_payload) VALUES(r.tenant_id,r.id,payload->>'namespace',(payload->>'new_id')::uuid,coalesce(counter,0)+1,r.writer_generation,(payload->>'checkpoint_id')::uuid,payload->'payload');
  END IF;
 ELSIF operation='writes' THEN
  IF jsonb_typeof(payload->'writes') IS DISTINCT FROM 'array' OR jsonb_array_length(payload->'writes')>32 THEN RETURN jsonb_build_object('status','invalid'); END IF;
  SELECT * INTO cp FROM {schema}.checkpoints WHERE tenant_id=r.tenant_id AND run_id=r.id AND namespace=payload->>'namespace' AND id=(payload->>'checkpoint_id')::uuid;
  SELECT id INTO latest FROM {schema}.checkpoints WHERE tenant_id=r.tenant_id AND run_id=r.id AND namespace=payload->>'namespace' ORDER BY checkpoint_no DESC LIMIT 1;
  -- Pending task writes may arrive after the next superstep checkpoint.
  -- Authority is still the exact current job+generation+run epoch, not the head ID.
  IF cp.id IS NULL THEN RETURN jsonb_build_object('status','conflict','reason','checkpoint'); END IF;
  FOR entry IN SELECT value FROM jsonb_array_elements(payload->'writes') LOOP
   IF jsonb_typeof(entry->'index') IS DISTINCT FROM 'number' OR jsonb_typeof(entry->'channel') IS DISTINCT FROM 'string' OR NOT entry ? 'value'
    OR (entry->>'index')::integer<>(CASE entry->>'channel' WHEN '__error__' THEN -1 WHEN '__scheduled__' THEN -2 WHEN '__interrupt__' THEN -3 WHEN '__resume__' THEN -4 ELSE greatest((entry->>'index')::integer,0) END) THEN RETURN jsonb_build_object('status','invalid'); END IF;
   SELECT * INTO old FROM {schema}.checkpoint_writes WHERE tenant_id=r.tenant_id AND run_id=r.id AND namespace=cp.namespace AND checkpoint_id=cp.id AND task_id=payload->>'task_id' AND write_index=(entry->>'index')::integer;
   IF FOUND THEN
    IF old.channel<>entry->>'channel' OR (old.write_index>=0 AND old.task_path<>payload->>'task_path') THEN RETURN jsonb_build_object('status','conflict','reason','channel_or_path'); END IF;
    IF old.write_index<0 THEN
     UPDATE {schema}.checkpoint_writes SET value=entry->'value',task_path=payload->>'task_path',generation=r.writer_generation WHERE tenant_id=r.tenant_id AND run_id=r.id AND namespace=cp.namespace AND checkpoint_id=cp.id AND task_id=old.task_id AND write_index=old.write_index;
    ELSIF old.value IS DISTINCT FROM entry->'value' THEN RETURN jsonb_build_object('status','conflict','reason','value'); END IF;
   ELSE
    IF (SELECT count(*) FROM {schema}.checkpoint_writes WHERE tenant_id=r.tenant_id AND run_id=r.id AND namespace=cp.namespace AND checkpoint_id=cp.id)>=128 THEN RETURN jsonb_build_object('status','capacity'); END IF;
    INSERT INTO {schema}.checkpoint_writes VALUES(r.tenant_id,r.id,cp.namespace,cp.id,payload->>'task_id',(entry->>'index')::integer,payload->>'task_path',entry->>'channel',entry->'value',r.writer_generation);
   END IF;
  END LOOP;
 ELSIF operation IN ('get','list') THEN
  IF operation='list' AND ((payload->>'limit')::integer NOT BETWEEN 1 AND 20 OR jsonb_typeof(payload->'filter') IS DISTINCT FROM 'object') THEN RETURN jsonb_build_object('status','invalid'); END IF;
  IF operation='list' AND payload->>'before' IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {schema}.checkpoints WHERE tenant_id=r.tenant_id AND run_id=r.id AND namespace=payload->>'namespace' AND id=(payload->>'before')::uuid) THEN RETURN jsonb_build_object('status','missing'); END IF;
  SELECT coalesce(jsonb_agg(item ORDER BY checkpoint_no DESC),'[]'::jsonb) INTO items FROM (
   SELECT q.checkpoint_no,jsonb_build_object('id',q.id,'namespace',q.namespace,'parent_id',q.parent_id,'payload',q.state_payload,'writes',coalesce((SELECT jsonb_agg(jsonb_build_object('task_id',w.task_id,'channel',w.channel,'value',w.value) ORDER BY w.task_path,w.task_id,w.write_index) FROM {schema}.checkpoint_writes w WHERE w.tenant_id=q.tenant_id AND w.run_id=q.run_id AND w.namespace=q.namespace AND w.checkpoint_id=q.id),'[]'::jsonb)) item
   FROM {schema}.checkpoints q WHERE q.tenant_id=r.tenant_id AND q.run_id=r.id AND q.namespace=payload->>'namespace'
    AND (operation='list' OR payload->>'checkpoint_id' IS NULL OR q.id=(payload->>'checkpoint_id')::uuid)
    AND (operation<>'list' OR ((q.state_payload->'metadata') @> (payload->'filter') AND (payload->>'before' IS NULL OR q.checkpoint_no<(SELECT checkpoint_no FROM {schema}.checkpoints WHERE tenant_id=r.tenant_id AND run_id=r.id AND namespace=q.namespace AND id=(payload->>'before')::uuid))))
   ORDER BY q.checkpoint_no DESC LIMIT CASE WHEN operation='get' THEN 1 ELSE (payload->>'limit')::integer END) page;
 ELSIF operation='finish' THEN
  IF payload->>'state' NOT IN ('waiting','completed','failed') THEN RETURN jsonb_build_object('status','invalid'); END IF;
  IF payload->>'state'='waiting' AND NOT EXISTS(SELECT 1 FROM {schema}.checkpoints WHERE tenant_id=r.tenant_id AND run_id=r.id) THEN RETURN jsonb_build_object('status','conflict'); END IF;
  UPDATE {schema}.runs SET state=payload->>'state',version=version+1,lease_until=NULL WHERE tenant_id=r.tenant_id AND id=r.id RETURNING * INTO r;
  IF r.state<>'waiting' THEN UPDATE {schema}.conversations SET active_run_id=NULL,version=version+1 WHERE tenant_id=r.tenant_id AND id=r.conversation_id; END IF;
  reference:={schema}.jobs_call('complete',jsonb_build_object('credential_hash',payload->>'credential_hash','id',j.id,'generation',j.generation));
  IF reference->>'status'<>'ok' THEN RAISE EXCEPTION 'Completion fenced.'; END IF;
 END IF;
 IF clock_timestamp()>=deadline THEN RAISE EXCEPTION 'Checkpoint authority expired.'; END IF;
 RETURN jsonb_build_object('status','ok','epoch',r.writer_generation,'version',r.version,'state',r.state,'items',coalesce(items,'[]'::jsonb),'authority_expires_at',extract(epoch FROM deadline));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR check_violation OR not_null_violation OR foreign_key_violation THEN RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}.checkpoint_call(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.checkpoint_call(text,jsonb) TO {access_role};

CREATE FUNCTION {schema}.checkpoint_owner_call(token text,operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE context jsonb;identity jsonb;r {schema}.runs%ROWTYPE;c {schema}.conversations%ROWTYPE;t uuid;p uuid;sid uuid;refs jsonb;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Run capability rejected.'; END IF;
 IF operation IS NULL OR operation NOT IN ('stage','read','resume') OR jsonb_typeof(payload) IS DISTINCT FROM 'object' OR octet_length(payload::text)>1024 OR NOT payload ? 'id' THEN RETURN jsonb_build_object('status','invalid'); END IF;
 context:={schema}.authorization_call(token,'context',CASE WHEN payload ? 'csrf_hash' THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RETURN context; END IF;
 identity:=context->'identity';t:=(identity->>'active_tenant_id')::uuid;p:=(identity->>'principal_id')::uuid;
 SELECT * INTO r FROM {schema}.runs WHERE tenant_id=t AND id=(payload->>'id')::uuid AND actor_id=p;
 IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM {schema}.membership_roles WHERE tenant_id=t AND membership_id=(identity->>'active_membership_id')::uuid AND role='employee' AND scope_kind='tenant' AND scope_ref=t::text) THEN RETURN jsonb_build_object('status','missing'); END IF;
 SELECT * INTO c FROM {schema}.conversations WHERE tenant_id=t AND id=r.conversation_id AND owner_principal_id=p FOR UPDATE;
 SELECT * INTO r FROM {schema}.runs WHERE tenant_id=t AND id=r.id FOR UPDATE;
 SELECT coalesce(jsonb_agg(jsonb_build_object('system',source_system,'kind',resource_type,'resource_id',resource_id,'version',resource_version,'relation',required_relation,'observed_revision',observed_revision,'page',page,'chunk',chunk)),'[]'::jsonb) INTO refs FROM {schema}.source_dependencies WHERE tenant_id=t AND run_id=r.id;
 IF operation='stage' THEN RETURN jsonb_build_object('status','ok','tenant_id',t,'principal_id',p,'sources',refs); END IF;
 IF operation='resume' THEN
  IF NOT payload ?& ARRAY['csrf_hash','expected_version','job_id'] OR (SELECT count(*) FROM jsonb_object_keys(payload))<>4 OR jsonb_typeof(payload->'expected_version') IS DISTINCT FROM 'number' OR payload->>'expected_version' !~ '^[1-9][0-9]{{0,17}}$' THEN RETURN jsonb_build_object('status','invalid'); END IF;
  IF r.state<>'waiting' OR r.version<>(payload->>'expected_version')::bigint OR c.active_run_id IS DISTINCT FROM r.id THEN RETURN jsonb_build_object('status','conflict'); END IF;
  PERFORM singleton FROM {schema}.job_scheduler FOR UPDATE;
  IF (SELECT count(*) FROM {schema}.outbox_jobs WHERE tenant_id=t AND state IN ('queued','retry','running'))>=100 OR (SELECT count(*) FROM {schema}.outbox_jobs WHERE state IN ('queued','retry','running'))>=1000 THEN RETURN jsonb_build_object('status','capacity'); END IF;
  SELECT id INTO sid FROM {schema}.sessions WHERE token_hash=token;
  INSERT INTO {schema}.outbox_jobs(tenant_id,id,job_type,business_key,target_id,expected_version,actor_id,membership_id,session_id,access_revision,retry_limit)
   VALUES(t,(payload->>'job_id')::uuid,'agent.resume',(payload->>'job_id')::uuid,r.id,r.version+1,p,(identity->>'active_membership_id')::uuid,sid,(identity->>'access_revision')::bigint,3);
  UPDATE {schema}.runs SET state='queued',version=version+1,writer_generation=writer_generation+1,job_id=(payload->>'job_id')::uuid,session_id=sid,membership_id=(identity->>'active_membership_id')::uuid,access_revision=(identity->>'access_revision')::bigint WHERE tenant_id=t AND id=r.id RETURNING * INTO r;
 ELSIF operation='read' AND (SELECT count(*) FROM jsonb_object_keys(payload))<>1 THEN RETURN jsonb_build_object('status','invalid');
 END IF;
 context:={schema}.authorization_call(token,'finish',CASE WHEN payload ? 'csrf_hash' THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'Run authority expired.'; END IF;
 RETURN jsonb_build_object('status','ok','id',r.id,'state',r.state,'version',r.version,'conversation_id',r.conversation_id,'authority_expires_at',(SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) FROM {schema}.sessions WHERE token_hash=token));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR check_violation OR not_null_violation OR foreign_key_violation THEN RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}.checkpoint_owner_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.checkpoint_owner_call(text,text,jsonb) TO {access_role};

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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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
                        AND NOT (f.proname='checkpoint_owner_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
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


