-- Security journal intent barrier. Immutable metadata only, scoped to current TX.
CREATE TABLE {schema}.security_journal_outbox(tenant_id uuid NOT NULL,event_id text NOT NULL,transaction_id text NOT NULL,event jsonb NOT NULL,PRIMARY KEY(tenant_id,event_id));
ALTER TABLE {schema}.security_journal_outbox ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.security_journal_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY maintenance ON {schema}.security_journal_outbox TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.security_journal_outbox FROM PUBLIC,{access_role};
CREATE FUNCTION {schema}._security_journal_capture() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE r jsonb;o jsonb;t uuid;rid uuid;kind text;action text;revision text;material jsonb;effect text;eid text;targets uuid[];
BEGIN
 r:=CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;o:=CASE WHEN TG_OP='INSERT' THEN '{{}}'::jsonb ELSE to_jsonb(OLD) END;
 kind:=TG_ARGV[0];action:=TG_ARGV[1];rid:=coalesce((r->>TG_ARGV[2])::uuid,(r->'result'->>'id')::uuid);t:=(r->>'tenant_id')::uuid;
 revision:=coalesce(r->>'version',r->>'access_revision',r->>'eligibility_revision',r->>'package_version',r->>'revision_no','1');
 material:=jsonb_build_object('table',TG_TABLE_NAME,'operation',TG_OP,'resource',rid,'revision',revision,'active',r->'active','revoked',r->'revoked_at','state',r->'state','stage',r->'active_stage_no','publication',r->'publication_state','role',r->'role','scope_kind',r->'scope_kind','scope_ref',r->'scope_ref','before_revision',o->'version','before_active',o->'active','before_revoked',o->'revoked_at','before_state',o->'state','before_stage',o->'active_stage_no','before_publication',o->'publication_state');
 IF TG_TABLE_NAME IN ('sessions','principals') THEN
  SELECT array_agg(DISTINCT tenant_id) INTO targets FROM {schema}.memberships WHERE principal_id=CASE WHEN TG_TABLE_NAME='sessions' THEN (r->>'principal_id')::uuid ELSE rid END;
 ELSE targets:=ARRAY[t];END IF;
 IF targets IS NULL THEN IF TG_OP='DELETE' THEN RETURN OLD;END IF;RETURN NEW;END IF;
 FOREACH t IN ARRAY targets LOOP
  effect:=encode(sha256(convert_to(material::text,'UTF8')),'hex');eid:=encode(sha256(convert_to('platform:'||t::text||':'||(pg_postmaster_start_time()::text||':'||pg_current_xact_id()::text)||':'||effect,'UTF8')),'hex');
  INSERT INTO {schema}.security_journal_outbox VALUES(t,eid,(pg_postmaster_start_time()::text||':'||pg_current_xact_id()::text),jsonb_build_object('event_id',eid,'tenant_id',t,'source','platform','resource_kind',kind,'resource_id',rid,'action',action,'revision',revision,'effect_digest',effect)) ON CONFLICT DO NOTHING;
 END LOOP;
 IF TG_OP='DELETE' THEN RETURN OLD;END IF;RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION {schema}._security_journal_capture() FROM PUBLIC,{access_role};
CREATE FUNCTION {schema}.security_journal_events() RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Journal source rejected';END IF;
 IF (SELECT count(*) FROM {schema}.security_journal_outbox WHERE transaction_id=(pg_postmaster_start_time()::text||':'||pg_current_xact_id()::text))>64 THEN RAISE EXCEPTION 'Journal transaction bound exceeded';END IF;
 IF EXISTS(SELECT 1 FROM {schema}.security_journal_outbox o
 JOIN {schema}.privacy_delete_results r ON r.tenant_id=o.tenant_id AND r.result->>'id'=o.event->>'resource_id'
 JOIN {schema}.privacy_conversation_fences f ON f.tenant_id=r.tenant_id AND f.actor_id=r.actor_id AND f.key=r.fence_key
 JOIN {schema}.sessions actor_session ON actor_session.id=f.original_session_id
 JOIN {schema}.privacy_effect_policies p ON p.tenant_id=f.tenant_id AND p.revision=f.policy_revision
 WHERE o.transaction_id=(pg_postmaster_start_time()::text||':'||pg_current_xact_id()::text) AND o.event->>'resource_kind'='conversation'
 AND (actor_session.revoked_at IS NOT NULL OR least(actor_session.expires_at,actor_session.last_seen_at+interval '1800 seconds')<=clock_timestamp()
 OR (p.policy->>'expires_at')::numeric<=extract(epoch FROM clock_timestamp())
 OR EXISTS(SELECT 1 FROM {schema}.privacy_delete_clearances c WHERE c.tenant_id=f.tenant_id AND c.actor_id=f.actor_id AND c.fence_key=f.key AND c.expires_at<=clock_timestamp())
 OR EXISTS(SELECT 1 FROM {schema}.privacy_store_locations l WHERE l.runtime_role=session_user::regrole::oid AND l.store='db' AND l.expires_at<=clock_timestamp()))) THEN RAISE EXCEPTION 'Original erasure deadline crossed';END IF;
 RETURN coalesce((SELECT jsonb_agg(event ORDER BY tenant_id,event_id) FROM {schema}.security_journal_outbox WHERE transaction_id=(pg_postmaster_start_time()::text||':'||pg_current_xact_id()::text)),'[]'::jsonb);
