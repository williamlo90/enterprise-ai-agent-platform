CREATE TABLE {schema}.trips (
    tenant_id uuid NOT NULL REFERENCES {schema}.tenant_registry(tenant_id),
    id uuid NOT NULL,
    owner_principal_ref uuid NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL CHECK(end_date>=start_date),
    employee_ref text NOT NULL CHECK(octet_length(employee_ref) BETWEEN 1 AND 128),
    state text NOT NULL CHECK(state='completed'),
    PRIMARY KEY(tenant_id,id),
    UNIQUE(tenant_id,id,owner_principal_ref)
);
CREATE INDEX trips_owner ON {schema}.trips(tenant_id,owner_principal_ref,id);
CREATE TABLE {schema}.trip_expense_items (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    trip_id uuid NOT NULL,
    owner_principal_ref uuid NOT NULL,
    expense_date date NOT NULL,
    category text NOT NULL CHECK(category ~ '^[a-z][a-z0-9_]{{0,63}}$'),
    declared_amount_minor bigint NOT NULL CHECK(declared_amount_minor>0),
    currency text NOT NULL CHECK(currency IN ('IDR','SGD','USD')),
    PRIMARY KEY(tenant_id,id),
    FOREIGN KEY(tenant_id,trip_id,owner_principal_ref) REFERENCES {schema}.trips(tenant_id,id,owner_principal_ref)
);
CREATE INDEX trip_items_parent ON {schema}.trip_expense_items(tenant_id,trip_id,id);
CREATE TABLE {schema}.service_policy_packages (
    tenant_id uuid NOT NULL REFERENCES {schema}.tenant_registry(tenant_id),
    package_id uuid NOT NULL,
    package_version bigint NOT NULL CHECK(package_version>0),
    digest text NOT NULL CHECK(digest ~ '^[a-f0-9]{{64}}$'),
    schema_version integer NOT NULL CHECK(schema_version=1),
    rules_json jsonb NOT NULL CHECK(jsonb_typeof(rules_json)='object'),
    document_versions jsonb NOT NULL CHECK(jsonb_typeof(document_versions)='array'),
    valid_trip_end_from date NOT NULL,
    valid_trip_end_to date,
    timezone text NOT NULL CHECK(length(timezone) BETWEEN 1 AND 128),
    canonical text NOT NULL CHECK(octet_length(canonical)<=65536),
    copied_at bigint NOT NULL,
    PRIMARY KEY(tenant_id,package_id,package_version),
    UNIQUE(tenant_id,package_id,package_version,digest),
    CHECK(valid_trip_end_to IS NULL OR valid_trip_end_to>valid_trip_end_from)
);
CREATE TABLE {schema}.package_receipts (
    tenant_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    key uuid NOT NULL,
    operation_id uuid NOT NULL,
    request_digest text NOT NULL CHECK(request_digest ~ '^[a-f0-9]{{64}}$'),
    package_id uuid NOT NULL,
    package_version bigint NOT NULL,
    digest text NOT NULL,
    received_at bigint NOT NULL,
    PRIMARY KEY(tenant_id,actor_id,key),
    UNIQUE(tenant_id,actor_id,operation_id),
    FOREIGN KEY(tenant_id,package_id,package_version,digest)
        REFERENCES {schema}.service_policy_packages(tenant_id,package_id,package_version,digest)
);
ALTER TABLE {schema}.trips ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.trips FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.trip_expense_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.trip_expense_items FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.service_policy_packages ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.service_policy_packages FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.package_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.package_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY trips_owner ON {schema}.trips TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY items_owner ON {schema}.trip_expense_items TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY packages_owner ON {schema}.service_policy_packages TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY package_receipts_owner ON {schema}.package_receipts TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.trips,{schema}.trip_expense_items,{schema}.service_policy_packages,{schema}.package_receipts FROM PUBLIC,{runtime};

