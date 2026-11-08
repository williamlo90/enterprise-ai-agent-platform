CREATE TABLE {schema}.service_operations (
    tenant_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    caller text NOT NULL CHECK(caller='platform-dispatcher'),
    kind text NOT NULL CHECK(kind IN ('claim.submit','claim.information','claim.decide')),
    key uuid NOT NULL,
    request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{{64}}$'),
    object_kind text NOT NULL CHECK(object_kind IN ('draft','claim')),
    object_id uuid NOT NULL,
    object_version bigint NOT NULL CHECK(object_version>0),
    state text NOT NULL CHECK(state IN ('committed','rejected','sealed_no_commit')),
    result jsonb NOT NULL,
    result_digest text NOT NULL CHECK(result_digest ~ '^[a-f0-9]{{64}}$'),
    completed_at bigint NOT NULL CHECK(completed_at>0),
    ledger_revision integer NOT NULL DEFAULT 1 CHECK(ledger_revision=1),
    PRIMARY KEY(tenant_id,operation_id),
    UNIQUE(tenant_id,kind,actor_id,key),
    FOREIGN KEY(tenant_id) REFERENCES {schema}.tenant_registry(tenant_id)
);
CREATE TABLE {schema}.service_audit (
    tenant_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    outcome text NOT NULL CHECK(outcome IN ('committed','rejected','sealed_no_commit')),
    occurred_at bigint NOT NULL,
    PRIMARY KEY(tenant_id,operation_id),
    FOREIGN KEY(tenant_id,operation_id) REFERENCES {schema}.service_operations(tenant_id,operation_id)
);
ALTER TABLE {schema}.service_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.service_operations FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.service_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.service_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY operations_owner ON {schema}.service_operations TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY audit_owner ON {schema}.service_audit TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.service_operations,{schema}.service_audit FROM PUBLIC,{runtime};

