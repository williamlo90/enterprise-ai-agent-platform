-- JOB-01. No caller/key is provisioned by a migration or API startup.
CREATE TABLE {schema}.job_callers (
    id uuid PRIMARY KEY,
    credential_hash text NOT NULL UNIQUE CHECK(credential_hash ~ '^[a-f0-9]{{64}}$'),
    capability text NOT NULL CHECK(capability IN ('producer','scheduler','executor')),
    tenant_id uuid REFERENCES {schema}.tenants(id),
    active boolean NOT NULL DEFAULT true, expires_at timestamptz NOT NULL,
    CHECK(capability='scheduler' OR tenant_id IS NOT NULL)
);
CREATE TABLE {schema}.job_scheduler (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), turn bigint NOT NULL DEFAULT 0 CHECK(turn>=0)
);
INSERT INTO {schema}.job_scheduler(singleton) VALUES(true);
CREATE TABLE {schema}.job_tenants (
    tenant_id uuid PRIMARY KEY REFERENCES {schema}.tenants(id), last_turn bigint NOT NULL DEFAULT 0
);
CREATE TABLE {schema}.outbox_jobs (
    tenant_id uuid NOT NULL, id uuid NOT NULL,
    job_type text NOT NULL CHECK(job_type IN ('knowledge.ingest','agent.resume','action.dispatch','action.reconcile')),
    business_key uuid NOT NULL, target_id uuid NOT NULL, expected_version bigint NOT NULL CHECK(expected_version>0),
    actor_id uuid NOT NULL, membership_id uuid NOT NULL, session_id uuid NOT NULL REFERENCES {schema}.sessions(id),
    access_revision bigint NOT NULL CHECK(access_revision>0),
    state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','retry','completed','dead','held')),
    generation bigint NOT NULL DEFAULT 0 CHECK(generation>=0), attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
    retry_limit integer NOT NULL DEFAULT 3 CHECK(retry_limit BETWEEN 1 AND 5),
    worker_id uuid REFERENCES {schema}.job_callers(id), lease_until timestamptz, attempt_deadline timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    error_code text CHECK(error_code IN ('transient','rate_limited','invalid','forbidden','unknown_commit','lease_expired','authority_changed')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,job_type,business_key),
    FOREIGN KEY(tenant_id,membership_id,actor_id) REFERENCES {schema}.memberships(tenant_id,id,principal_id),
    CHECK(attempts<=retry_limit),
    CHECK((state='running' AND worker_id IS NOT NULL AND lease_until IS NOT NULL AND attempt_deadline IS NOT NULL
        AND lease_until<=attempt_deadline) OR (state<>'running' AND worker_id IS NULL AND lease_until IS NULL AND attempt_deadline IS NULL))
);
CREATE INDEX outbox_due ON {schema}.outbox_jobs(tenant_id,next_attempt_at,created_at,id) WHERE state IN ('queued','retry');
CREATE INDEX outbox_leases ON {schema}.outbox_jobs(lease_until) WHERE state='running';
-- Only references are stored. Payloads, credentials and business authority belong to feature owners.
ALTER TABLE {schema}.job_callers ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.job_callers FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.job_scheduler ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.job_scheduler FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.job_tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.job_tenants FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.outbox_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.outbox_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_maintenance ON {schema}.job_callers TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.job_scheduler TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.job_tenants TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.outbox_jobs TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.job_callers,{schema}.job_scheduler,{schema}.job_tenants,{schema}.outbox_jobs FROM PUBLIC,{access_role};

-- Internal helper; never callable by runtime. Locks current authority until transaction end.
CREATE FUNCTION {schema}._job_actor_valid(j {schema}.outbox_jobs) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE s {schema}.sessions%ROWTYPE; m record; moment timestamptz;
BEGIN
    PERFORM id FROM {schema}.principals WHERE id=j.actor_id AND active FOR SHARE;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT * INTO s FROM {schema}.sessions WHERE id=j.session_id FOR SHARE;
    IF NOT FOUND OR s.principal_id IS DISTINCT FROM j.actor_id OR s.active_tenant_id IS DISTINCT FROM j.tenant_id
        OR s.membership_revision IS DISTINCT FROM j.access_revision OR s.revoked_at IS NOT NULL THEN RETURN false; END IF;
    SELECT a.access_revision INTO m FROM {schema}.memberships a JOIN {schema}.tenants t ON t.id=a.tenant_id
        WHERE a.tenant_id=j.tenant_id AND a.id=j.membership_id AND a.principal_id=j.actor_id AND a.active AND t.active
        FOR SHARE OF a,t;
    IF NOT FOUND OR m.access_revision<>j.access_revision THEN RETURN false; END IF;
    moment:=clock_timestamp();
    RETURN moment<s.expires_at AND moment<s.last_seen_at+interval '1800 seconds';
