-- BD-FE-05: current authorized query only; no new business writer.
CREATE FUNCTION {schema}.workspace_call(view text,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $fn$
#variable_conflict use_column
<<workspace>>
DECLARE t uuid:={schema}.context_tenant();a uuid:={schema}.context_actor();id uuid:=(payload->>'claim_id')::uuid;after_id uuid:=(payload->>'after')::uuid;lim integer:=(payload->>'limit')::integer;
 c {schema}.claims%ROWTYPE;result jsonb;rows jsonb;entry jsonb;expiry numeric:=(payload->>'expires_at')::numeric;
BEGIN
 IF NOT {schema}.runtime_ready() THEN RAISE EXCEPTION 'Service runtime rejected';END IF;
 IF t IS NULL OR a IS NULL OR view IS NULL OR view NOT IN ('mine','approvals') OR lim NOT BETWEEN 1 AND 20 OR expiry IS NULL OR expiry<=extract(epoch FROM clock_timestamp()) OR expiry>extract(epoch FROM clock_timestamp())+65 OR NOT EXISTS(SELECT 1 FROM {schema}.tenant_registry WHERE tenant_id=t AND active) THEN RETURN jsonb_build_object('status','invalid');END IF;
 IF id IS NOT NULL THEN
  SELECT * INTO c FROM {schema}.claims WHERE tenant_id=t AND claims.id=workspace.id FOR SHARE;
  IF NOT FOUND OR (view='mine' AND c.owner_principal_ref<>a) OR (view='approvals' AND (c.owner_principal_ref=a OR c.state<>'pending_approval' OR NOT EXISTS(SELECT 1 FROM {schema}.claim_stages WHERE tenant_id=t AND claim_id=c.id AND revision_no=c.current_revision_no AND stage_no=c.active_stage_no AND approver_principal_ref=a AND stage_state='pending'))) THEN RETURN jsonb_build_object('status','missing');END IF;
  result:={schema}.claim_call('read',jsonb_build_object('claim_id',c.id,'expires_at',expiry));
  IF result->>'status' IS DISTINCT FROM 'ok' THEN RETURN result;END IF;
  entry:=result->'claim';
  entry:=entry||jsonb_build_object('trip',(SELECT jsonb_build_object('id',tr.id,'start_date',tr.start_date,'end_date',tr.end_date,'employee_ref',tr.employee_ref,'state',tr.state) FROM {schema}.trips tr WHERE tenant_id=t AND tr.id=c.trip_id),
   'rules',(SELECT jsonb_build_object('timezone',pkg.timezone,'rules',pkg.rules_json) FROM {schema}.service_policy_packages pkg WHERE tenant_id=t AND package_id=c.policy_id AND package_version=c.policy_version AND digest=c.policy_digest),
   'decisions',(SELECT coalesce(jsonb_agg(jsonb_build_object('stage',d.stage_no,'actor_id',d.actor_ref,'decision',d.decision,'reason',d.reason,'created_at',d.created_at) ORDER BY d.stage_no),'[]'::jsonb) FROM {schema}.decisions d WHERE tenant_id=t AND claim_id=c.id AND revision_no=c.current_revision_no));
  result:=jsonb_build_object('claim',entry);
 ELSE
  -- Lock headers before snapshotting current stages/revision. Writers use the
  -- same header-first order, so stage changes cannot slip within this read UoW.
  PERFORM cc.id FROM {schema}.claims cc WHERE cc.tenant_id=t AND (after_id IS NULL OR cc.id>=after_id) AND ((view='mine' AND cc.owner_principal_ref=a) OR (view='approvals' AND cc.owner_principal_ref<>a AND cc.state='pending_approval' AND EXISTS(SELECT 1 FROM {schema}.claim_stages ss WHERE ss.tenant_id=t AND ss.claim_id=cc.id AND ss.revision_no=cc.current_revision_no AND ss.stage_no=cc.active_stage_no AND ss.approver_principal_ref=a AND ss.stage_state='pending'))) ORDER BY cc.id LIMIT lim+2 FOR SHARE;
  IF after_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {schema}.claims cc WHERE tenant_id=t AND cc.id=after_id AND
   ((view='mine' AND owner_principal_ref=a) OR (view='approvals' AND owner_principal_ref<>a AND state='pending_approval' AND EXISTS(SELECT 1 FROM {schema}.claim_stages WHERE tenant_id=t AND claim_id=cc.id AND revision_no=cc.current_revision_no AND stage_no=cc.active_stage_no AND approver_principal_ref=a AND stage_state='pending')))) THEN RETURN jsonb_build_object('status','conflict');END IF;
  SELECT coalesce(jsonb_agg(value ORDER BY cid),'[]'::jsonb) INTO rows FROM (
   SELECT cc.id cid,jsonb_build_object('id',cc.id,'public_reference',cc.public_reference,'owner_id',cc.owner_principal_ref,'state',cc.state,'version',cc.version,'revision',cc.current_revision_no,'active_stage',cc.active_stage_no,'total_minor',r.total_minor,'currency',r.currency,'submitted_at',cc.submitted_at,'needs_action',view='approvals' OR cc.state='needs_information') value
   FROM {schema}.claims cc JOIN {schema}.claim_revisions r ON r.tenant_id=cc.tenant_id AND r.claim_id=cc.id AND r.revision_no=cc.current_revision_no
   WHERE cc.tenant_id=t AND (after_id IS NULL OR cc.id>after_id) AND ((view='mine' AND cc.owner_principal_ref=a) OR (view='approvals' AND cc.owner_principal_ref<>a AND cc.state='pending_approval' AND EXISTS(SELECT 1 FROM {schema}.claim_stages ss WHERE ss.tenant_id=t AND ss.claim_id=cc.id AND ss.revision_no=cc.current_revision_no AND ss.stage_no=cc.active_stage_no AND ss.approver_principal_ref=a AND ss.stage_state='pending')))
   ORDER BY cc.id LIMIT lim+1) q;
  result:=jsonb_build_object('items',CASE WHEN jsonb_array_length(rows)>lim THEN rows-lim ELSE rows END,'next_after',CASE WHEN jsonb_array_length(rows)>lim THEN rows->(lim-1)->>'id' END);
 END IF;
 IF extract(epoch FROM clock_timestamp())>=expiry THEN RAISE EXCEPTION 'Workspace read expired' USING ERRCODE='40001';END IF;
 RETURN result||jsonb_build_object('status','ok');