CREATE FUNCTION {schema}.operation_call(operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $fn$
DECLARE ident jsonb; old {schema}.service_operations%ROWTYPE; by_key {schema}.service_operations%ROWTYPE;
    t uuid; a uuid; op uuid; k uuid; lock1 bigint; lock2 bigint; expiry double precision;
    outcome text; answer jsonb; result_value jsonb; result_hash text; stamp bigint;
BEGIN
    IF NOT {schema}.runtime_ready() THEN RETURN jsonb_build_object('status','unavailable'); END IF;
    t:={schema}.context_tenant();a:={schema}.context_actor();
    IF t IS NULL OR a IS NULL OR NOT EXISTS(SELECT 1 FROM {schema}.tenant_registry WHERE tenant_id=t AND active)
      THEN RETURN jsonb_build_object('status','not_found'); END IF;
    IF jsonb_typeof(payload) IS DISTINCT FROM 'object' OR jsonb_typeof(payload->'expires_at') IS DISTINCT FROM 'number'
      THEN RETURN jsonb_build_object('status','invalid'); END IF;
    expiry:=(payload->>'expires_at')::double precision;
    IF expiry<=extract(epoch FROM clock_timestamp()) OR expiry>extract(epoch FROM clock_timestamp())+65
      THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
    IF operation='lookup' THEN
        IF (SELECT count(*) FROM jsonb_object_keys(payload))<>2 OR NOT payload ? 'operation_id'
          THEN RETURN jsonb_build_object('status','invalid'); END IF;
        op:=(payload->>'operation_id')::uuid;
        SELECT * INTO old FROM {schema}.service_operations WHERE tenant_id=t AND operation_id=op AND actor_id=a FOR SHARE;
        IF NOT FOUND THEN RETURN jsonb_build_object('status','not_found'); END IF;
    ELSIF operation IN ('lock','finish') THEN
        ident:=payload->'identity';
        IF jsonb_typeof(ident)<>'object' OR (SELECT count(*) FROM jsonb_object_keys(ident))<>10
          OR NOT ident ?& ARRAY['tenant_id','operation_id','actor_id','caller','kind','key','request_hash','object_kind','object_id','object_version']
          OR (SELECT count(*) FROM jsonb_object_keys(payload))<>(CASE WHEN operation='lock' THEN 2 ELSE 5 END)
          THEN RETURN jsonb_build_object('status','invalid'); END IF;
        IF (ident->>'tenant_id')::uuid IS DISTINCT FROM t OR (ident->>'actor_id')::uuid IS DISTINCT FROM a
          THEN RETURN jsonb_build_object('status','not_found'); END IF;
        IF ident->>'caller' IS DISTINCT FROM 'platform-dispatcher'
          OR ident->>'kind' NOT IN ('claim.submit','claim.information','claim.decide')
          OR ident->>'object_kind' IS DISTINCT FROM (CASE WHEN ident->>'kind'='claim.submit' THEN 'draft' ELSE 'claim' END)
          OR ident->>'request_hash' !~ '^[a-f0-9]{{64}}$'
          OR jsonb_typeof(ident->'object_version')<>'number' OR ident->>'object_version' !~ '^[1-9][0-9]*$'
          OR (ident->>'object_version')::bigint<=0 THEN RETURN jsonb_build_object('status','invalid'); END IF;
        PERFORM (ident->>'object_id')::uuid;
        op:=(ident->>'operation_id')::uuid;k:=(ident->>'key')::uuid;
        lock1:=hashtextextended('reimbursement:operation:'||t::text||':'||op::text,0);
        lock2:=hashtextextended('reimbursement:key:'||t::text||':'||a::text||':'||(ident->>'kind')||':'||k::text,0);
        PERFORM pg_advisory_xact_lock(least(lock1,lock2));
        PERFORM pg_advisory_xact_lock(greatest(lock1,lock2));
        IF expiry<=extract(epoch FROM clock_timestamp()) THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
        SELECT * INTO old FROM {schema}.service_operations WHERE tenant_id=t AND operation_id=op FOR UPDATE;
        IF FOUND AND old.actor_id<>a THEN RETURN jsonb_build_object('status','not_found'); END IF;
        SELECT * INTO by_key FROM {schema}.service_operations WHERE tenant_id=t AND actor_id=a AND kind=ident->>'kind' AND key=k FOR UPDATE;
        IF by_key.operation_id IS NOT NULL AND by_key.operation_id<>op THEN RETURN jsonb_build_object('status','conflict'); END IF;
        IF old.operation_id IS NOT NULL THEN
            IF (to_jsonb(old)-ARRAY['state','result','result_digest','completed_at','ledger_revision'])<>ident
              THEN RETURN jsonb_build_object('status','conflict'); END IF;
        ELSIF operation='lock' THEN RETURN jsonb_build_object('status','absent');
        ELSE
            outcome:=payload->>'state';result_value:=payload->'result';result_hash:=payload->>'result_digest';
            IF outcome NOT IN ('committed','rejected','sealed_no_commit') OR outcome IS NULL
              OR result_hash IS NULL OR result_hash !~ '^[a-f0-9]{{64}}$' OR jsonb_typeof(result_value)<>'object'
              OR (SELECT count(*) FROM jsonb_object_keys(result_value))<>3 OR NOT result_value ?& ARRAY['ref','version','code']
              OR jsonb_typeof(result_value->'code')<>'string' OR result_value->>'code' !~ '^[a-z][a-z0-9_]{{0,63}}$'
              THEN RETURN jsonb_build_object('status','invalid'); END IF;
            IF (result_value->'ref'='null'::jsonb)<>(result_value->'version'='null'::jsonb)
              THEN RETURN jsonb_build_object('status','invalid'); END IF;
            IF result_value->'ref'<>'null'::jsonb THEN
                PERFORM (result_value->>'ref')::uuid;
                IF jsonb_typeof(result_value->'version')<>'number' OR result_value->>'version' !~ '^[1-9][0-9]*$'
                  THEN RETURN jsonb_build_object('status','invalid'); END IF;
                PERFORM (result_value->>'version')::bigint;
            END IF;
            IF outcome='sealed_no_commit' AND result_value<>jsonb_build_object('ref',NULL,'version',NULL,'code','sealed_no_commit')
              THEN RETURN jsonb_build_object('status','invalid'); END IF;
            IF result_hash<>encode(sha256(convert_to('reimbursement:operation-result:v1:'||
                '{{"code":'||(result_value->'code')::text||',"ref":'||(result_value->'ref')::text||
                ',"version":'||(result_value->'version')::text||'}}','UTF8')),'hex')
              THEN RETURN jsonb_build_object('status','invalid'); END IF;
            stamp:=floor(extract(epoch FROM clock_timestamp()))::bigint;
            INSERT INTO {schema}.service_operations VALUES(t,op,a,ident->>'caller',ident->>'kind',k,
                ident->>'request_hash',ident->>'object_kind',(ident->>'object_id')::uuid,(ident->>'object_version')::bigint,
                outcome,result_value,result_hash,stamp,1) RETURNING * INTO old;
            INSERT INTO {schema}.service_audit VALUES(t,op,a,outcome,stamp);
        END IF;
    ELSE RETURN jsonb_build_object('status','invalid'); END IF;
    IF expiry<=extract(epoch FROM clock_timestamp()) THEN
        RAISE EXCEPTION 'Operation authority expired' USING ERRCODE='40001';
    END IF;
    answer:=jsonb_build_object('identity',to_jsonb(old)-ARRAY['state','result','result_digest','completed_at','ledger_revision'],
        'state',old.state,'result',old.result,'result_digest',old.result_digest,'completed_at',old.completed_at,'ledger_revision',old.ledger_revision);
    RETURN jsonb_build_object('status','ok','terminal',answer);
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN RETURN jsonb_build_object('status','invalid');
END $fn$;
REVOKE ALL ON FUNCTION {schema}.operation_call(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.operation_call(text,jsonb) TO {runtime};

CREATE OR REPLACE FUNCTION {schema}.runtime_ready() RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $fn$
DECLARE r record; n oid; c record; p record; privilege text;
BEGIN
    SELECT roles.* INTO r FROM pg_catalog.pg_roles roles
      JOIN {schema}.runtime_binding b ON b.role_oid=roles.oid AND b.role_name=roles.rolname
      WHERE roles.rolname=SESSION_USER;
    IF NOT FOUND OR NOT r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreatedb
        OR r.rolcreaterole OR r.rolreplication OR r.rolinherit THEN RETURN false; END IF;
    IF EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members WHERE member=r.oid) THEN RETURN false; END IF;
    SELECT oid INTO n FROM pg_catalog.pg_namespace WHERE nspname={schema_name};
    IF NOT has_schema_privilege(r.oid,n,'USAGE') OR has_schema_privilege(r.oid,n,'CREATE')
      OR EXISTS(SELECT 1 FROM pg_catalog.pg_namespace WHERE nspowner=r.oid)
      OR EXISTS(SELECT 1 FROM pg_catalog.pg_class WHERE relowner=r.oid)
      OR EXISTS(SELECT 1 FROM pg_catalog.pg_proc WHERE proowner=r.oid) THEN RETURN false; END IF;
    IF (SELECT count(*) FROM {schema}.schema_migrations)<>2 OR NOT EXISTS(
        SELECT 1 FROM {schema}.schema_migrations WHERE version=1 AND name='0001_foundation.sql'
    ) OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=2 AND name='0002_operations.sql'
    ) THEN RETURN false; END IF;
    FOR c IN SELECT * FROM pg_catalog.pg_class WHERE relnamespace=n AND relkind IN ('r','p','v','m','S','f') LOOP
        IF c.relkind<>'r' OR c.relname NOT IN ('schema_migrations','runtime_binding','tenant_registry','service_operations','service_audit')
          OR NOT c.relrowsecurity OR NOT c.relforcerowsecurity THEN RETURN false; END IF;
        IF has_table_privilege(r.oid,c.oid,'SELECT')<>(c.relname='tenant_registry') THEN RETURN false; END IF;
        FOREACH privilege IN ARRAY ARRAY['INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER','SELECT WITH GRANT OPTION'] LOOP
            IF has_table_privilege(r.oid,c.oid,privilege) THEN RETURN false; END IF;
        END LOOP;
        FOREACH privilege IN ARRAY ARRAY['INSERT','UPDATE','REFERENCES','SELECT WITH GRANT OPTION'] LOOP
            IF has_any_column_privilege(r.oid,c.oid,privilege) THEN RETURN false; END IF;
        END LOOP;
        IF c.relname<>'tenant_registry' AND has_any_column_privilege(r.oid,c.oid,'SELECT') THEN RETURN false; END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_catalog.pg_class WHERE relnamespace=n AND relkind='r')<>5 THEN RETURN false; END IF;
    FOR p IN SELECT * FROM pg_catalog.pg_proc WHERE pronamespace=n LOOP
        IF p.proname NOT IN ('context_tenant','context_actor','runtime_ready','operation_call') OR p.pronargs<>(CASE WHEN p.proname='operation_call' THEN 2 ELSE 0 END)
          OR NOT has_function_privilege(r.oid,p.oid,'EXECUTE')
          OR has_function_privilege(r.oid,p.oid,'EXECUTE WITH GRANT OPTION') THEN RETURN false; END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_catalog.pg_proc WHERE pronamespace=n)<>4 THEN RETURN false; END IF;
    RETURN true;
END $fn$;

