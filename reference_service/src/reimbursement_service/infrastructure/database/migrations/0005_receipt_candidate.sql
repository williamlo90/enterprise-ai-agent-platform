-- FILE-02: add exact source-derived candidate binding without modifying0001-4.
ALTER FUNCTION {schema}.claim_call(text,jsonb) RENAME TO _claim_base_call;
REVOKE ALL ON FUNCTION {schema}._claim_base_call(text,jsonb) FROM PUBLIC,{runtime};

CREATE FUNCTION {schema}.claim_call(operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $fn$
DECLARE permit jsonb;pin jsonb;normalized jsonb;
BEGIN
 IF NOT {schema}.runtime_ready() THEN RAISE EXCEPTION 'Service runtime rejected';END IF;
 IF operation IN ('submit','information','decision') THEN
  permit:=payload->'permit';
  IF jsonb_typeof(permit) IS DISTINCT FROM 'object' OR jsonb_typeof(permit->'candidate_claim_id') IS DISTINCT FROM 'string'
   OR permit->>'candidate_claim_id' IS DISTINCT FROM payload->>'claim_id'
   OR jsonb_typeof(permit->'operation_key') IS DISTINCT FROM 'string' OR permit->>'operation_key' IS DISTINCT FROM payload->'identity'->>'key'
   OR (permit->>'candidate_claim_id')::uuid='00000000-0000-0000-0000-000000000000'::uuid
   OR (permit->>'candidate_claim_id')::uuid::text IS DISTINCT FROM permit->>'candidate_claim_id'
   OR jsonb_typeof(permit->'receipt_pins') IS DISTINCT FROM 'array'
   THEN RETURN jsonb_build_object('status','forbidden');END IF;
  FOR pin IN SELECT value FROM jsonb_array_elements(permit->'receipt_pins') LOOP
   IF jsonb_typeof(pin) IS DISTINCT FROM 'object' OR pin->>'candidate_claim_id' IS DISTINCT FROM permit->>'candidate_claim_id'
    THEN RETURN jsonb_build_object('status','forbidden');END IF;
  END LOOP;
  SELECT coalesce(jsonb_agg(value-'candidate_claim_id' ORDER BY ordinal),'[]'::jsonb) INTO normalized
   FROM jsonb_array_elements(permit->'receipt_pins') WITH ORDINALITY AS e(value,ordinal);
  -- The private v4 kernel still independently checks canonical hash, all
  -- original permit/pin bindings, constraints and shared REF03 ledger effects.
  permit:=jsonb_set(permit-'candidate_claim_id'-'operation_key','{{receipt_pins}}',normalized);
  payload:=jsonb_set(payload,'{{permit}}',permit);
 END IF;
 RETURN {schema}._claim_base_call(operation,payload);
EXCEPTION WHEN data_exception THEN RETURN jsonb_build_object('status','invalid');
END $fn$;
REVOKE ALL ON FUNCTION {schema}.claim_call(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.claim_call(text,jsonb) TO {runtime};

-- Exact service5 privilege manifest; v4 kernel is private.
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
    IF (SELECT count(*) FROM {schema}.schema_migrations)<>5 OR NOT EXISTS(
        SELECT 1 FROM {schema}.schema_migrations WHERE version=1 AND name='0001_foundation.sql'
    ) OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=2 AND name='0002_operations.sql'
    ) OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=3 AND name='0003_reference_data.sql'
    ) OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=4 AND name='0004_claims.sql') OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=5 AND name='0005_receipt_candidate.sql') THEN RETURN false; END IF;
    FOR c IN SELECT * FROM pg_catalog.pg_class WHERE relnamespace=n AND relkind IN ('r','p','v','m','S','f') LOOP
        IF c.relkind<>'r' OR c.relname NOT IN ('schema_migrations','runtime_binding','tenant_registry','service_operations','service_audit','trips','trip_expense_items','service_policy_packages','package_receipts','claims','claim_revisions','claim_revision_items','claim_receipts','claim_stages','decisions','expense_reservations')
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
    IF (SELECT count(*) FROM pg_catalog.pg_class WHERE relnamespace=n AND relkind='r')<>16 THEN RETURN false; END IF;
    FOR p IN SELECT * FROM pg_catalog.pg_proc WHERE pronamespace=n LOOP
        IF p.proname IN ('context_tenant','context_actor','runtime_ready','operation_call','trip_call','package_call','claim_call') THEN
            IF p.pronargs<>(CASE WHEN p.proname IN ('operation_call','trip_call','package_call','claim_call') THEN 2 ELSE 0 END)
              OR NOT has_function_privilege(r.oid,p.oid,'EXECUTE') OR has_function_privilege(r.oid,p.oid,'EXECUTE WITH GRANT OPTION') THEN RETURN false;END IF;
        ELSIF p.proname='_claim_base_call' THEN
            IF p.pronargs<>2 OR has_function_privilege(r.oid,p.oid,'EXECUTE') THEN RETURN false;END IF;
        ELSIF p.proname IN ('_claim_immutable','_claim_header_guard','_claim_stage_guard','_claim_ledger_guard') THEN
            IF p.pronargs<>0 OR has_function_privilege(r.oid,p.oid,'EXECUTE') THEN RETURN false;END IF;
        ELSE RETURN false;END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_catalog.pg_proc WHERE pronamespace=n)<>12 THEN RETURN false;END IF;
    RETURN true;
END $fn$;