END;
$$;
REVOKE ALL ON FUNCTION {schema}._job_actor_valid({schema}.outbox_jobs) FROM PUBLIC,{access_role};

-- Admission/claim serialize on a short mutex, after admission authority locks.
-- Executor operations never take it. Locked expired running rows reserve capacity.
-- The scheduler learns IDs/type/state/lease only. Executor reads an authority-bound reference.
CREATE FUNCTION {schema}.jobs_call(operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE caller {schema}.job_callers%ROWTYPE; worker {schema}.job_callers%ROWTYPE;
    j {schema}.outbox_jobs%ROWTYPE; old {schema}.outbox_jobs%ROWTYPE;
    moment timestamptz; sequence_no bigint; chosen uuid; actor_valid boolean;
BEGIN
    IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Job caller rejected.'; END IF;
    IF payload IS NULL OR jsonb_typeof(payload)<>'object' OR octet_length(payload::text)>4096
        OR payload->>'credential_hash' IS NULL OR payload->>'credential_hash' !~ '^[a-f0-9]{{64}}$' THEN
        RETURN jsonb_build_object('status','unauthenticated');
    END IF;
    -- Credential and actor locks are independent of scheduling; recheck clocks after waits.
    SELECT * INTO caller FROM {schema}.job_callers WHERE credential_hash=payload->>'credential_hash';
    IF NOT FOUND OR NOT caller.active OR clock_timestamp()>=caller.expires_at THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
    SELECT * INTO caller FROM {schema}.job_callers WHERE credential_hash=payload->>'credential_hash' FOR SHARE;
    IF NOT FOUND OR NOT caller.active OR clock_timestamp()>=caller.expires_at THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;

    IF operation='enqueue' THEN
        IF caller.capability<>'producer' THEN RETURN jsonb_build_object('status','forbidden'); END IF;
        IF NOT payload ?& ARRAY['credential_hash','id','job_type','business_key','target_id','expected_version','actor_id','membership_id','session_id','access_revision','retry_limit']
            OR (SELECT count(*) FROM jsonb_object_keys(payload))<>11
            OR jsonb_typeof(payload->'expected_version')<>'number' OR payload->>'expected_version' !~ '^[1-9][0-9]{{0,17}}$'
            OR jsonb_typeof(payload->'access_revision')<>'number' OR payload->>'access_revision' !~ '^[1-9][0-9]{{0,17}}$'
            OR jsonb_typeof(payload->'retry_limit')<>'number' OR payload->>'retry_limit' !~ '^[1-5]$'
            OR payload->>'job_type' NOT IN ('knowledge.ingest','agent.resume','action.dispatch','action.reconcile') THEN
            RETURN jsonb_build_object('status','invalid');
        END IF;
        j.tenant_id:=caller.tenant_id;j.id:=(payload->>'id')::uuid;j.job_type:=payload->>'job_type';
        j.business_key:=(payload->>'business_key')::uuid;j.target_id:=(payload->>'target_id')::uuid;
        j.expected_version:=(payload->>'expected_version')::bigint;j.actor_id:=(payload->>'actor_id')::uuid;
        j.membership_id:=(payload->>'membership_id')::uuid;j.session_id:=(payload->>'session_id')::uuid;
        j.access_revision:=(payload->>'access_revision')::bigint;j.retry_limit:=(payload->>'retry_limit')::integer;
        IF NOT {schema}._job_actor_valid(j) THEN RETURN jsonb_build_object('status','forbidden'); END IF;
        PERFORM singleton FROM {schema}.job_scheduler FOR UPDATE;
        IF clock_timestamp()>=caller.expires_at OR NOT {schema}._job_actor_valid(j) THEN RETURN jsonb_build_object('status','forbidden'); END IF;
        SELECT * INTO old FROM {schema}.outbox_jobs WHERE tenant_id=j.tenant_id AND job_type=j.job_type AND business_key=j.business_key;
        IF FOUND THEN
            IF ROW(old.target_id,old.expected_version,old.actor_id,old.membership_id,old.session_id,old.access_revision,old.retry_limit)
                IS DISTINCT FROM ROW(j.target_id,j.expected_version,j.actor_id,j.membership_id,j.session_id,j.access_revision,j.retry_limit) THEN
                RETURN jsonb_build_object('status','conflict');
            END IF;
            RETURN jsonb_build_object('status','ok','id',old.id,'replayed',true);
        END IF;
        IF (SELECT count(*) FROM {schema}.outbox_jobs WHERE tenant_id=j.tenant_id AND state IN ('queued','retry','running'))>=100
            OR (SELECT count(*) FROM {schema}.outbox_jobs WHERE state IN ('queued','retry','running'))>=1000 THEN
            RETURN jsonb_build_object('status','capacity');
        END IF;
        IF clock_timestamp()>=caller.expires_at OR NOT {schema}._job_actor_valid(j) THEN RETURN jsonb_build_object('status','forbidden'); END IF;
        INSERT INTO {schema}.job_tenants(tenant_id) VALUES(j.tenant_id) ON CONFLICT DO NOTHING;
        INSERT INTO {schema}.outbox_jobs(tenant_id,id,job_type,business_key,target_id,expected_version,actor_id,membership_id,session_id,access_revision,retry_limit)
            VALUES(j.tenant_id,j.id,j.job_type,j.business_key,j.target_id,j.expected_version,j.actor_id,j.membership_id,j.session_id,j.access_revision,j.retry_limit);
        RETURN jsonb_build_object('status','ok','id',j.id,'replayed',false);
    ELSIF operation='claim' THEN
        IF caller.capability<>'scheduler' THEN RETURN jsonb_build_object('status','forbidden'); END IF;
        IF (SELECT count(*) FROM jsonb_object_keys(payload))<>1 THEN RETURN jsonb_build_object('status','invalid'); END IF;
        PERFORM singleton FROM {schema}.job_scheduler FOR UPDATE;
        IF clock_timestamp()>=caller.expires_at THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
        moment:=clock_timestamp();
        -- Skip rows held by another transaction; a busy tenant must not block B.
        WITH expired AS (SELECT tenant_id,id FROM {schema}.outbox_jobs WHERE state='running' AND lease_until<=moment FOR UPDATE SKIP LOCKED)
        UPDATE {schema}.outbox_jobs r SET state=CASE WHEN attempts>=retry_limit THEN 'dead' ELSE 'retry' END,
            worker_id=NULL,lease_until=NULL,attempt_deadline=NULL,error_code='lease_expired',updated_at=moment,
            next_attempt_at=moment+make_interval(secs=>least(300,power(2,attempts)::integer)*(1+random()*0.25))
            FROM expired e WHERE r.tenant_id=e.tenant_id AND r.id=e.id;
        IF (SELECT count(*) FROM {schema}.outbox_jobs WHERE state='running')>=4 THEN RETURN jsonb_build_object('status','empty'); END IF;
        SELECT r.* INTO j FROM {schema}.outbox_jobs r JOIN {schema}.job_tenants q ON q.tenant_id=r.tenant_id
            WHERE r.state IN ('queued','retry') AND r.next_attempt_at<=moment AND r.attempts<r.retry_limit
                AND (caller.tenant_id IS NULL OR caller.tenant_id=r.tenant_id)
                AND NOT EXISTS(SELECT 1 FROM {schema}.outbox_jobs x WHERE x.tenant_id=r.tenant_id AND x.state='running')
                AND EXISTS(SELECT 1 FROM {schema}.job_callers c WHERE c.tenant_id=r.tenant_id AND c.capability='executor' AND c.active AND c.expires_at>moment)
            ORDER BY q.last_turn,r.next_attempt_at,r.created_at,r.id LIMIT 1 FOR UPDATE OF r SKIP LOCKED;
        IF NOT FOUND THEN RETURN jsonb_build_object('status','empty'); END IF;
        chosen:=j.tenant_id;
        SELECT * INTO worker FROM {schema}.job_callers WHERE tenant_id=chosen AND capability='executor' AND active AND expires_at>clock_timestamp() ORDER BY id LIMIT 1 FOR SHARE;
        IF NOT FOUND THEN RETURN jsonb_build_object('status','empty'); END IF;
        moment:=clock_timestamp();
        IF moment>=least(caller.expires_at,worker.expires_at) THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
        UPDATE {schema}.job_scheduler SET turn=turn+1 RETURNING turn INTO sequence_no;
        UPDATE {schema}.job_tenants SET last_turn=sequence_no WHERE tenant_id=chosen;
        UPDATE {schema}.outbox_jobs SET state='running',attempts=attempts+1,generation=generation+1,worker_id=worker.id,
            attempt_deadline=least(moment+interval '120 seconds',worker.expires_at),
            lease_until=least(moment+interval '15 seconds',worker.expires_at),updated_at=moment
            WHERE tenant_id=j.tenant_id AND id=j.id RETURNING * INTO j;
        RETURN jsonb_build_object('status','ok','id',j.id,'tenant_id',j.tenant_id,'job_type',j.job_type,'worker_id',j.worker_id,'generation',j.generation,'lease_until',j.lease_until,'attempts',j.attempts);
    ELSIF operation='inspect' THEN
        IF caller.capability NOT IN ('producer','executor') OR (SELECT count(*) FROM jsonb_object_keys(payload))<>2 OR NOT payload ? 'id' THEN RETURN jsonb_build_object('status','forbidden'); END IF;
        SELECT * INTO j FROM {schema}.outbox_jobs WHERE tenant_id=caller.tenant_id AND id=(payload->>'id')::uuid;
        IF NOT FOUND THEN RETURN jsonb_build_object('status','missing'); END IF;
        RETURN jsonb_build_object('status','ok','id',j.id,'state',j.state,'generation',j.generation,'attempts',j.attempts,'error_code',j.error_code,'next_attempt_at',j.next_attempt_at);
    END IF;

    IF operation NOT IN ('reference','heartbeat','complete','fail') OR caller.capability<>'executor' THEN RETURN jsonb_build_object('status','forbidden'); END IF;
    IF NOT payload ?& ARRAY['credential_hash','id','generation'] OR (SELECT count(*) FROM jsonb_object_keys(payload))<>(CASE WHEN operation='fail' THEN 4 ELSE 3 END)
        OR jsonb_typeof(payload->'generation')<>'number' OR payload->>'generation' !~ '^[1-9][0-9]{{0,17}}$'
        OR (operation='fail' AND (payload->>'error_code' IS NULL OR payload->>'error_code' NOT IN ('transient','rate_limited','invalid','forbidden','unknown_commit'))) THEN
        RETURN jsonb_build_object('status','invalid');
    END IF;
    SELECT * INTO j FROM {schema}.outbox_jobs WHERE tenant_id=caller.tenant_id AND id=(payload->>'id')::uuid;
    IF NOT FOUND THEN RETURN jsonb_build_object('status','stale'); END IF;
    -- Match AUTH-03: principal -> session -> membership/tenant -> job. No global mutex here.
    actor_valid:={schema}._job_actor_valid(j);
    SELECT * INTO j FROM {schema}.outbox_jobs WHERE tenant_id=caller.tenant_id AND id=(payload->>'id')::uuid FOR UPDATE NOWAIT;
    moment:=clock_timestamp();
    IF NOT FOUND OR j.state<>'running' OR j.worker_id IS DISTINCT FROM caller.id OR j.generation<>(payload->>'generation')::bigint
        OR moment>=least(j.lease_until,j.attempt_deadline,caller.expires_at) THEN RETURN jsonb_build_object('status','stale'); END IF;
    IF NOT actor_valid OR NOT {schema}._job_actor_valid(j) THEN
        UPDATE {schema}.outbox_jobs SET state='held',worker_id=NULL,lease_until=NULL,attempt_deadline=NULL,error_code='authority_changed',updated_at=clock_timestamp()
            WHERE tenant_id=j.tenant_id AND id=j.id;
        RETURN jsonb_build_object('status','held');
    END IF;
    moment:=clock_timestamp();
    IF moment>=least(j.lease_until,j.attempt_deadline,caller.expires_at) THEN RETURN jsonb_build_object('status','stale'); END IF;
    IF operation='reference' THEN
        RETURN jsonb_build_object('status','ok','target_id',j.target_id,'expected_version',j.expected_version,'actor_id',j.actor_id,
            'membership_id',j.membership_id,'session_id',j.session_id,'access_revision',j.access_revision,'job_type',j.job_type,
            'authority_scope','actor_only','lease_until',j.lease_until);
    ELSIF operation='heartbeat' THEN
        UPDATE {schema}.outbox_jobs SET lease_until=least(moment+interval '15 seconds',attempt_deadline,caller.expires_at),updated_at=moment
            WHERE tenant_id=j.tenant_id AND id=j.id RETURNING * INTO j;
        RETURN jsonb_build_object('status','ok','lease_until',j.lease_until);
    ELSIF operation='complete' THEN
        UPDATE {schema}.outbox_jobs SET state='completed',worker_id=NULL,lease_until=NULL,attempt_deadline=NULL,error_code=NULL,updated_at=moment
            WHERE tenant_id=j.tenant_id AND id=j.id;
    ELSE
        UPDATE {schema}.outbox_jobs SET state=CASE WHEN payload->>'error_code' NOT IN ('transient','rate_limited') THEN 'held'
            WHEN attempts>=retry_limit THEN 'dead' ELSE 'retry' END,
            error_code=payload->>'error_code',worker_id=NULL,lease_until=NULL,attempt_deadline=NULL,updated_at=moment,
            next_attempt_at=moment+make_interval(secs=>least(300,power(2,attempts)::integer)*(1+random()*0.25))
            WHERE tenant_id=j.tenant_id AND id=j.id;
    END IF;
    RETURN jsonb_build_object('status','ok');
EXCEPTION WHEN lock_not_available THEN
    RETURN jsonb_build_object('status','unavailable');
WHEN invalid_text_representation OR numeric_value_out_of_range OR not_null_violation OR unique_violation THEN
    RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}.jobs_call(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.jobs_call(text,jsonb) TO {access_role};

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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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

