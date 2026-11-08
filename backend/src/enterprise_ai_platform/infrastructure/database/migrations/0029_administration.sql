-- BD-FE-06: audited scoped administration; no content or credential capability.
CREATE TABLE {schema}.registered_config_refs(kind text NOT NULL CHECK(kind IN ('connector','model')),ref text NOT NULL CHECK(ref ~ '^[a-z][a-z0-9_.-]{{0,127}}$'),label text NOT NULL CHECK(octet_length(label) BETWEEN 1 AND 200),active boolean NOT NULL DEFAULT true,PRIMARY KEY(kind,ref));
INSERT INTO {schema}.registered_config_refs(kind,ref,label) VALUES('connector','reference-reimbursement','Reimbursement reference'),('model','nova-pro-apac-fictional','Nova Pro APAC fictional development');
CREATE TABLE {schema}.admin_requests(tenant_id uuid NOT NULL REFERENCES {schema}.tenants(id),actor_id uuid NOT NULL REFERENCES {schema}.principals(id),key uuid NOT NULL,operation text NOT NULL,request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{{64}}$'),response jsonb NOT NULL CHECK(octet_length(response::text)<=131072),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(tenant_id,actor_id,key));
CREATE TABLE {schema}.admin_events(tenant_id uuid NOT NULL REFERENCES {schema}.tenants(id),id uuid NOT NULL,actor_id uuid NOT NULL REFERENCES {schema}.principals(id),key uuid NOT NULL,operation text NOT NULL,object_id uuid NOT NULL,object_version bigint NOT NULL CHECK(object_version>0),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,actor_id,key),FOREIGN KEY(tenant_id,actor_id,key) REFERENCES {schema}.admin_requests(tenant_id,actor_id,key) DEFERRABLE INITIALLY DEFERRED);
ALTER TABLE {schema}.registered_config_refs ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.registered_config_refs FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.admin_requests ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.admin_requests FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.admin_events ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.admin_events FORCE ROW LEVEL SECURITY;
CREATE POLICY maintenance ON {schema}.registered_config_refs TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.admin_requests TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.admin_events TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.registered_config_refs,{schema}.admin_requests,{schema}.admin_events FROM PUBLIC,{access_role};
CREATE FUNCTION {schema}._admin_immutable() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN RAISE EXCEPTION 'Immutable administration record' USING ERRCODE='23514';END $$;
CREATE TRIGGER admin_request_immutable BEFORE UPDATE OR DELETE ON {schema}.admin_requests FOR EACH ROW EXECUTE FUNCTION {schema}._admin_immutable();
CREATE TRIGGER admin_event_immutable BEFORE UPDATE OR DELETE ON {schema}.admin_events FOR EACH ROW EXECUTE FUNCTION {schema}._admin_immutable();
CREATE FUNCTION {schema}._admin_member(m {schema}.memberships) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('id',m.id,'principal_id',m.principal_id,'active',m.active,'version',m.version,'access_revision',m.access_revision,'roles',coalesce((SELECT jsonb_agg(jsonb_build_object('role',r.role,'scope_kind',r.scope_kind,'scope_ref',r.scope_ref) ORDER BY role,scope_kind,scope_ref) FROM {schema}.membership_roles r WHERE r.tenant_id=m.tenant_id AND r.membership_id=m.id),'[]'::jsonb));
$$;
REVOKE ALL ON FUNCTION {schema}._admin_immutable(),{schema}._admin_member({schema}.memberships) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.admin_call(token text,operation text,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
#variable_conflict use_column
<<admin>>
DECLARE auth jsonb;i jsonb;result jsonb;cfg jsonb;rows jsonb;entry jsonb;prior {schema}.admin_requests%ROWTYPE;m {schema}.memberships%ROWTYPE;pkg {schema}.policy_packages%ROWTYPE;
 t uuid;person uuid;target_person uuid;key uuid;object_id uuid;version bigint;expected bigint;hash text;deadline numeric;lim integer;after_id uuid;after_version bigint;new_tenant uuid;new_member uuid;is_admin boolean;is_owner boolean;is_operator boolean;is_jobs boolean;writing boolean;lock_only boolean:=false;role_result jsonb;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Administration capability rejected';END IF;
 IF operation='lock' THEN
  IF jsonb_typeof(payload) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(payload))<>2 OR NOT payload ?& ARRAY['operation','payload'] THEN RETURN jsonb_build_object('status','invalid');END IF;
  operation:=payload->>'operation';payload:=payload->'payload';lock_only:=true;
 END IF;
 IF operation IS NULL OR operation NOT IN ('members','member','member_create','member_active','role','config','config_write','catalog','profile','scopes','policies','policy','tenants','provision','jobs','audit') OR jsonb_typeof(payload) IS DISTINCT FROM 'object' OR octet_length(payload::text)>262144 THEN RETURN jsonb_build_object('status','invalid');END IF;
 writing:=operation IN ('member_create','member_active','role','config_write','provision');
 IF lock_only AND NOT writing THEN RETURN jsonb_build_object('status','invalid');END IF;
 SELECT principal_id,active_tenant_id INTO person,t FROM {schema}.sessions WHERE token_hash=token;
 IF person IS NULL OR t IS NULL THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
 IF writing THEN
  IF jsonb_typeof(payload->'key') IS DISTINCT FROM 'string' OR (payload->>'key')::uuid='00000000-0000-0000-0000-000000000000'::uuid THEN RETURN jsonb_build_object('status','invalid');END IF;
  key:=(payload->>'key')::uuid;
  -- Same writer lock and principal ordering as AUTH03, before session locks.
  PERFORM pg_advisory_xact_lock(hashtextextended('authorization-role:'||t::text,0));
  IF operation IN ('role','member_active') THEN SELECT principal_id INTO target_person FROM {schema}.memberships WHERE tenant_id=t AND id=(payload->>'id')::uuid;
  ELSIF operation='member_create' THEN target_person:=(payload->>'principal_id')::uuid;
  ELSIF operation='provision' THEN target_person:=(payload->>'initial_admin_id')::uuid;END IF;
  PERFORM id FROM {schema}.principals WHERE id IN (person,target_person) ORDER BY id FOR UPDATE;
 END IF;
 auth:={schema}.authorization_call(token,'context',CASE WHEN writing THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
 IF auth->>'status' IS DISTINCT FROM 'ok' THEN RETURN auth;END IF;
 i:=auth->'identity';
 IF i->>'active_tenant_id' IS DISTINCT FROM t::text OR i->>'principal_id' IS DISTINCT FROM person::text THEN RETURN jsonb_build_object('status','conflict');END IF;
 is_admin:=EXISTS(SELECT 1 FROM jsonb_array_elements(auth->'grants') g WHERE g->>'role'='tenant_admin' AND g->>'scope_kind'='tenant' AND g->>'scope_ref'=t::text);
 is_owner:=EXISTS(SELECT 1 FROM jsonb_array_elements(auth->'grants') g WHERE g->>'role'='process_owner' AND g->>'scope_kind'='process' AND g->>'scope_ref'='reimbursement');
 is_operator:=EXISTS(SELECT 1 FROM jsonb_array_elements(auth->'grants') g WHERE g->>'role'='operator_scoped' AND g->>'scope_kind'='operations' AND g->>'scope_ref'='provisioning');
 is_jobs:=EXISTS(SELECT 1 FROM jsonb_array_elements(auth->'grants') g WHERE g->>'role'='operator_scoped' AND g->>'scope_kind'='operations' AND g->>'scope_ref'='job_metadata');
 IF (operation IN ('tenants','provision') AND NOT is_operator) OR (operation='jobs' AND NOT (is_admin OR is_jobs)) OR (operation IN ('policies','policy') AND NOT (is_admin OR is_owner)) OR (operation NOT IN ('tenants','provision','jobs','policies','policy','profile') AND NOT is_admin) THEN RETURN jsonb_build_object('status','forbidden');END IF;
 SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) INTO deadline FROM {schema}.sessions WHERE token_hash=token;
 IF lock_only THEN RETURN jsonb_build_object('status','ok','authority_expires_at',deadline);END IF;
 IF writing THEN
  hash:=encode(sha256(convert_to(operation||':'||(payload-'csrf_hash')::text,'UTF8')),'hex');
  SELECT * INTO prior FROM {schema}.admin_requests WHERE tenant_id=t AND actor_id=person AND admin_requests.key=admin.key;
  IF FOUND THEN
   IF prior.operation<>operation OR prior.request_hash<>hash THEN RETURN jsonb_build_object('status','conflict');END IF;
   auth:={schema}.authorization_call(token,'finish',jsonb_build_object('csrf_hash',payload->>'csrf_hash'));
   IF auth->>'status' IS DISTINCT FROM 'ok' OR extract(epoch FROM clock_timestamp())>=deadline THEN RAISE EXCEPTION 'Admin replay expired' USING ERRCODE='40001';END IF;
   RETURN prior.response||jsonb_build_object('status','ok','replayed',true,'authority_expires_at',deadline);
  END IF;
  IF operation IN ('member_create','member_active','role','provision') AND target_person=person THEN RETURN jsonb_build_object('status','forbidden');END IF;
  IF operation IN ('member_create','provision') AND NOT EXISTS(SELECT 1 FROM {schema}.principals WHERE id=target_person AND active) THEN RETURN jsonb_build_object('status','missing');END IF;
  IF operation='member_create' THEN
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>3 THEN RETURN jsonb_build_object('status','invalid');END IF;
   IF EXISTS(SELECT 1 FROM {schema}.memberships WHERE tenant_id=t AND principal_id=target_person) THEN RETURN jsonb_build_object('status','conflict');END IF;
   object_id:=md5('admin:member:'||t::text||':'||person::text||':'||key::text)::uuid;
   INSERT INTO {schema}.memberships(tenant_id,id,principal_id) VALUES(t,object_id,target_person);
   INSERT INTO {schema}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) VALUES(t,gen_random_uuid(),object_id,'employee','tenant',t::text,person);
   SELECT * INTO m FROM {schema}.memberships WHERE tenant_id=t AND id=object_id;version:=m.version;
   result:=jsonb_build_object('membership_id',object_id,'membership_version',version);
  ELSIF operation='member_active' THEN
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>5 OR jsonb_typeof(payload->'active') IS DISTINCT FROM 'boolean' OR jsonb_typeof(payload->'expected_version') IS DISTINCT FROM 'number' OR payload->>'expected_version' !~ '^[1-9][0-9]{{0,17}}$' THEN RETURN jsonb_build_object('status','invalid');END IF;
   SELECT * INTO m FROM {schema}.memberships WHERE tenant_id=t AND id=(payload->>'id')::uuid FOR UPDATE;
   IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
   IF m.version<>(payload->>'expected_version')::bigint THEN RETURN jsonb_build_object('status','conflict');END IF;
   IF m.active IS DISTINCT FROM (payload->>'active')::boolean THEN
    UPDATE {schema}.memberships SET active=(payload->>'active')::boolean,version=memberships.version+1,access_revision=memberships.access_revision+1,updated_at=clock_timestamp() WHERE tenant_id=t AND id=m.id RETURNING * INTO m;
    UPDATE {schema}.sessions SET revoked_at=clock_timestamp(),updated_at=clock_timestamp() WHERE principal_id=m.principal_id AND active_tenant_id=t AND revoked_at IS NULL;
   END IF;
   object_id:=m.id;version:=m.version;result:=jsonb_build_object('membership_id',m.id,'membership_version',m.version,'active',m.active);
  ELSIF operation='role' THEN
   role_result:={schema}.authorization_call(token,payload->>'operation',jsonb_build_object('operation',payload->>'operation','membership_id',payload->>'id','expected_version',payload->'expected_version','role',payload->>'role','scope_kind',payload->>'scope_kind','scope_ref',payload->>'scope_ref','idempotency_key',payload->>'key','csrf_hash',payload->>'csrf_hash'));
   IF role_result->>'status' IS DISTINCT FROM 'ok' THEN RETURN role_result;END IF;
   object_id:=(payload->>'id')::uuid;version:=(role_result->>'membership_version')::bigint;result:=role_result-'status';
  ELSIF operation='config_write' THEN
   cfg:=payload->'config';
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>4 OR jsonb_typeof(payload->'expected_version') IS DISTINCT FROM 'number' OR payload->>'expected_version' !~ '^[1-9][0-9]{{0,17}}$' OR jsonb_typeof(cfg) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(cfg))<>6 OR NOT cfg ?& ARRAY['assistant_name','locales','help_contact','instruction_text','connector_ref','model_policy_ref'] THEN RETURN jsonb_build_object('status','invalid');END IF;
   IF EXISTS(SELECT 1 FROM jsonb_each(cfg) e WHERE e.key<>'locales' AND jsonb_typeof(e.value) IS DISTINCT FROM 'string') OR cfg->'locales' NOT IN ('["id"]'::jsonb,'["en"]'::jsonb,'["id","en"]'::jsonb,'["en","id"]'::jsonb) OR NOT {schema}._organization_label(cfg->>'assistant_name',200,800) OR NOT {schema}._organization_label(cfg->>'help_contact',500,2000) OR length(btrim(cfg->>'instruction_text'))=0 OR length(cfg->>'instruction_text')>16000 OR octet_length(cfg->>'instruction_text')>64000 OR translate(cfg->>'instruction_text',E'\r\n\t','') ~ '[[:cntrl:]]' THEN RETURN jsonb_build_object('status','invalid');END IF;
   IF NOT EXISTS(SELECT 1 FROM {schema}.registered_config_refs WHERE kind='connector' AND ref=cfg->>'connector_ref' AND active) OR NOT EXISTS(SELECT 1 FROM {schema}.registered_config_refs WHERE kind='model' AND ref=cfg->>'model_policy_ref' AND active) THEN RETURN jsonb_build_object('status','invalid');END IF;
   SELECT config_version INTO version FROM {schema}.tenants WHERE id=t FOR UPDATE;
   IF version<>(payload->>'expected_version')::bigint THEN RETURN jsonb_build_object('status','conflict');END IF;
   version:=version+1;object_id:=md5('admin:config:'||t::text||':'||key::text)::uuid;
   INSERT INTO {schema}.tenant_configs(tenant_id,id,config_version,assistant_name,locales,help_contact,instruction_text,connector_ref,model_policy_ref,schema_version) VALUES(t,object_id,version,cfg->>'assistant_name',cfg->'locales',cfg->>'help_contact',cfg->>'instruction_text',cfg->>'connector_ref',cfg->>'model_policy_ref',1);
   UPDATE {schema}.tenants SET config_version=admin.version,version=tenants.version+1,updated_at=clock_timestamp() WHERE id=t;
   result:=jsonb_build_object('config_version',version);
  ELSE
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>6 OR payload->>'slug' IS NULL OR payload->>'slug' !~ '^[a-z0-9]+(-[a-z0-9]+)*$' OR length(payload->>'slug')>80 OR NOT {schema}._organization_label(payload->>'name',200,800) OR payload->>'timezone' NOT IN ('Asia/Jakarta','Asia/Singapore','UTC') THEN RETURN jsonb_build_object('status','invalid');END IF;
   IF NOT EXISTS(SELECT 1 FROM {schema}.registered_config_refs WHERE kind='connector' AND ref='reference-reimbursement' AND active) OR NOT EXISTS(SELECT 1 FROM {schema}.registered_config_refs WHERE kind='model' AND ref='nova-pro-apac-fictional' AND active) THEN RETURN jsonb_build_object('status','invalid');END IF;
   new_tenant:=md5('admin:tenant:'||t::text||':'||person::text||':'||key::text)::uuid;new_member:=md5('admin:first-admin:'||new_tenant::text)::uuid;
   INSERT INTO {schema}.tenants(id,slug,name,timezone) VALUES(new_tenant,payload->>'slug',payload->>'name',payload->>'timezone');
   INSERT INTO {schema}.tenant_configs(tenant_id,id,config_version,assistant_name,locales,help_contact,instruction_text,connector_ref,model_policy_ref,schema_version) VALUES(new_tenant,gen_random_uuid(),1,'Enterprise Assistant','["id","en"]'::jsonb,'Hubungi administrator perusahaan.','Gunakan sumber yang diizinkan dan ikuti kewenangan pengguna.','reference-reimbursement','nova-pro-apac-fictional',1);
   INSERT INTO {schema}.memberships(tenant_id,id,principal_id) VALUES(new_tenant,new_member,target_person);
   INSERT INTO {schema}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) VALUES(new_tenant,gen_random_uuid(),new_member,'tenant_admin','tenant',new_tenant::text,person);
   object_id:=new_tenant;version:=1;result:=jsonb_build_object('tenant_id',new_tenant,'initial_admin_membership_id',new_member,'version',1,'config_version',1,'integration_state','requires_service_registration');
  END IF;
  auth:={schema}.authorization_call(token,'finish',jsonb_build_object('csrf_hash',payload->>'csrf_hash'));
  IF auth->>'status' IS DISTINCT FROM 'ok' OR extract(epoch FROM clock_timestamp())>=deadline THEN RAISE EXCEPTION 'Admin mutation expired' USING ERRCODE='40001';END IF;
  INSERT INTO {schema}.admin_requests(tenant_id,actor_id,key,operation,request_hash,response) VALUES(t,person,key,operation,hash,result);
  INSERT INTO {schema}.admin_events(tenant_id,id,actor_id,key,operation,object_id,object_version) VALUES(t,gen_random_uuid(),person,key,operation,object_id,version);
 ELSE
  IF operation IN ('members','scopes','policies','tenants','jobs','audit') THEN
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>2 OR NOT payload ?& ARRAY['after','limit'] OR jsonb_typeof(payload->'limit') IS DISTINCT FROM 'number' OR payload->>'limit' !~ '^[1-9][0-9]?$' THEN RETURN jsonb_build_object('status','invalid');END IF;
   lim:=(payload->>'limit')::integer;IF lim NOT BETWEEN 1 AND 20 THEN RETURN jsonb_build_object('status','invalid');END IF;
   IF operation='policies' AND payload->>'after' IS NOT NULL THEN after_id:=split_part(payload->>'after',':',1)::uuid;after_version:=split_part(payload->>'after',':',2)::bigint;
   ELSE after_id:=(payload->>'after')::uuid;END IF;
  END IF;
  IF operation='members' THEN
   IF after_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {schema}.memberships WHERE tenant_id=t AND id=after_id) THEN RETURN jsonb_build_object('status','conflict');END IF;
   SELECT coalesce(jsonb_agg(value ORDER BY id),'[]'::jsonb) INTO rows FROM (SELECT m.id,{schema}._admin_member(m) value FROM {schema}.memberships m WHERE tenant_id=t AND (after_id IS NULL OR id>after_id) ORDER BY id LIMIT lim+1) q;
  ELSIF operation='member' THEN
   SELECT * INTO m FROM {schema}.memberships WHERE tenant_id=t AND id=(payload->>'id')::uuid FOR SHARE;
   IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;result:=jsonb_build_object('member',{schema}._admin_member(m));
  ELSIF operation IN ('config','profile') THEN
   SELECT jsonb_build_object('config_version',c.config_version,'assistant_name',c.assistant_name,'locales',c.locales,'help_contact',c.help_contact)||CASE WHEN operation='config' THEN jsonb_build_object('instruction_text',c.instruction_text,'connector_ref',c.connector_ref,'model_policy_ref',c.model_policy_ref) ELSE '{{}}'::jsonb END INTO result FROM {schema}.tenant_configs c JOIN {schema}.tenants tt ON tt.id=c.tenant_id AND tt.config_version=c.config_version WHERE c.tenant_id=t FOR SHARE OF tt,c;
  ELSIF operation='catalog' THEN
   result:=jsonb_build_object('connectors',(SELECT coalesce(jsonb_agg(jsonb_build_object('ref',ref,'label',label) ORDER BY ref),'[]'::jsonb) FROM {schema}.registered_config_refs WHERE kind='connector' AND active),'model_policies',(SELECT coalesce(jsonb_agg(jsonb_build_object('ref',ref,'label',label) ORDER BY ref),'[]'::jsonb) FROM {schema}.registered_config_refs WHERE kind='model' AND active));
  ELSIF operation='scopes' THEN
   IF after_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {schema}.authorization_scopes WHERE tenant_id=t AND scope_ref=after_id) THEN RETURN jsonb_build_object('status','conflict');END IF;
   SELECT coalesce(jsonb_agg(value ORDER BY id),'[]'::jsonb) INTO rows FROM (SELECT scope_ref id,jsonb_build_object('id',scope_ref,'active',active,'kind',scope_kind) value FROM {schema}.authorization_scopes WHERE tenant_id=t AND (after_id IS NULL OR scope_ref>after_id) ORDER BY scope_ref LIMIT lim+1) q;
  ELSIF operation IN ('policies','policy') THEN
   IF operation='policy' THEN
    SELECT * INTO pkg FROM {schema}.policy_packages WHERE tenant_id=t AND id=(payload->>'id')::uuid AND package_version=(payload->>'package_version')::bigint FOR SHARE;
    IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
    result:=jsonb_build_object('id',pkg.id,'package_version',pkg.package_version,'version',pkg.version,'digest',pkg.digest,'state',pkg.publication_state,'allowed_for_new',pkg.allowed_for_new,'allowed_for_locked_claims',pkg.allowed_for_locked_claims,'blocked_reason',pkg.blocked_reason,'canonical',CASE WHEN is_owner THEN pkg.canonical END);
   ELSE
    IF after_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {schema}.policy_packages WHERE tenant_id=t AND id=after_id AND package_version=after_version) THEN RETURN jsonb_build_object('status','conflict');END IF;
    SELECT coalesce(jsonb_agg(value ORDER BY id,pv),'[]'::jsonb) INTO rows FROM (SELECT id,package_version pv,jsonb_build_object('id',id,'package_version',package_version,'version',version,'digest',digest,'state',publication_state,'allowed_for_new',allowed_for_new,'allowed_for_locked_claims',allowed_for_locked_claims,'blocked_reason',blocked_reason) value FROM {schema}.policy_packages WHERE tenant_id=t AND (after_id IS NULL OR ROW(id,package_version)>ROW(after_id,after_version)) ORDER BY id,package_version LIMIT lim+1) q;
   END IF;
  ELSIF operation='tenants' THEN
   IF after_id IS NOT NULL AND after_id<>t AND NOT EXISTS(SELECT 1 FROM {schema}.admin_requests rr WHERE rr.tenant_id=t AND rr.actor_id=person AND rr.operation='provision' AND rr.response->>'tenant_id'=after_id::text) THEN RETURN jsonb_build_object('status','conflict');END IF;
   SELECT coalesce(jsonb_agg(value ORDER BY id),'[]'::jsonb) INTO rows FROM (SELECT tt.id,jsonb_build_object('id',tt.id,'slug',tt.slug,'name',tt.name,'timezone',tt.timezone,'active',tt.active,'version',tt.version,'config_version',tt.config_version) value FROM {schema}.tenants tt WHERE (tt.id=t OR EXISTS(SELECT 1 FROM {schema}.admin_requests rr WHERE rr.tenant_id=t AND rr.actor_id=person AND rr.operation='provision' AND rr.response->>'tenant_id'=tt.id::text)) AND (after_id IS NULL OR tt.id>after_id) ORDER BY tt.id LIMIT lim+1) q;
  ELSIF operation='jobs' THEN
   IF after_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {schema}.outbox_jobs WHERE tenant_id=t AND id=after_id) THEN RETURN jsonb_build_object('status','conflict');END IF;
   SELECT coalesce(jsonb_agg(value ORDER BY id),'[]'::jsonb) INTO rows FROM (SELECT j.id,jsonb_build_object('id',id,'job_type',job_type,'state',state,'attempts',attempts,'retry_limit',retry_limit,'error_code',error_code,'next_attempt_at',next_attempt_at) value FROM {schema}.outbox_jobs j WHERE tenant_id=t AND (after_id IS NULL OR id>after_id) ORDER BY id LIMIT lim+1) q;
  ELSE
   IF after_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {schema}.admin_events WHERE tenant_id=t AND id=after_id) THEN RETURN jsonb_build_object('status','conflict');END IF;
   SELECT coalesce(jsonb_agg(value ORDER BY id),'[]'::jsonb) INTO rows FROM (SELECT e.id,jsonb_build_object('id',id,'actor_id',actor_id,'operation',operation,'object_id',object_id,'object_version',object_version,'created_at',created_at) value FROM {schema}.admin_events e WHERE tenant_id=t AND (after_id IS NULL OR id>after_id) ORDER BY id LIMIT lim+1) q;
  END IF;
  IF rows IS NOT NULL THEN result:=jsonb_build_object('items',CASE WHEN jsonb_array_length(rows)>lim THEN rows-lim ELSE rows END,'next_after',CASE WHEN jsonb_array_length(rows)>lim THEN rows->(lim-1)->>'id'||CASE WHEN operation='policies' THEN ':'||(rows->(lim-1)->>'package_version') ELSE '' END END);END IF;
  auth:={schema}.authorization_call(token,'context','{{}}'::jsonb);
  IF auth->>'status' IS DISTINCT FROM 'ok' OR extract(epoch FROM clock_timestamp())>=deadline THEN RAISE EXCEPTION 'Admin read expired' USING ERRCODE='40001';END IF;
 END IF;
 RETURN result||jsonb_build_object('status','ok','authority_expires_at',deadline,'replayed',false);
EXCEPTION WHEN unique_violation THEN RETURN jsonb_build_object('status','conflict');WHEN data_exception OR check_violation OR not_null_violation THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.admin_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.admin_call(text,text,jsonb) TO {access_role};
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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','registered_config_refs','admin_requests','admin_events','assisted_requests','action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('registered_config_refs','admin_requests','admin_events','assisted_requests','action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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



