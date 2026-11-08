-- FILE-01: immutable byte intents, owner/current policy, durable inspection jobs.
CREATE TABLE {schema}.files (
    tenant_id uuid NOT NULL,id uuid NOT NULL,owner_principal_id uuid NOT NULL,membership_id uuid NOT NULL,
    session_id uuid NOT NULL REFERENCES {schema}.sessions(id),access_revision bigint NOT NULL CHECK(access_revision>0),
    purpose text NOT NULL CHECK(purpose IN ('knowledge','receipt')),
    collection_id uuid,collection_kind text NOT NULL DEFAULT 'knowledge_collection' CHECK(collection_kind='knowledge_collection'),
    mime text NOT NULL CHECK(mime IN ('application/pdf','image/jpeg','image/png','text/plain','text/markdown')),
    size_bytes bigint NOT NULL CHECK(size_bytes BETWEEN 1 AND 10485760),digest text NOT NULL CHECK(digest ~ '^[a-f0-9]{{64}}$'),
    idempotency_key uuid NOT NULL,object_key text NOT NULL,object_version text,
    scan_state text NOT NULL DEFAULT 'quarantine' CHECK(scan_state IN ('quarantine','ready','rejected')),
    deletion_state text NOT NULL DEFAULT 'active' CHECK(deletion_state IN ('active','retained','deletion_pending','deleted')),
    inspection_job_id uuid,upload_attempts integer NOT NULL DEFAULT 0 CHECK(upload_attempts BETWEEN 0 AND 3),version bigint NOT NULL DEFAULT 1 CHECK(version>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,owner_principal_id,idempotency_key),UNIQUE(tenant_id,object_key),UNIQUE(tenant_id,id,digest),
    FOREIGN KEY(tenant_id,membership_id,owner_principal_id) REFERENCES {schema}.memberships(tenant_id,id,principal_id),
    FOREIGN KEY(tenant_id,collection_kind,collection_id) REFERENCES {schema}.authorization_scopes(tenant_id,scope_kind,scope_ref),
    FOREIGN KEY(tenant_id,inspection_job_id) REFERENCES {schema}.outbox_jobs(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
    CHECK(object_key='quarantine/'||tenant_id::text||'/'||id::text),
    CHECK((object_version IS NULL AND inspection_job_id IS NULL AND scan_state='quarantine') OR
        (object_version IS NOT NULL AND object_version<>'null' AND octet_length(object_version) BETWEEN 1 AND 1024 AND inspection_job_id IS NOT NULL)),
    CHECK((purpose='receipt' AND collection_id IS NULL AND mime IN ('application/pdf','image/jpeg','image/png')) OR
        (purpose='knowledge' AND mime IN ('application/pdf','text/plain','text/markdown')))
);
CREATE INDEX files_owner_created ON {schema}.files(tenant_id,owner_principal_id,created_at,id);
ALTER TABLE {schema}.files ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.files FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_maintenance ON {schema}.files TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.files FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._files_allowed(t uuid,p uuid,purpose text,collection uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT EXISTS(SELECT 1 FROM {schema}.membership_roles r JOIN {schema}.memberships m
    ON m.tenant_id=r.tenant_id AND m.id=r.membership_id WHERE m.tenant_id=t AND m.principal_id=p AND m.active
      AND {schema}._authorization_scope_valid(t,r.role,r.scope_kind,r.scope_ref)
      AND ((purpose='receipt' AND r.role='employee') OR
        (purpose='knowledge' AND r.role='knowledge_manager' AND
            ((r.scope_kind='tenant' AND r.scope_ref=t::text) OR (r.scope_kind='knowledge_collection' AND r.scope_ref=collection::text)))))
    AND (collection IS NULL OR EXISTS(SELECT 1 FROM {schema}.authorization_scopes s WHERE s.tenant_id=t
        AND s.scope_kind='knowledge_collection' AND s.scope_ref=collection AND s.active));
$$;
REVOKE ALL ON FUNCTION {schema}._files_allowed(uuid,uuid,text,uuid) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._file_json(f {schema}.files,replayed boolean DEFAULT false) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('status','ok','id',f.id,'purpose',f.purpose,'mime',f.mime,'size_bytes',f.size_bytes,'scan_state',f.scan_state,
    'version',f.version,'object_key',f.object_key,'object_version',f.object_version,'digest',f.digest,'upload_attempts',f.upload_attempts,'replayed',replayed);
$$;
REVOKE ALL ON FUNCTION {schema}._file_json({schema}.files,boolean) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.files_call(token text,operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE context jsonb;identity jsonb;f {schema}.files%ROWTYPE;person uuid;tenant uuid;member uuid;actor_session uuid;
    job {schema}.outbox_jobs%ROWTYPE;proof jsonb;job_id uuid;moment timestamptz;
BEGIN
    IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'File capability rejected.'; END IF;
    IF payload IS NULL OR jsonb_typeof(payload)<>'object' OR octet_length(payload::text)>4096 THEN RETURN jsonb_build_object('status','invalid'); END IF;
    IF operation IN ('work','processed') THEN
        IF NOT payload ?& ARRAY['credential_hash','job_id','generation'] OR (SELECT count(*) FROM jsonb_object_keys(payload))<>(CASE WHEN operation='work' THEN 3 ELSE 4 END)
            OR (operation='processed' AND jsonb_typeof(payload->'accepted') IS DISTINCT FROM 'boolean') THEN RETURN jsonb_build_object('status','invalid'); END IF;
        proof:={schema}.jobs_call('reference',jsonb_build_object('credential_hash',payload->>'credential_hash','id',payload->>'job_id','generation',payload->'generation'));
        IF proof->>'status' IS DISTINCT FROM 'ok' THEN RETURN proof; END IF;
        SELECT j.* INTO job FROM {schema}.outbox_jobs j JOIN {schema}.job_callers c ON c.tenant_id=j.tenant_id AND c.id=j.worker_id
            WHERE j.id=(payload->>'job_id')::uuid AND j.job_type='file.inspect' AND c.credential_hash=payload->>'credential_hash';
        IF NOT FOUND THEN RETURN jsonb_build_object('status','missing'); END IF;
        SELECT * INTO f FROM {schema}.files WHERE tenant_id=job.tenant_id AND id=job.target_id FOR UPDATE;
        IF NOT FOUND OR f.inspection_job_id IS DISTINCT FROM job.id OR f.owner_principal_id IS DISTINCT FROM job.actor_id
            OR f.session_id IS DISTINCT FROM job.session_id OR f.version<>job.expected_version OR f.scan_state<>'quarantine'
            OR f.object_version IS NULL OR f.deletion_state<>'active'
            OR NOT {schema}._files_allowed(f.tenant_id,f.owner_principal_id,f.purpose,f.collection_id) THEN RETURN jsonb_build_object('status','missing'); END IF;
        IF operation='work' THEN
            proof:={schema}.jobs_call('reference',jsonb_build_object('credential_hash',payload->>'credential_hash','id',job.id,'generation',payload->'generation'));
            IF proof->>'status' IS DISTINCT FROM 'ok' THEN RETURN proof; END IF;
            RETURN {schema}._file_json(f);
        END IF;
        proof:={schema}.jobs_call('complete',jsonb_build_object('credential_hash',payload->>'credential_hash','id',job.id,'generation',payload->'generation'));
        IF proof->>'status' IS DISTINCT FROM 'ok' THEN RETURN proof; END IF;
        UPDATE {schema}.files SET scan_state=CASE WHEN (payload->>'accepted')::boolean THEN 'ready' ELSE 'rejected' END,
            version=version+1,updated_at=clock_timestamp() WHERE tenant_id=f.tenant_id AND id=f.id RETURNING * INTO f;
        RETURN {schema}._file_json(f);
    END IF;
    IF operation NOT IN ('reserve','upload_attempt','bind','status','reference','download') THEN RETURN jsonb_build_object('status','invalid'); END IF;
    context:={schema}.authorization_call(token,'context',CASE WHEN operation IN ('reserve','bind','upload_attempt') THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
    IF context->>'status' IS DISTINCT FROM 'ok' THEN RETURN context; END IF;
    identity:=context->'identity';person:=(identity->>'principal_id')::uuid;tenant:=(identity->>'active_tenant_id')::uuid;member:=(identity->>'active_membership_id')::uuid;
    SELECT id INTO actor_session FROM {schema}.sessions WHERE token_hash=token;
    IF operation='reserve' THEN
        IF NOT payload ?& ARRAY['id','purpose','mime','size_bytes','digest','idempotency_key','collection_id','csrf_hash']
            OR (SELECT count(*) FROM jsonb_object_keys(payload))<>8
            OR jsonb_typeof(payload->'size_bytes') IS DISTINCT FROM 'number' OR payload->>'size_bytes' !~ '^[1-9][0-9]{{0,7}}$'
            OR payload->>'digest' IS NULL OR payload->>'digest' !~ '^[a-f0-9]{{64}}$' THEN RETURN jsonb_build_object('status','invalid'); END IF;
        IF NOT {schema}._files_allowed(tenant,person,payload->>'purpose',(payload->>'collection_id')::uuid) THEN RETURN jsonb_build_object('status','forbidden'); END IF;
        PERFORM singleton FROM {schema}.job_scheduler FOR UPDATE;
        SELECT * INTO f FROM {schema}.files WHERE tenant_id=tenant AND owner_principal_id=person AND idempotency_key=(payload->>'idempotency_key')::uuid;
        IF FOUND THEN
            IF f.deletion_state NOT IN ('active','retained') THEN RETURN jsonb_build_object('status','missing'); END IF;
            IF ROW(f.purpose,f.mime,f.size_bytes,f.digest,f.collection_id) IS DISTINCT FROM ROW(payload->>'purpose',payload->>'mime',(payload->>'size_bytes')::bigint,payload->>'digest',(payload->>'collection_id')::uuid) THEN RETURN jsonb_build_object('status','conflict'); END IF;
            context:={schema}.authorization_call(token,'finish',jsonb_build_object('csrf_hash',payload->>'csrf_hash'));
            IF context->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'File authority expired.' USING ERRCODE='40001'; END IF;
            RETURN {schema}._file_json(f,true)||jsonb_build_object('authority_expires_at',
                (SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) FROM {schema}.sessions WHERE token_hash=token));
        END IF;
        IF (SELECT count(*) FROM {schema}.files WHERE tenant_id=tenant AND owner_principal_id=person)>=100
            OR (SELECT count(*) FROM {schema}.files WHERE tenant_id=tenant)>=1000 OR (SELECT count(*) FROM {schema}.files)>=10000 THEN RETURN jsonb_build_object('status','capacity'); END IF;
        INSERT INTO {schema}.files(tenant_id,id,owner_principal_id,membership_id,session_id,access_revision,purpose,collection_id,mime,size_bytes,digest,idempotency_key,object_key)
        VALUES(tenant,(payload->>'id')::uuid,person,member,actor_session,(identity->>'access_revision')::bigint,payload->>'purpose',(payload->>'collection_id')::uuid,
            payload->>'mime',(payload->>'size_bytes')::bigint,payload->>'digest',(payload->>'idempotency_key')::uuid,'quarantine/'||tenant::text||'/'||(payload->>'id')) RETURNING * INTO f;
    ELSE
        IF NOT payload ? 'id' OR (SELECT count(*) FROM jsonb_object_keys(payload))<>(CASE WHEN operation='bind' THEN 4 WHEN operation='upload_attempt' THEN 3 WHEN operation='download' THEN 2 ELSE 1 END)
            OR (operation IN ('bind','download','upload_attempt') AND (jsonb_typeof(payload->'version') IS DISTINCT FROM 'number' OR payload->>'version' !~ '^[1-9][0-9]{{0,17}}$')) THEN RETURN jsonb_build_object('status','invalid'); END IF;
        SELECT * INTO f FROM {schema}.files WHERE tenant_id=tenant AND id=(payload->>'id')::uuid AND owner_principal_id=person FOR UPDATE;
        IF NOT FOUND OR f.deletion_state NOT IN ('active','retained') OR NOT {schema}._files_allowed(tenant,person,f.purpose,f.collection_id) THEN RETURN jsonb_build_object('status','missing'); END IF;
        IF operation IN ('reference','download') AND f.scan_state<>'ready' THEN RETURN jsonb_build_object('status','missing'); END IF;
        IF operation IN ('bind','download','upload_attempt') AND f.version<>(payload->>'version')::bigint THEN RETURN jsonb_build_object('status','conflict'); END IF;
        IF operation='upload_attempt' THEN
            IF f.object_version IS NOT NULL THEN RETURN jsonb_build_object('status','conflict'); END IF;
            IF f.upload_attempts>=3 THEN RETURN jsonb_build_object('status','capacity'); END IF;
            UPDATE {schema}.files SET upload_attempts=upload_attempts+1 WHERE tenant_id=tenant AND id=f.id RETURNING * INTO f;
        END IF;
        IF operation='bind' THEN
            IF f.object_version IS NOT NULL OR f.scan_state<>'quarantine' OR payload->>'object_version' IS NULL
                OR octet_length(payload->>'object_version') NOT BETWEEN 1 AND 1024 OR payload->>'object_version'='null' THEN RETURN jsonb_build_object('status','conflict'); END IF;
            PERFORM singleton FROM {schema}.job_scheduler FOR UPDATE;
            IF (SELECT count(*) FROM {schema}.outbox_jobs WHERE tenant_id=tenant AND state IN ('queued','retry','running'))>=100
                OR (SELECT count(*) FROM {schema}.outbox_jobs WHERE state IN ('queued','retry','running'))>=1000 THEN RETURN jsonb_build_object('status','capacity'); END IF;
            job_id:=f.id;
            INSERT INTO {schema}.job_tenants(tenant_id) VALUES(tenant) ON CONFLICT DO NOTHING;
            INSERT INTO {schema}.outbox_jobs(tenant_id,id,job_type,business_key,target_id,expected_version,actor_id,membership_id,session_id,access_revision,retry_limit)
                VALUES(tenant,job_id,'file.inspect',f.id,f.id,f.version+1,person,member,actor_session,(identity->>'access_revision')::bigint,5);
            UPDATE {schema}.files SET object_version=payload->>'object_version',inspection_job_id=job_id,session_id=actor_session,
                membership_id=member,access_revision=(identity->>'access_revision')::bigint,version=version+1,updated_at=clock_timestamp()
                WHERE tenant_id=tenant AND id=f.id RETURNING * INTO f;
        END IF;
    END IF;
    context:={schema}.authorization_call(token,CASE WHEN operation='reference' THEN 'context' ELSE 'finish' END,
        CASE WHEN operation IN ('reserve','bind','upload_attempt') THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
    IF context->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'File authority expired.' USING ERRCODE='40001'; END IF;
    RETURN {schema}._file_json(f)||jsonb_build_object('authority_expires_at',
        (SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) FROM {schema}.sessions WHERE token_hash=token));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR not_null_violation OR check_violation OR unique_violation THEN
    RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}.files_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.files_call(text,text,jsonb) TO {access_role};

ALTER TABLE {schema}.outbox_jobs DROP CONSTRAINT outbox_jobs_job_type_check;
ALTER TABLE {schema}.outbox_jobs ADD CONSTRAINT outbox_jobs_job_type_check
    CHECK(job_type IN ('knowledge.ingest','agent.resume','action.dispatch','action.reconcile','file.inspect'));
CREATE OR REPLACE FUNCTION {schema}.jobs_call(operation text,payload jsonb) RETURNS jsonb
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
            OR payload->>'job_type' NOT IN ('knowledge.ingest','agent.resume','action.dispatch','action.reconcile','file.inspect') THEN
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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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