CREATE FUNCTION {schema}.trip_call(operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $fn$
DECLARE t uuid; a uuid; trip {schema}.trips%ROWTYPE; selected_id uuid; cursor_id uuid;
    page_size integer; rows_json jsonb; next_id uuid; expiry double precision;
BEGIN
    IF NOT {schema}.runtime_ready() THEN RETURN jsonb_build_object('status','unavailable'); END IF;
    t:={schema}.context_tenant();a:={schema}.context_actor();
    IF t IS NULL OR a IS NULL OR NOT EXISTS(SELECT 1 FROM {schema}.tenant_registry WHERE tenant_id=t AND active)
      THEN RETURN jsonb_build_object('status','not_found'); END IF;
    IF jsonb_typeof(payload) IS DISTINCT FROM 'object' OR jsonb_typeof(payload->'expires_at') IS DISTINCT FROM 'number'
      OR payload->>'request_digest' IS DISTINCT FROM current_setting('reimbursement.catalog_digest',true)
      THEN RETURN jsonb_build_object('status','invalid'); END IF;
    expiry:=(payload->>'expires_at')::double precision;
    IF expiry<=extract(epoch FROM clock_timestamp()) OR expiry>extract(epoch FROM clock_timestamp())+65
      THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
    IF operation='detail' THEN
        IF current_setting('reimbursement.catalog_action',true) IS DISTINCT FROM 'trip.read'
          OR (SELECT count(*) FROM jsonb_object_keys(payload))<>3 OR NOT payload ? 'trip_id'
          THEN RETURN jsonb_build_object('status','forbidden'); END IF;
        selected_id:=(payload->>'trip_id')::uuid;
        SELECT * INTO trip FROM {schema}.trips WHERE tenant_id=t AND id=selected_id AND owner_principal_ref=a FOR SHARE;
        IF NOT FOUND THEN RETURN jsonb_build_object('status','not_found'); END IF;
        SELECT coalesce(jsonb_agg(jsonb_build_object('id',i.id,'expense_date',i.expense_date,'category',i.category,
            'declared_amount_minor',i.declared_amount_minor,'currency',i.currency) ORDER BY i.id),'[]'::jsonb)
          INTO rows_json FROM {schema}.trip_expense_items i WHERE i.tenant_id=t AND i.trip_id=selected_id AND i.owner_principal_ref=a;
        IF jsonb_array_length(rows_json)>100 THEN RETURN jsonb_build_object('status','unavailable'); END IF;
        rows_json:=jsonb_build_object('id',trip.id,'start_date',trip.start_date,'end_date',trip.end_date,
            'state',trip.state,'employee_ref',trip.employee_ref,'items',rows_json);
    ELSIF operation='list' THEN
        IF current_setting('reimbursement.catalog_action',true) IS DISTINCT FROM 'trips.list'
          OR (SELECT count(*) FROM jsonb_object_keys(payload))<>4 OR NOT payload ?& ARRAY['after','limit']
          OR jsonb_typeof(payload->'limit') IS DISTINCT FROM 'number' OR payload->>'limit' !~ '^[1-9][0-9]*$'
          THEN RETURN jsonb_build_object('status','forbidden'); END IF;
        page_size:=(payload->>'limit')::integer;
        IF page_size<1 OR page_size>50 THEN RETURN jsonb_build_object('status','invalid'); END IF;
        cursor_id:=(payload->>'after')::uuid;
        IF cursor_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {schema}.trips WHERE tenant_id=t AND id=cursor_id AND owner_principal_ref=a)
          THEN RETURN jsonb_build_object('status','not_found'); END IF;
        SELECT coalesce(jsonb_agg(row_data ORDER BY id),'[]'::jsonb) INTO rows_json FROM (
            SELECT id,jsonb_build_object('id',id,'start_date',start_date,'end_date',end_date,'state',state,'employee_ref',employee_ref) row_data
              FROM {schema}.trips WHERE tenant_id=t AND owner_principal_ref=a AND (cursor_id IS NULL OR id>cursor_id)
              ORDER BY id LIMIT page_size+1) page;
        IF jsonb_array_length(rows_json)>page_size THEN
            next_id:=(rows_json->(page_size-1)->>'id')::uuid;
            rows_json:=rows_json-page_size;
        END IF;
        rows_json:=jsonb_build_object('items',rows_json,'next_cursor',next_id);
    ELSE RETURN jsonb_build_object('status','invalid'); END IF;
    IF expiry<=extract(epoch FROM clock_timestamp()) THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
    RETURN jsonb_build_object('status','ok','result',rows_json);
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN RETURN jsonb_build_object('status','invalid');
END $fn$;
REVOKE ALL ON FUNCTION {schema}.trip_call(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.trip_call(text,jsonb) TO {runtime};

CREATE FUNCTION {schema}.package_call(operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $fn$
DECLARE t uuid;a uuid;key_id uuid;pid uuid;ver bigint;expiry double precision;content jsonb;
    copied {schema}.service_policy_packages%ROWTYPE;receipt {schema}.package_receipts%ROWTYPE;
    lock1 bigint;lock2 bigint;value_digest text;request_hash text;stamp bigint;
BEGIN
    IF NOT {schema}.runtime_ready() THEN RETURN jsonb_build_object('status','unavailable'); END IF;
    t:={schema}.context_tenant();a:={schema}.context_actor();
    IF t IS NULL OR a IS NULL OR NOT EXISTS(SELECT 1 FROM {schema}.tenant_registry WHERE tenant_id=t AND active)
      THEN RETURN jsonb_build_object('status','not_found'); END IF;
    IF operation IS DISTINCT FROM 'receive' OR jsonb_typeof(payload) IS DISTINCT FROM 'object'
      OR (SELECT count(*) FROM jsonb_object_keys(payload))<>6 OR NOT payload ?& ARRAY['key','operation_id','canonical','digest','request_digest','expires_at']
      OR current_setting('reimbursement.catalog_action',true) IS DISTINCT FROM 'policy.publish'
      OR payload->>'request_digest' IS DISTINCT FROM current_setting('reimbursement.catalog_digest',true)
      OR jsonb_typeof(payload->'expires_at') IS DISTINCT FROM 'number'
      THEN RETURN jsonb_build_object('status','forbidden'); END IF;
    expiry:=(payload->>'expires_at')::double precision;
    IF expiry<=extract(epoch FROM clock_timestamp()) OR expiry>extract(epoch FROM clock_timestamp())+65
      THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
    content:=(payload->>'canonical')::jsonb;
    IF jsonb_typeof(content) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(content))<>9
      OR NOT content ?& ARRAY['schema_version','tenant_id','package_id','package_version','valid_trip_end_from','valid_trip_end_to','timezone','document_versions','rules']
      OR (content->>'tenant_id')::uuid IS DISTINCT FROM t OR content->'schema_version' IS DISTINCT FROM '1'::jsonb
      OR jsonb_typeof(content->'package_version') IS DISTINCT FROM 'number' OR content->>'package_version' !~ '^[1-9][0-9]*$'
      OR octet_length(payload->>'canonical')>65536 THEN RETURN jsonb_build_object('status','invalid'); END IF;
    pid:=(content->>'package_id')::uuid;ver:=(content->>'package_version')::bigint;key_id:=(payload->>'key')::uuid;
    value_digest:=encode(sha256(convert_to('reimbursement:rule-package:v1:'||(payload->>'canonical'),'UTF8')),'hex');
    request_hash:=payload->>'request_digest';
    IF value_digest IS DISTINCT FROM payload->>'digest' OR request_hash !~ '^[a-f0-9]{{64}}$'
      THEN RETURN jsonb_build_object('status','conflict'); END IF;
    lock1:=hashtextextended('reimbursement:package:'||t::text||':'||pid::text||':'||ver::text,0);
    lock2:=hashtextextended('reimbursement:package-key:'||t::text||':'||a::text||':'||key_id::text,0);
    PERFORM pg_advisory_xact_lock(least(lock1,lock2));PERFORM pg_advisory_xact_lock(greatest(lock1,lock2));
    IF expiry<=extract(epoch FROM clock_timestamp()) THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
    SELECT * INTO receipt FROM {schema}.package_receipts WHERE tenant_id=t AND actor_id=a AND key=key_id FOR UPDATE;
    IF FOUND THEN
        IF receipt.request_digest<>request_hash OR receipt.package_id<>pid OR receipt.package_version<>ver OR receipt.digest<>value_digest
          OR receipt.operation_id<>(payload->>'operation_id')::uuid
          THEN RETURN jsonb_build_object('status','conflict'); END IF;
    ELSE
        SELECT * INTO copied FROM {schema}.service_policy_packages WHERE tenant_id=t AND package_id=pid AND package_version=ver FOR UPDATE;
        IF FOUND THEN
            IF copied.digest<>value_digest OR copied.canonical<>payload->>'canonical' THEN RETURN jsonb_build_object('status','conflict'); END IF;
        ELSE
            stamp:=floor(extract(epoch FROM clock_timestamp()))::bigint;
            INSERT INTO {schema}.service_policy_packages VALUES(t,pid,ver,value_digest,1,content->'rules',content->'document_versions',
                (content->>'valid_trip_end_from')::date,(content->>'valid_trip_end_to')::date,content->>'timezone',payload->>'canonical',stamp)
                RETURNING * INTO copied;
        END IF;
        INSERT INTO {schema}.package_receipts VALUES(t,a,key_id,(payload->>'operation_id')::uuid,request_hash,pid,ver,value_digest,copied.copied_at) RETURNING * INTO receipt;
    END IF;
    IF expiry<=extract(epoch FROM clock_timestamp()) THEN RAISE EXCEPTION 'Publisher authority expired' USING ERRCODE='40001'; END IF;
    RETURN jsonb_build_object('status','ok','result',jsonb_build_object('package_id',pid,'package_version',ver,'digest',receipt.digest,
        'received_at',receipt.received_at,'state','received'));
EXCEPTION WHEN unique_violation THEN RETURN jsonb_build_object('status','conflict');
WHEN invalid_text_representation OR numeric_value_out_of_range OR datetime_field_overflow THEN RETURN jsonb_build_object('status','invalid');
END $fn$;
REVOKE ALL ON FUNCTION {schema}.package_call(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.package_call(text,jsonb) TO {runtime};

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
    IF (SELECT count(*) FROM {schema}.schema_migrations)<>3 OR NOT EXISTS(
        SELECT 1 FROM {schema}.schema_migrations WHERE version=1 AND name='0001_foundation.sql'
    ) OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=2 AND name='0002_operations.sql'
    ) OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=3 AND name='0003_reference_data.sql'
    ) THEN RETURN false; END IF;
    FOR c IN SELECT * FROM pg_catalog.pg_class WHERE relnamespace=n AND relkind IN ('r','p','v','m','S','f') LOOP
        IF c.relkind<>'r' OR c.relname NOT IN ('schema_migrations','runtime_binding','tenant_registry','service_operations','service_audit','trips','trip_expense_items','service_policy_packages','package_receipts')
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
    IF (SELECT count(*) FROM pg_catalog.pg_class WHERE relnamespace=n AND relkind='r')<>9 THEN RETURN false; END IF;
    FOR p IN SELECT * FROM pg_catalog.pg_proc WHERE pronamespace=n LOOP
        IF p.proname NOT IN ('context_tenant','context_actor','runtime_ready','operation_call','trip_call','package_call') OR p.pronargs<>(CASE WHEN p.proname IN ('operation_call','trip_call','package_call') THEN 2 ELSE 0 END)
          OR NOT has_function_privilege(r.oid,p.oid,'EXECUTE')
          OR has_function_privilege(r.oid,p.oid,'EXECUTE WITH GRANT OPTION') THEN RETURN false; END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_catalog.pg_proc WHERE pronamespace=n)<>6 THEN RETURN false; END IF;
    RETURN true;
END $fn$;