EXCEPTION WHEN data_exception THEN RETURN jsonb_build_object('status','invalid');
END $fn$;
REVOKE ALL ON FUNCTION {schema}.workspace_call(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.workspace_call(text,jsonb) TO {runtime};
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
    IF (SELECT count(*) FROM {schema}.schema_migrations)<>6 OR NOT EXISTS(
        SELECT 1 FROM {schema}.schema_migrations WHERE version=1 AND name='0001_foundation.sql'
    ) OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=2 AND name='0002_operations.sql'
    ) OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=3 AND name='0003_reference_data.sql'
    ) OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=4 AND name='0004_claims.sql') OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=5 AND name='0005_receipt_candidate.sql') OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=6 AND name='0006_workflow_workspace.sql') THEN RETURN false; END IF;
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
        IF p.proname IN ('context_tenant','context_actor','runtime_ready','operation_call','trip_call','package_call','claim_call','workspace_call') THEN
            IF p.pronargs<>(CASE WHEN p.proname IN ('operation_call','trip_call','package_call','claim_call','workspace_call') THEN 2 ELSE 0 END)
              OR NOT has_function_privilege(r.oid,p.oid,'EXECUTE') OR has_function_privilege(r.oid,p.oid,'EXECUTE WITH GRANT OPTION') THEN RETURN false;END IF;
        ELSIF p.proname='_claim_base_call' THEN
            IF p.pronargs<>2 OR has_function_privilege(r.oid,p.oid,'EXECUTE') THEN RETURN false;END IF;
        ELSIF p.proname IN ('_claim_immutable','_claim_header_guard','_claim_stage_guard','_claim_ledger_guard') THEN
            IF p.pronargs<>0 OR has_function_privilege(r.oid,p.oid,'EXECUTE') THEN RETURN false;END IF;
        ELSE RETURN false;END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_catalog.pg_proc WHERE pronamespace=n)<>13 THEN RETURN false;END IF;
    RETURN true;
END $fn$;


