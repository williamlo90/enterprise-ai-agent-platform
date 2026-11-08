-- Security journal intent barrier. Immutable metadata only, scoped to current TX.
CREATE TABLE {schema}.security_journal_outbox(tenant_id uuid NOT NULL,event_id text NOT NULL,transaction_id text NOT NULL,event jsonb NOT NULL,PRIMARY KEY(tenant_id,event_id));
ALTER TABLE {schema}.security_journal_outbox ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.security_journal_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY maintenance ON {schema}.security_journal_outbox TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.security_journal_outbox FROM PUBLIC,{runtime};
CREATE FUNCTION {schema}._security_journal_capture() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE r jsonb;o jsonb;t uuid;rid uuid;kind text;action text;revision text;material jsonb;effect text;eid text;targets uuid[];
BEGIN
 r:=CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;o:=CASE WHEN TG_OP='INSERT' THEN '{{}}'::jsonb ELSE to_jsonb(OLD) END;
 kind:=TG_ARGV[0];action:=TG_ARGV[1];rid:=coalesce((r->>TG_ARGV[2])::uuid,(r->'result'->>'id')::uuid);t:=(r->>'tenant_id')::uuid;
 revision:=coalesce(r->>'version',r->>'access_revision',r->>'eligibility_revision',r->>'package_version',r->>'revision_no','1');
 material:=jsonb_build_object('table',TG_TABLE_NAME,'operation',TG_OP,'resource',rid,'revision',revision,'active',r->'active','revoked',r->'revoked_at','state',r->'state','stage',r->'active_stage_no','publication',r->'publication_state','role',r->'role','scope_kind',r->'scope_kind','scope_ref',r->'scope_ref','before_revision',o->'version','before_active',o->'active','before_revoked',o->'revoked_at','before_state',o->'state','before_stage',o->'active_stage_no','before_publication',o->'publication_state');
 targets:=ARRAY[t];
 FOREACH t IN ARRAY targets LOOP
  effect:=encode(sha256(convert_to(material::text,'UTF8')),'hex');eid:=encode(sha256(convert_to('reimbursement:'||t::text||':'||(pg_postmaster_start_time()::text||':'||pg_current_xact_id()::text)||':'||effect,'UTF8')),'hex');
  INSERT INTO {schema}.security_journal_outbox VALUES(t,eid,(pg_postmaster_start_time()::text||':'||pg_current_xact_id()::text),jsonb_build_object('event_id',eid,'tenant_id',t,'source','reimbursement','resource_kind',kind,'resource_id',rid,'action',action,'revision',revision,'effect_digest',effect)) ON CONFLICT DO NOTHING;
 END LOOP;
 IF TG_OP='DELETE' THEN RETURN OLD;END IF;RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION {schema}._security_journal_capture() FROM PUBLIC,{runtime};
CREATE FUNCTION {schema}.security_journal_events() RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF NOT {schema}.runtime_ready() THEN RAISE EXCEPTION 'Journal source rejected';END IF;
 IF (SELECT count(*) FROM {schema}.security_journal_outbox WHERE transaction_id=(pg_postmaster_start_time()::text||':'||pg_current_xact_id()::text))>64 THEN RAISE EXCEPTION 'Journal transaction bound exceeded';END IF;
 RETURN coalesce((SELECT jsonb_agg(event ORDER BY tenant_id,event_id) FROM {schema}.security_journal_outbox WHERE transaction_id=(pg_postmaster_start_time()::text||':'||pg_current_xact_id()::text)),'[]'::jsonb);
END $$;
REVOKE ALL ON FUNCTION {schema}.security_journal_events() FROM PUBLIC;GRANT EXECUTE ON FUNCTION {schema}.security_journal_events() TO {runtime};
CREATE TRIGGER security_journal_capture AFTER INSERT OR UPDATE ON {schema}.claims FOR EACH ROW EXECUTE FUNCTION {schema}._security_journal_capture('claim','stage_change','id');
CREATE TRIGGER security_journal_capture AFTER INSERT OR UPDATE ON {schema}.claim_stages FOR EACH ROW EXECUTE FUNCTION {schema}._security_journal_capture('claim','stage_change','claim_id');
CREATE TRIGGER security_journal_capture AFTER INSERT OR UPDATE ON {schema}.service_policy_packages FOR EACH ROW EXECUTE FUNCTION {schema}._security_journal_capture('policy','package_withdrawal','package_id');
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
    IF (SELECT count(*) FROM {schema}.schema_migrations)<>7 OR NOT EXISTS(
        SELECT 1 FROM {schema}.schema_migrations WHERE version=1 AND name='0001_foundation.sql'
    ) OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=2 AND name='0002_operations.sql'
    ) OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=3 AND name='0003_reference_data.sql'
    ) OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=4 AND name='0004_claims.sql') OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=5 AND name='0005_receipt_candidate.sql') OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=6 AND name='0006_workflow_workspace.sql') OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=7 AND name='0007_security_journal.sql') THEN RETURN false; END IF;
    FOR c IN SELECT * FROM pg_catalog.pg_class WHERE relnamespace=n AND relkind IN ('r','p','v','m','S','f') LOOP
        IF c.relkind<>'r' OR c.relname NOT IN ('security_journal_outbox','schema_migrations','runtime_binding','tenant_registry','service_operations','service_audit','trips','trip_expense_items','service_policy_packages','package_receipts','claims','claim_revisions','claim_revision_items','claim_receipts','claim_stages','decisions','expense_reservations')
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
    IF (SELECT count(*) FROM pg_catalog.pg_class WHERE relnamespace=n AND relkind='r')<>17 THEN RETURN false; END IF;
    FOR p IN SELECT * FROM pg_catalog.pg_proc WHERE pronamespace=n LOOP
        IF p.proname IN ('context_tenant','context_actor','runtime_ready','operation_call','trip_call','package_call','claim_call','workspace_call','security_journal_events') THEN
            IF p.pronargs<>(CASE WHEN p.proname IN ('operation_call','trip_call','package_call','claim_call','workspace_call') THEN 2 ELSE 0 END)
              OR NOT has_function_privilege(r.oid,p.oid,'EXECUTE') OR has_function_privilege(r.oid,p.oid,'EXECUTE WITH GRANT OPTION') THEN RETURN false;END IF;
        ELSIF p.proname='_claim_base_call' THEN
            IF p.pronargs<>2 OR has_function_privilege(r.oid,p.oid,'EXECUTE') THEN RETURN false;END IF;
        ELSIF p.proname IN ('_claim_immutable','_claim_header_guard','_claim_stage_guard','_claim_ledger_guard','_security_journal_capture') THEN
            IF p.pronargs<>0 OR has_function_privilege(r.oid,p.oid,'EXECUTE') THEN RETURN false;END IF;
        ELSE RETURN false;END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_catalog.pg_proc WHERE pronamespace=n)<>15 THEN RETURN false;END IF;
    RETURN true;
END $fn$;



CREATE TRIGGER security_journal_immutable BEFORE UPDATE OR DELETE ON {schema}.security_journal_outbox FOR EACH ROW EXECUTE FUNCTION {schema}._claim_immutable();