END $$;
REVOKE ALL ON FUNCTION {schema}.security_journal_events() FROM PUBLIC;GRANT EXECUTE ON FUNCTION {schema}.security_journal_events() TO {access_role};
CREATE TRIGGER security_journal_capture AFTER UPDATE ON {schema}.sessions FOR EACH ROW WHEN (OLD.revoked_at IS DISTINCT FROM NEW.revoked_at OR NEW.expires_at<OLD.expires_at OR OLD.token_hash IS DISTINCT FROM NEW.token_hash) EXECUTE FUNCTION {schema}._security_journal_capture('session','deny','id');
CREATE TRIGGER security_journal_capture AFTER UPDATE ON {schema}.principals FOR EACH ROW WHEN (OLD.active IS DISTINCT FROM NEW.active) EXECUTE FUNCTION {schema}._security_journal_capture('principal','deny','id');
CREATE TRIGGER security_journal_capture AFTER UPDATE OR DELETE ON {schema}.memberships FOR EACH ROW EXECUTE FUNCTION {schema}._security_journal_capture('membership','access_change','id');
CREATE TRIGGER security_journal_capture AFTER INSERT OR UPDATE OR DELETE ON {schema}.membership_roles FOR EACH ROW EXECUTE FUNCTION {schema}._security_journal_capture('membership','access_change','membership_id');
CREATE TRIGGER security_journal_capture AFTER INSERT OR DELETE ON {schema}.document_acl FOR EACH ROW EXECUTE FUNCTION {schema}._security_journal_capture('document','access_change','document_id');
CREATE TRIGGER security_journal_capture AFTER UPDATE ON {schema}.documents FOR EACH ROW WHEN (OLD.access_revision IS DISTINCT FROM NEW.access_revision OR OLD.active_version_id IS DISTINCT FROM NEW.active_version_id OR OLD.withdrawn_at IS DISTINCT FROM NEW.withdrawn_at) EXECUTE FUNCTION {schema}._security_journal_capture('document','access_change','id');
CREATE TRIGGER security_journal_capture AFTER UPDATE ON {schema}.policy_packages FOR EACH ROW WHEN (OLD.eligibility_revision IS DISTINCT FROM NEW.eligibility_revision OR OLD.publication_state IS DISTINCT FROM NEW.publication_state OR OLD.allowed_for_new IS DISTINCT FROM NEW.allowed_for_new OR OLD.allowed_for_locked_claims IS DISTINCT FROM NEW.allowed_for_locked_claims) EXECUTE FUNCTION {schema}._security_journal_capture('policy','package_withdrawal','id');
CREATE TRIGGER security_journal_capture AFTER INSERT ON {schema}.privacy_delete_results FOR EACH ROW EXECUTE FUNCTION {schema}._security_journal_capture('conversation','delete','conversation_id');
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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','security_journal_outbox','privacy_delete_clearances','privacy_delete_results','privacy_erasure_contexts','privacy_store_locations','privacy_export_receipts','privacy_fence_releases','privacy_conversation_effect_intents','privacy_conversation_fences','privacy_effect_policies','privacy_effect_decisions','privacy_policies','privacy_policy_decisions','registered_config_refs','admin_requests','admin_events','assisted_requests','action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('security_journal_outbox','privacy_delete_clearances','privacy_delete_results','privacy_erasure_contexts','privacy_store_locations','privacy_export_receipts','privacy_fence_releases','privacy_conversation_effect_intents','privacy_conversation_fences','privacy_effect_policies','privacy_effect_decisions','privacy_policies','privacy_policy_decisions','registered_config_refs','admin_requests','admin_events','assisted_requests','action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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
                        AND NOT (f.proname='security_journal_events' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='')
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





CREATE TRIGGER security_journal_immutable BEFORE UPDATE OR DELETE ON {schema}.security_journal_outbox FOR EACH ROW EXECUTE FUNCTION {schema}.reject_snapshot_mutation();
