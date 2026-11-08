-- POL-01: policy publication, live eligibility and scoped trip catalog.
-- Forward-only; prior migrations remain immutable.

-- POL-01: explicit catalog v2 and immutable policy publication/window state.
ALTER TABLE {schema}.internal_caller_credentials DROP CONSTRAINT internal_caller_credentials_allowed_actions_check;
ALTER TABLE {schema}.internal_caller_credentials ADD CONSTRAINT internal_caller_credentials_allowed_actions_check CHECK(
 jsonb_typeof(allowed_actions)='array' AND jsonb_array_length(allowed_actions) BETWEEN 1 AND 8
 AND allowed_actions <@ '["trip.read","claim.read","operation.read","claim.submit","claim.information","claim.decide","trips.list","policy.publish"]'::jsonb);

-- Exact private reference for policy checks; no JWT/bearer is persisted.
CREATE TABLE {schema}.delegation_verification_contexts (
 issuer text NOT NULL,jti uuid NOT NULL,credential_hash text NOT NULL CHECK(credential_hash ~ '^[a-f0-9]{{64}}$'),
 key_kid text NOT NULL,key_hash text NOT NULL CHECK(key_hash ~ '^[a-f0-9]{{64}}$'),
 binding_hash text NOT NULL CHECK(binding_hash ~ '^[a-f0-9]{{64}}$'),context jsonb NOT NULL CHECK(jsonb_typeof(context)='object' AND octet_length(context::text)<=32768),
 expires_at timestamptz NOT NULL,
 PRIMARY KEY(issuer,jti,credential_hash,key_kid,key_hash),
 FOREIGN KEY(issuer,jti) REFERENCES {schema}.delegation_replays(issuer,jti) ON DELETE CASCADE
);
ALTER TABLE {schema}.delegation_verification_contexts ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.delegation_verification_contexts FORCE ROW LEVEL SECURITY;
CREATE POLICY maintenance ON {schema}.delegation_verification_contexts TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.delegation_verification_contexts FROM PUBLIC,{access_role};

CREATE OR REPLACE FUNCTION {schema}.delegation_call(operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE caller {schema}.internal_caller_credentials%ROWTYPE; signing {schema}.delegation_keys%ROWTYPE;
    claims jsonb; person uuid; member record; actor_session {schema}.sessions%ROWTYPE; moment timestamptz; epoch bigint; prior record; replayed boolean:=false;
BEGIN
    IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Internal caller rejected.'; END IF;
    IF payload IS NULL OR jsonb_typeof(payload)<>'object' OR octet_length(payload::text)>16384
        OR payload->>'credential_hash' IS NULL OR payload->>'credential_hash' !~ '^[a-f0-9]{{64}}$' THEN
        RETURN jsonb_build_object('status','unauthenticated');
    END IF;
    SELECT * INTO caller FROM {schema}.internal_caller_credentials WHERE credential_hash=payload->>'credential_hash' FOR SHARE;
    IF NOT FOUND OR caller.active IS NOT TRUE OR clock_timestamp()>=caller.expires_at THEN
        RETURN jsonb_build_object('status','unauthenticated');
    END IF;
    IF operation='caller' THEN
        IF (SELECT count(*) FROM jsonb_object_keys(payload))<>1 THEN RETURN jsonb_build_object('status','invalid'); END IF;
        RETURN jsonb_build_object('status','ok','caller_ref',caller.caller_ref,'audience',caller.audience,'delegation_caller',caller.delegation_caller);
    ELSIF operation='key' THEN
        IF (SELECT count(*) FROM jsonb_object_keys(payload))<>3 OR NOT payload ?& ARRAY['credential_hash','issuer','kid'] THEN
            RETURN jsonb_build_object('status','invalid');
        END IF;
        SELECT * INTO signing FROM {schema}.delegation_keys WHERE issuer=payload->>'issuer' AND kid=payload->>'kid' FOR SHARE;
        IF NOT FOUND OR signing.active IS NOT TRUE OR clock_timestamp()<signing.not_before OR clock_timestamp()>=signing.not_after THEN
            RETURN jsonb_build_object('status','unauthenticated');
        END IF;
        RETURN jsonb_build_object('status','ok','jwk',signing.public_jwk,'key_hash',encode(sha256(convert_to(signing.public_jwk::text,'UTF8')),'hex'));
    ELSIF operation IS DISTINCT FROM 'verify' THEN RETURN jsonb_build_object('status','invalid'); END IF;
    IF (SELECT count(*) FROM jsonb_object_keys(payload))<>5 OR NOT payload ?& ARRAY['credential_hash','claims','key_hash','key_kid','binding_hash']
        OR jsonb_typeof(payload->'claims')<>'object' OR payload->>'binding_hash' IS NULL
        OR payload->>'binding_hash' !~ '^[a-f0-9]{{64}}$' THEN RETURN jsonb_build_object('status','invalid'); END IF;
    claims:=payload->'claims';
    IF NOT claims ?& ARRAY['iss','aud','caller','sub','tenant_id','membership_id','access_revision','action','object_kind','object_id',
        'object_version','request_hash','operation_id','jti','iat','nbf','exp','human_receipt_id','session_id']
        OR (SELECT count(*) FROM jsonb_object_keys(claims))<>(CASE WHEN claims->>'action' IN ('trips.list','policy.publish') THEN 20 ELSE 19 END)
        THEN RETURN jsonb_build_object('status','invalid'); END IF;
    IF claims->>'action' IN ('trips.list','policy.publish') THEN
        IF claims->'canonical_version' IS DISTINCT FROM '2'::jsonb OR NOT claims ? 'canonical_version'
          OR claims->'human_receipt_id' IS DISTINCT FROM 'null'::jsonb
          OR (claims->>'action'='trips.list' AND (claims->>'object_kind' IS DISTINCT FROM 'trip_collection'
             OR claims->'object_id' IS DISTINCT FROM 'null'::jsonb OR claims->'object_version' IS DISTINCT FROM '1'::jsonb))
          OR (claims->>'action'='policy.publish' AND (claims->>'object_kind' IS DISTINCT FROM 'policy_package' OR claims->>'object_id' IS NULL))
          THEN RETURN jsonb_build_object('status','invalid'); END IF;
    ELSIF claims ? 'canonical_version' OR claims->>'action' NOT IN ('trip.read','claim.read','operation.read','claim.submit','claim.information','claim.decide')
      THEN RETURN jsonb_build_object('status','invalid'); END IF;
    SELECT * INTO signing FROM {schema}.delegation_keys WHERE issuer=claims->>'iss' AND kid=payload->>'key_kid'
        AND encode(sha256(convert_to(public_jwk::text,'UTF8')),'hex')=payload->>'key_hash' FOR SHARE;
    IF NOT FOUND OR signing.active IS NOT TRUE OR claims->>'aud' IS DISTINCT FROM caller.audience
        OR claims->>'caller' IS DISTINCT FROM caller.delegation_caller
        OR NOT caller.allowed_actions ? (claims->>'action') THEN RETURN jsonb_build_object('status','forbidden'); END IF;
    person:=(claims->>'sub')::uuid;
    PERFORM id FROM {schema}.principals WHERE id=person AND active FOR SHARE;
    IF NOT FOUND THEN RETURN jsonb_build_object('status','forbidden'); END IF;
    SELECT * INTO actor_session FROM {schema}.sessions WHERE id=(claims->>'session_id')::uuid FOR SHARE;
    IF NOT FOUND OR actor_session.principal_id IS DISTINCT FROM person
        OR actor_session.active_tenant_id IS DISTINCT FROM (claims->>'tenant_id')::uuid
        OR actor_session.membership_revision IS DISTINCT FROM (claims->>'access_revision')::bigint
        OR actor_session.revoked_at IS NOT NULL THEN RETURN jsonb_build_object('status','forbidden'); END IF;
    SELECT m.id,m.access_revision INTO member FROM {schema}.memberships m JOIN {schema}.tenants t ON t.id=m.tenant_id
        WHERE m.tenant_id=(claims->>'tenant_id')::uuid AND m.id=(claims->>'membership_id')::uuid
          AND m.principal_id=person AND m.active AND t.active FOR SHARE OF m,t;
    IF NOT FOUND OR member.access_revision<>(claims->>'access_revision')::bigint THEN RETURN jsonb_build_object('status','forbidden'); END IF;
    IF NOT EXISTS(SELECT 1 FROM {schema}.membership_roles r WHERE r.tenant_id=(claims->>'tenant_id')::uuid
        AND r.membership_id=member.id AND {schema}._authorization_scope_valid(r.tenant_id,r.role,r.scope_kind,r.scope_ref)
        AND ((r.role='employee' AND r.scope_kind='tenant' AND r.scope_ref=(claims->>'tenant_id')
             AND claims->>'action' IN ('trip.read','claim.read','operation.read','claim.submit','claim.information','trips.list'))
          OR (r.role='approver' AND claims->>'action' IN ('claim.read','operation.read','claim.decide'))
          OR (r.role='process_owner' AND r.scope_kind='process' AND r.scope_ref='reimbursement' AND claims->>'action'='policy.publish'))) THEN
        RETURN jsonb_build_object('status','forbidden');
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('delegation-replay:v1',0));
    moment:=clock_timestamp();epoch:=floor(extract(epoch FROM moment))::bigint;
    IF moment>=actor_session.expires_at OR moment>=actor_session.last_seen_at+interval '1800 seconds'
        OR caller.active IS NOT TRUE OR moment>=caller.expires_at OR signing.active IS NOT TRUE OR moment<signing.not_before OR moment>=signing.not_after
        OR (claims->>'iat')::bigint>epoch+5 OR (claims->>'iat')::bigint<=0
        OR claims->>'nbf' IS DISTINCT FROM claims->>'iat' OR (claims->>'exp')::bigint<=epoch
        OR (claims->>'exp')::bigint<=(claims->>'iat')::bigint OR (claims->>'exp')::bigint>(claims->>'iat')::bigint+60
        OR to_timestamp((claims->>'exp')::double precision)>signing.not_after THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
    DELETE FROM {schema}.delegation_replays WHERE expires_at<=moment;
    SELECT * INTO prior FROM {schema}.delegation_replays WHERE issuer=claims->>'iss' AND jti=(claims->>'jti')::uuid;
    IF FOUND THEN
        IF prior.binding_hash IS DISTINCT FROM payload->>'binding_hash' THEN RETURN jsonb_build_object('status','conflict'); END IF;
        replayed:=true;
    ELSE
        IF (SELECT count(*) FROM {schema}.delegation_replays)>=100000 THEN RETURN jsonb_build_object('status','unavailable'); END IF;
        INSERT INTO {schema}.delegation_replays(issuer,jti,binding_hash,operation_id,expires_at)
            VALUES(claims->>'iss',(claims->>'jti')::uuid,payload->>'binding_hash',(claims->>'operation_id')::uuid,to_timestamp((claims->>'exp')::double precision));
    END IF;
    moment:=clock_timestamp();epoch:=floor(extract(epoch FROM moment))::bigint;
    IF moment>=least(actor_session.expires_at,actor_session.last_seen_at+interval '1800 seconds',
        caller.expires_at,signing.not_after,to_timestamp((claims->>'exp')::double precision)) THEN
        RETURN jsonb_build_object('status','unauthenticated');
    END IF;
    IF claims->>'action' IN ('claim.submit','claim.information','claim.decide') THEN
        DELETE FROM {schema}.delegation_verification_contexts WHERE expires_at<=moment;
        IF NOT EXISTS(SELECT 1 FROM {schema}.delegation_verification_contexts
          WHERE issuer=claims->>'iss' AND jti=(claims->>'jti')::uuid AND credential_hash=payload->>'credential_hash'
            AND key_kid=payload->>'key_kid' AND key_hash=payload->>'key_hash') THEN
            IF (SELECT count(*) FROM {schema}.delegation_verification_contexts)>=100000 THEN RETURN jsonb_build_object('status','unavailable'); END IF;
            INSERT INTO {schema}.delegation_verification_contexts
              VALUES(claims->>'iss',(claims->>'jti')::uuid,payload->>'credential_hash',payload->>'key_kid',payload->>'key_hash',payload->>'binding_hash',
                jsonb_build_object('claims',claims,'caller_ref',caller.caller_ref,'audience',caller.audience,'delegation_caller',caller.delegation_caller),
                least(moment+interval '60 seconds',actor_session.expires_at,actor_session.last_seen_at+interval '1800 seconds',caller.expires_at,signing.not_after,to_timestamp((claims->>'exp')::double precision)));
        ELSE
            IF EXISTS(SELECT 1 FROM {schema}.delegation_verification_contexts
              WHERE issuer=claims->>'iss' AND jti=(claims->>'jti')::uuid AND credential_hash=payload->>'credential_hash'
                AND key_kid=payload->>'key_kid' AND key_hash=payload->>'key_hash'
                AND (binding_hash IS DISTINCT FROM payload->>'binding_hash' OR context IS DISTINCT FROM
                  jsonb_build_object('claims',claims,'caller_ref',caller.caller_ref,'audience',caller.audience,'delegation_caller',caller.delegation_caller)))
              THEN RETURN jsonb_build_object('status','conflict'); END IF;
        END IF;
    END IF;
    RETURN jsonb_build_object('status','ok','access_revision',member.access_revision,'verified_at',epoch,'replayed',replayed,
        'authority_expires_at',extract(epoch FROM least(actor_session.expires_at,actor_session.last_seen_at+interval '1800 seconds',
            caller.expires_at,signing.not_after,to_timestamp((claims->>'exp')::double precision))));
END;
$$;
REVOKE ALL ON FUNCTION {schema}.delegation_call(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.delegation_call(text,jsonb) TO {access_role};

CREATE TABLE {schema}.policy_packages (
 tenant_id uuid NOT NULL REFERENCES {schema}.tenants(id),id uuid NOT NULL,package_version bigint NOT NULL CHECK(package_version>0),
 digest text NOT NULL CHECK(digest ~ '^[a-f0-9]{{64}}$'),canonical text NOT NULL CHECK(octet_length(canonical)<=65536),
 rules_json jsonb NOT NULL CHECK(jsonb_typeof(rules_json)='object'),document_refs jsonb NOT NULL CHECK(jsonb_typeof(document_refs)='array'),
 valid_trip_end_from date NOT NULL,valid_trip_end_to date,timezone text NOT NULL REFERENCES {schema}.timezone_catalog(name),
 publication_state text NOT NULL CHECK(publication_state IN ('draft','prepared','active','withdrawn')),
 service_ack_digest text,version bigint NOT NULL DEFAULT 1 CHECK(version>0),eligibility_revision bigint NOT NULL DEFAULT 1 CHECK(eligibility_revision>0),
 allowed_for_new boolean NOT NULL DEFAULT false,allowed_for_locked_claims boolean NOT NULL DEFAULT false,
 blocked_reason text,publisher_id uuid NOT NULL,published_at timestamptz,
 PRIMARY KEY(tenant_id,id,package_version),UNIQUE(tenant_id,id,package_version,digest),
 CHECK(valid_trip_end_to IS NULL OR valid_trip_end_to>valid_trip_end_from),
 CHECK(service_ack_digest IS NULL OR service_ack_digest=digest)
);
CREATE TABLE {schema}.policy_document_refs (
 tenant_id uuid NOT NULL,package_id uuid NOT NULL,package_version bigint NOT NULL,document_id uuid NOT NULL,document_version_id uuid NOT NULL,
 content_digest text NOT NULL CHECK(content_digest ~ '^[a-f0-9]{{64}}$'),
 PRIMARY KEY(tenant_id,package_id,package_version,document_version_id),
 FOREIGN KEY(tenant_id,package_id,package_version) REFERENCES {schema}.policy_packages(tenant_id,id,package_version),
 FOREIGN KEY(tenant_id,document_id) REFERENCES {schema}.documents(tenant_id,id),
 FOREIGN KEY(tenant_id,document_version_id) REFERENCES {schema}.document_versions(tenant_id,id)
);
CREATE TABLE {schema}.policy_coordination (
 tenant_id uuid PRIMARY KEY REFERENCES {schema}.tenants(id),version bigint NOT NULL DEFAULT 1 CHECK(version>0)
);
CREATE TABLE {schema}.policy_windows (
 tenant_id uuid NOT NULL,id uuid NOT NULL,package_id uuid NOT NULL,package_version bigint NOT NULL,
 starts_on date NOT NULL,ends_on date,version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,package_id,package_version),
 FOREIGN KEY(tenant_id,package_id,package_version) REFERENCES {schema}.policy_packages(tenant_id,id,package_version),
 CHECK(ends_on IS NULL OR ends_on>starts_on)
);
CREATE TABLE {schema}.policy_requests (
 tenant_id uuid NOT NULL,actor_id uuid NOT NULL,key uuid NOT NULL,operation text NOT NULL,request_hash text NOT NULL,
 response jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,actor_id,key),FOREIGN KEY(tenant_id,actor_id) REFERENCES {schema}.memberships(tenant_id,principal_id)
);
CREATE TABLE {schema}.policy_dispatches (
 tenant_id uuid NOT NULL,actor_id uuid NOT NULL,key uuid NOT NULL,package_id uuid NOT NULL,package_version bigint NOT NULL,
 operation_id uuid NOT NULL,digest text NOT NULL,base_version bigint NOT NULL CHECK(base_version>0),ack jsonb,
 PRIMARY KEY(tenant_id,actor_id,key),UNIQUE(tenant_id,operation_id),
 FOREIGN KEY(tenant_id,package_id,package_version,digest) REFERENCES {schema}.policy_packages(tenant_id,id,package_version,digest)
);
CREATE TABLE {schema}.policy_audit (
 tenant_id uuid NOT NULL,id uuid NOT NULL,actor_id uuid NOT NULL,package_id uuid NOT NULL,package_version bigint NOT NULL,
 operation text NOT NULL,before_state jsonb,after_state jsonb,occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,id),FOREIGN KEY(tenant_id,package_id,package_version) REFERENCES {schema}.policy_packages(tenant_id,id,package_version)
);
-- Secret HMAC key derived by deployment from the dispatcher credential with the
-- eai:policy-settlement:key:v1: domain. Never expose it to the runtime role.
CREATE TABLE {schema}.policy_receiver_keys (
 key_id text PRIMARY KEY CHECK(key_id ~ '^[A-Za-z0-9_.-]{{1,64}}$'),
 key_hash text NOT NULL CHECK(key_hash ~ '^[a-f0-9]{{64}}$'),active boolean NOT NULL DEFAULT true,
 not_before timestamptz NOT NULL,not_after timestamptz NOT NULL,CHECK(not_after>not_before)
);
ALTER TABLE {schema}.policy_receiver_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.policy_receiver_keys FORCE ROW LEVEL SECURITY;
CREATE POLICY maintenance ON {schema}.policy_receiver_keys TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.policy_receiver_keys FROM PUBLIC,{access_role};
ALTER TABLE {schema}.policy_packages ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.policy_packages FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.policy_document_refs ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.policy_document_refs FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.policy_coordination ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.policy_coordination FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.policy_windows ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.policy_windows FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.policy_requests ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.policy_requests FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.policy_dispatches ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.policy_dispatches FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.policy_audit ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.policy_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY maintenance ON {schema}.policy_packages TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.policy_document_refs TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.policy_coordination TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.policy_windows TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.policy_requests TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.policy_dispatches TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.policy_audit TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.policy_packages,{schema}.policy_document_refs,{schema}.policy_coordination,{schema}.policy_windows,
 {schema}.policy_requests,{schema}.policy_dispatches,{schema}.policy_audit FROM PUBLIC,{access_role};

-- Knowledge-owned metadata guard. No extracted text or reader grant is returned.
CREATE FUNCTION {schema}._knowledge_policy_sources(t uuid,p uuid,refs jsonb,current_only boolean,reader_required boolean) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE source jsonb;d {schema}.documents%ROWTYPE;v {schema}.document_versions%ROWTYPE;f {schema}.files%ROWTYPE;
BEGIN
 IF jsonb_typeof(refs) IS DISTINCT FROM 'array' OR jsonb_array_length(refs) NOT BETWEEN 1 AND 10 THEN RETURN false; END IF;
 FOR source IN SELECT value FROM jsonb_array_elements(refs) ORDER BY value->>'document_id',value->>'version_id' LOOP
  SELECT * INTO d FROM {schema}.documents WHERE tenant_id=t AND id=(source->>'document_id')::uuid FOR SHARE NOWAIT;
  IF NOT FOUND OR d.withdrawn_at IS NOT NULL THEN RETURN false; END IF;
  SELECT * INTO v FROM {schema}.document_versions WHERE tenant_id=t AND id=(source->>'version_id')::uuid AND document_id=d.id FOR SHARE NOWAIT;
  IF NOT FOUND OR v.content_digest IS DISTINCT FROM source->>'content_digest' OR NOT {schema}._knowledge_complete(v)
   OR v.publication_state NOT IN ('published','superseded')
   OR (current_only AND (v.publication_state<>'published' OR d.active_version_id IS DISTINCT FROM v.id)) THEN RETURN false; END IF;
  SELECT * INTO f FROM {schema}.files WHERE tenant_id=t AND id=v.file_id FOR SHARE NOWAIT;
  IF NOT FOUND OR f.purpose<>'knowledge' OR f.scan_state<>'ready' OR f.deletion_state NOT IN ('active','retained')
   OR ROW(f.digest,f.object_version,f.size_bytes,f.mime,f.collection_id) IS DISTINCT FROM ROW(v.file_digest,v.object_version,v.size_bytes,v.mime,d.collection_id) THEN RETURN false; END IF;
  IF d.collection_id IS NOT NULL THEN
   PERFORM scope_ref FROM {schema}.authorization_scopes WHERE tenant_id=t AND scope_kind='knowledge_collection' AND scope_ref=d.collection_id AND active FOR SHARE NOWAIT;
   IF NOT FOUND THEN RETURN false; END IF;
  END IF;
  IF reader_required AND NOT {schema}._knowledge_readable(t,p,v.id,true) THEN RETURN false; END IF;
 END LOOP;
 RETURN true;
END;
$$;
REVOKE ALL ON FUNCTION {schema}._knowledge_policy_sources(uuid,uuid,jsonb,boolean,boolean) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._policy_json(item {schema}.policy_packages) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('package_id',item.id,'package_version',item.package_version,'digest',item.digest,
  'state',item.publication_state,'version',item.version,'eligibility_revision',item.eligibility_revision,
  'allowed_for_new',item.allowed_for_new,'allowed_for_locked_claims',item.allowed_for_locked_claims,'blocked_reason',item.blocked_reason,
  'acknowledged',item.service_ack_digest IS NOT NULL);
$$;
REVOKE ALL ON FUNCTION {schema}._policy_json({schema}.policy_packages) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._policy_window_check() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE item {schema}.policy_packages%ROWTYPE;
BEGIN
 INSERT INTO {schema}.policy_coordination(tenant_id) VALUES(NEW.tenant_id) ON CONFLICT DO NOTHING;
 PERFORM tenant_id FROM {schema}.policy_coordination WHERE tenant_id=NEW.tenant_id FOR UPDATE;
 SELECT * INTO item FROM {schema}.policy_packages WHERE tenant_id=NEW.tenant_id AND id=NEW.package_id AND package_version=NEW.package_version;
 IF NOT FOUND OR item.service_ack_digest IS DISTINCT FROM item.digest
  OR (TG_OP='INSERT' AND item.publication_state<>'active')
  OR NEW.starts_on<item.valid_trip_end_from OR (item.valid_trip_end_to IS NOT NULL AND (NEW.ends_on IS NULL OR NEW.ends_on>item.valid_trip_end_to))
  OR EXISTS(SELECT 1 FROM {schema}.policy_windows w WHERE w.tenant_id=NEW.tenant_id AND w.id<>NEW.id
   AND daterange(w.starts_on,w.ends_on,'[)') && daterange(NEW.starts_on,NEW.ends_on,'[)'))
  THEN RAISE EXCEPTION 'Policy window rejected' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' THEN
  IF ROW(NEW.tenant_id,NEW.id,NEW.package_id,NEW.package_version,NEW.starts_on)
    IS DISTINCT FROM ROW(OLD.tenant_id,OLD.id,OLD.package_id,OLD.package_version,OLD.starts_on)
   OR NEW.ends_on IS NULL OR (OLD.ends_on IS NOT NULL AND NEW.ends_on>=OLD.ends_on)
   OR NEW.ends_on<=(clock_timestamp() AT TIME ZONE item.timezone)::date
   OR NEW.version<>OLD.version+1 THEN
   RAISE EXCEPTION 'Only future narrowing is allowed' USING ERRCODE='23514';
  END IF;
 END IF;
 RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION {schema}._policy_window_check() FROM PUBLIC,{access_role};
CREATE TRIGGER policy_window_check BEFORE INSERT OR UPDATE ON {schema}.policy_windows FOR EACH ROW EXECUTE FUNCTION {schema}._policy_window_check();

CREATE FUNCTION {schema}._policy_immutable_check() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF OLD.publication_state<>'draft' AND ROW(NEW.digest,NEW.canonical,NEW.rules_json,NEW.document_refs,NEW.valid_trip_end_from,NEW.valid_trip_end_to,NEW.timezone)
  IS DISTINCT FROM ROW(OLD.digest,OLD.canonical,OLD.rules_json,OLD.document_refs,OLD.valid_trip_end_from,OLD.valid_trip_end_to,OLD.timezone)
  THEN RAISE EXCEPTION 'Prepared content is immutable' USING ERRCODE='23514'; END IF;
 IF OLD.publication_state='withdrawn' AND NEW.publication_state<>'withdrawn' THEN RAISE EXCEPTION 'Withdrawn package is terminal' USING ERRCODE='23514'; END IF;
 IF OLD.publication_state='active' AND NEW.publication_state IN ('draft','prepared') THEN RAISE EXCEPTION 'Active package cannot revert' USING ERRCODE='23514'; END IF;
 IF OLD.publication_state='prepared' AND NEW.publication_state='draft' THEN RAISE EXCEPTION 'Prepared package cannot revert' USING ERRCODE='23514'; END IF;
 IF (NEW.allowed_for_new OR NEW.allowed_for_locked_claims) AND
  (NEW.publication_state<>'active' OR NEW.service_ack_digest IS DISTINCT FROM NEW.digest) THEN
  RAISE EXCEPTION 'Eligibility requires activation and acknowledgement' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION {schema}._policy_immutable_check() FROM PUBLIC,{access_role};
CREATE TRIGGER policy_immutable BEFORE UPDATE ON {schema}.policy_packages FOR EACH ROW EXECUTE FUNCTION {schema}._policy_immutable_check();


-- POL-01 capabilities. Compose after policy_schema_draft.sql, before role manifest.
CREATE FUNCTION {schema}._policy_canonical(value jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
DECLARE output text;
BEGIN
 IF jsonb_typeof(value)='object' THEN
  SELECT '{{'||coalesce(string_agg(to_jsonb(key)::text||':'||{schema}._policy_canonical(item),',' ORDER BY key COLLATE "C"),'')||'}}'
   INTO output FROM jsonb_each(value) AS e(key,item);
 ELSIF jsonb_typeof(value)='array' THEN
  SELECT '['||coalesce(string_agg({schema}._policy_canonical(item),',' ORDER BY ordinal),'')||']'
   INTO output FROM jsonb_array_elements(value) WITH ORDINALITY AS e(item,ordinal);
 ELSE output:=value::text; END IF;
 RETURN output;
END;
$$;
REVOKE ALL ON FUNCTION {schema}._policy_canonical(jsonb) FROM PUBLIC,{access_role};

-- RFC 2104 HMAC-SHA256 using PostgreSQL's built-in SHA256. Registry key material
-- is private; the caller cannot mint settlement by supplying a valid browser CSRF.
CREATE FUNCTION {schema}._policy_hmac(secret bytea,message bytea) RETURNS bytea
LANGUAGE plpgsql IMMUTABLE STRICT SET search_path=pg_catalog AS $$
DECLARE normalized bytea:=secret;ipad bytea:=decode(repeat('36',64),'hex');opad bytea:=decode(repeat('5c',64),'hex');i integer;
BEGIN
 IF octet_length(normalized)>64 THEN normalized:=sha256(normalized); END IF;
 FOR i IN 0..octet_length(normalized)-1 LOOP
  ipad:=set_byte(ipad,i,get_byte(ipad,i) # get_byte(normalized,i));
  opad:=set_byte(opad,i,get_byte(opad,i) # get_byte(normalized,i));
 END LOOP;
 RETURN sha256(opad||sha256(ipad||message));
END;
$$;
REVOKE ALL ON FUNCTION {schema}._policy_hmac(bytea,bytea) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._policy_content_valid(value jsonb,t uuid,p uuid,v bigint) RETURNS boolean
LANGUAGE plpgsql STABLE SET search_path=pg_catalog AS $$
DECLARE item jsonb;rules jsonb;route integer:=0;
BEGIN
 IF jsonb_typeof(value) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(value))<>9
  OR NOT value ?& ARRAY['tenant_id','package_id','package_version','schema_version','valid_trip_end_from','valid_trip_end_to','timezone','document_versions','rules']
  OR value->>'tenant_id' IS DISTINCT FROM t::text OR value->>'package_id' IS DISTINCT FROM p::text
  OR value->'package_version' IS DISTINCT FROM to_jsonb(v) OR value->'schema_version' IS DISTINCT FROM '1'::jsonb
  OR jsonb_typeof(value->'package_version') IS DISTINCT FROM 'number' OR value->>'package_version' !~ '^[1-9][0-9]*$'
  OR jsonb_typeof(value->'schema_version') IS DISTINCT FROM 'number' OR value->>'schema_version'<>'1'
  OR jsonb_typeof(value->'valid_trip_end_from') IS DISTINCT FROM 'string'
  OR jsonb_typeof(value->'timezone') IS DISTINCT FROM 'string'
  OR value->>'valid_trip_end_from' !~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'
  OR to_char((value->>'valid_trip_end_from')::date,'YYYY-MM-DD') IS DISTINCT FROM value->>'valid_trip_end_from'
  OR NOT EXISTS(SELECT 1 FROM {schema}.timezone_catalog WHERE name=value->>'timezone') THEN RETURN false; END IF;
 IF value->'valid_trip_end_to'<>'null'::jsonb AND (jsonb_typeof(value->'valid_trip_end_to') IS DISTINCT FROM 'string' OR value->>'valid_trip_end_to' !~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'
  OR to_char((value->>'valid_trip_end_to')::date,'YYYY-MM-DD') IS DISTINCT FROM value->>'valid_trip_end_to'
  OR (value->>'valid_trip_end_to')::date<=(value->>'valid_trip_end_from')::date) THEN RETURN false; END IF;
 IF jsonb_typeof(value->'document_versions') IS DISTINCT FROM 'array' OR jsonb_array_length(value->'document_versions') NOT BETWEEN 1 AND 10 THEN RETURN false; END IF;
 FOR item IN SELECT * FROM jsonb_array_elements(value->'document_versions') LOOP
  IF jsonb_typeof(item) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(item))<>3
   OR NOT item ?& ARRAY['document_id','version_id','content_digest']
   OR jsonb_typeof(item->'document_id') IS DISTINCT FROM 'string' OR jsonb_typeof(item->'version_id') IS DISTINCT FROM 'string'
   OR jsonb_typeof(item->'content_digest') IS DISTINCT FROM 'string' OR item->>'content_digest' !~ '^[a-f0-9]{{64}}$'
   OR (item->>'document_id')::uuid::text IS DISTINCT FROM item->>'document_id'
   OR (item->>'version_id')::uuid::text IS DISTINCT FROM item->>'version_id'
   OR (item->>'document_id')::uuid='00000000-0000-0000-0000-000000000000'::uuid
   OR (item->>'version_id')::uuid='00000000-0000-0000-0000-000000000000'::uuid THEN RETURN false; END IF;
 END LOOP;
 IF (SELECT count(DISTINCT e.value_item->>'version_id') FROM jsonb_array_elements(value->'document_versions') e(value_item))<>jsonb_array_length(value->'document_versions') THEN RETURN false; END IF;
 rules:=value->'rules';
 IF jsonb_typeof(rules) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(rules))<>5
  OR NOT rules ?& ARRAY['currency','submission_deadline_days','max_total_minor','categories','approval_route']
  OR jsonb_typeof(rules->'currency') IS DISTINCT FROM 'string' OR rules->>'currency' NOT IN ('SGD','IDR','USD') OR rules->>'currency' IS NULL
  OR jsonb_typeof(rules->'submission_deadline_days') IS DISTINCT FROM 'number'
  OR rules->>'submission_deadline_days' !~ '^(0|[1-9][0-9]*)$'
  OR (rules->>'submission_deadline_days')::bigint NOT BETWEEN 0 AND 3650 THEN RETURN false; END IF;
 IF rules->'max_total_minor'<>'null'::jsonb AND (jsonb_typeof(rules->'max_total_minor') IS DISTINCT FROM 'number' OR rules->>'max_total_minor' !~ '^[1-9][0-9]*$' OR (rules->>'max_total_minor')::bigint<=0) THEN RETURN false; END IF;
 IF jsonb_typeof(rules->'categories') IS DISTINCT FROM 'array' OR jsonb_array_length(rules->'categories') NOT BETWEEN 1 AND 100
  OR jsonb_typeof(rules->'approval_route') IS DISTINCT FROM 'array' OR jsonb_array_length(rules->'approval_route') NOT BETWEEN 1 AND 10 THEN RETURN false; END IF;
 FOR item IN SELECT * FROM jsonb_array_elements(rules->'categories') LOOP
  IF jsonb_typeof(item) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(item))<>3
   OR NOT item ?& ARRAY['code','receipt_required','max_amount_minor'] OR jsonb_typeof(item->'code') IS DISTINCT FROM 'string' OR item->>'code' !~ '^[a-z][a-z0-9_]{{0,63}}$'
   OR jsonb_typeof(item->'receipt_required') IS DISTINCT FROM 'boolean' THEN RETURN false; END IF;
  IF item->'max_amount_minor'<>'null'::jsonb AND (jsonb_typeof(item->'max_amount_minor') IS DISTINCT FROM 'number' OR item->>'max_amount_minor' !~ '^[1-9][0-9]*$' OR (item->>'max_amount_minor')::bigint<=0) THEN RETURN false; END IF;
 END LOOP;
 IF (SELECT count(DISTINCT e.value_item->>'code') FROM jsonb_array_elements(rules->'categories') e(value_item))<>jsonb_array_length(rules->'categories') THEN RETURN false; END IF;
 FOR item IN SELECT * FROM jsonb_array_elements(rules->'approval_route') LOOP
  route:=route+1;
  IF jsonb_typeof(item) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(item))<>3
   OR NOT item ?& ARRAY['stage','approver_id','scope_ref'] OR jsonb_typeof(item->'stage') IS DISTINCT FROM 'number' OR item->>'stage' IS DISTINCT FROM route::text
   OR jsonb_typeof(item->'approver_id') IS DISTINCT FROM 'string'
   OR item->>'scope_ref' IS DISTINCT FROM 'reimbursement' OR (item->>'approver_id')::uuid::text IS DISTINCT FROM item->>'approver_id'
   OR (item->>'approver_id')::uuid='00000000-0000-0000-0000-000000000000'::uuid THEN RETURN false; END IF;
 END LOOP;
 RETURN true;
EXCEPTION WHEN data_exception THEN RETURN false;
END;
$$;
REVOKE ALL ON FUNCTION {schema}._policy_content_valid(jsonb,uuid,uuid,bigint) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.policy_call(token text,operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
<<policy_call>>
DECLARE context jsonb;identity jsonb;t uuid;p uuid;pkg {schema}.policy_packages%ROWTYPE;prior {schema}.policy_requests%ROWTYPE;
 intent {schema}.policy_dispatches%ROWTYPE;old_window {schema}.policy_windows%ROWTYPE;tenant_revision bigint;
 package_id uuid;package_version bigint;intent_key uuid;expected bigint;material_hash text;content jsonb;source jsonb;
 result jsonb;before_state jsonb;deadline numeric;cutoff date;business_day date;tenant_zone text;old_before jsonb;
 receiver_key {schema}.policy_receiver_keys%ROWTYPE;settlement jsonb;proof_material jsonb;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Policy capability rejected.'; END IF;
 IF payload IS NULL OR jsonb_typeof(payload)<>'object' OR octet_length(payload::text)>131072
  OR operation IS NULL OR operation NOT IN ('context','draft','prepare','dispatch','ack','activate','supersede','retire','withdraw','conflict') THEN RETURN jsonb_build_object('status','invalid'); END IF;
 context:={schema}.authorization_call(token,'context',CASE WHEN operation='context' THEN '{{}}'::jsonb ELSE jsonb_build_object('csrf_hash',payload->>'csrf_hash') END);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RETURN context; END IF;
 identity:=context->'identity';t:=(identity->>'active_tenant_id')::uuid;p:=(identity->>'principal_id')::uuid;
 IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(context->'grants') grant_row
  WHERE grant_row->>'role'='process_owner' AND grant_row->>'scope_kind'='process' AND grant_row->>'scope_ref'='reimbursement') THEN RETURN jsonb_build_object('status','forbidden'); END IF;
 SELECT timezone INTO tenant_zone FROM {schema}.tenants WHERE id=t;
 IF operation='context' THEN
  IF payload<>'{{}}'::jsonb THEN RETURN jsonb_build_object('status','invalid'); END IF;
  result:=jsonb_build_object('tenant_id',t,'timezone',tenant_zone);
 ELSE
  IF NOT payload ?& ARRAY['package_id','package_version','key','expected_version','csrf_hash']
   OR (SELECT count(*) FROM jsonb_object_keys(payload))<>(CASE operation WHEN 'draft' THEN 7 WHEN 'supersede' THEN 9 WHEN 'ack' THEN 7 ELSE 5 END)
   OR jsonb_typeof(payload->'package_version') IS DISTINCT FROM 'number' OR payload->>'package_version' !~ '^[1-9][0-9]{{0,18}}$'
   OR jsonb_typeof(payload->'expected_version') IS DISTINCT FROM 'number' OR payload->>'expected_version' !~ '^(0|[1-9][0-9]{{0,18}})$'
   THEN RETURN jsonb_build_object('status','invalid'); END IF;
  package_id:=(payload->>'package_id')::uuid;package_version:=(payload->>'package_version')::bigint;intent_key:=(payload->>'key')::uuid;expected:=(payload->>'expected_version')::bigint;
  IF package_id::text IS DISTINCT FROM payload->>'package_id' OR intent_key::text IS DISTINCT FROM payload->>'key'
   OR package_id='00000000-0000-0000-0000-000000000000'::uuid OR intent_key='00000000-0000-0000-0000-000000000000'::uuid THEN RETURN jsonb_build_object('status','invalid'); END IF;
  INSERT INTO {schema}.policy_coordination(tenant_id) VALUES(t) ON CONFLICT DO NOTHING;
  SELECT version INTO tenant_revision FROM {schema}.policy_coordination WHERE tenant_id=t FOR UPDATE;
  SELECT * INTO pkg FROM {schema}.policy_packages WHERE tenant_id=t AND id=package_id AND policy_packages.package_version=policy_call.package_version FOR UPDATE;
  before_state:=CASE WHEN pkg.id IS NULL THEN NULL ELSE {schema}._policy_json(pkg) END;
  material_hash:=encode(sha256(convert_to((payload-'csrf_hash')::text,'UTF8')),'hex');
  IF operation IN ('prepare','activate','supersede','dispatch','ack') AND pkg.id IS NOT NULL
    AND NOT {schema}._knowledge_policy_sources(t,p,pkg.document_refs,true,true) THEN RETURN jsonb_build_object('status','conflict'); END IF;
  IF operation NOT IN ('dispatch','ack') THEN
   IF EXISTS(SELECT 1 FROM {schema}.policy_dispatches WHERE tenant_id=t AND actor_id=p AND key=intent_key) THEN RETURN jsonb_build_object('status','conflict'); END IF;
   SELECT * INTO prior FROM {schema}.policy_requests WHERE tenant_id=t AND actor_id=p AND key=intent_key;
   IF FOUND THEN
    IF prior.operation IS DISTINCT FROM operation OR prior.request_hash IS DISTINCT FROM material_hash OR pkg.id IS NULL THEN RETURN jsonb_build_object('status','conflict'); END IF;
    result:={schema}._policy_json(pkg);
   ELSE
    IF operation='draft' THEN
     IF NOT payload ?& ARRAY['canonical','digest'] OR jsonb_typeof(payload->'canonical') IS DISTINCT FROM 'string'
       OR payload->>'digest' !~ '^[a-f0-9]{{64}}$' OR octet_length(payload->>'canonical')>65536 THEN RETURN jsonb_build_object('status','invalid'); END IF;
     content:=(payload->>'canonical')::jsonb;
     IF NOT {schema}._policy_content_valid(content,t,package_id,package_version) OR {schema}._policy_canonical(content) IS DISTINCT FROM payload->>'canonical'
       OR encode(sha256(convert_to('reimbursement:rule-package:v1:'||(payload->>'canonical'),'UTF8')),'hex') IS DISTINCT FROM payload->>'digest'
       THEN RETURN jsonb_build_object('status','invalid'); END IF;
     IF (pkg.id IS NULL AND expected<>0) OR (pkg.id IS NOT NULL AND (pkg.publication_state<>'draft' OR pkg.version<>expected)) THEN RETURN jsonb_build_object('status','conflict'); END IF;
     IF pkg.id IS NULL THEN
      INSERT INTO {schema}.policy_packages(tenant_id,id,package_version,digest,canonical,rules_json,document_refs,valid_trip_end_from,valid_trip_end_to,timezone,publication_state,publisher_id)
       VALUES(t,package_id,package_version,payload->>'digest',payload->>'canonical',content->'rules',content->'document_versions',(content->>'valid_trip_end_from')::date,(content->>'valid_trip_end_to')::date,content->>'timezone','draft',p) RETURNING * INTO pkg;
     ELSE
      UPDATE {schema}.policy_packages SET digest=payload->>'digest',canonical=payload->>'canonical',rules_json=content->'rules',document_refs=content->'document_versions',valid_trip_end_from=(content->>'valid_trip_end_from')::date,valid_trip_end_to=(content->>'valid_trip_end_to')::date,timezone=content->>'timezone',version=version+1,publisher_id=p
       WHERE tenant_id=t AND id=package_id AND policy_packages.package_version=policy_call.package_version RETURNING * INTO pkg;
     END IF;
     DELETE FROM {schema}.policy_document_refs WHERE tenant_id=t AND policy_document_refs.package_id=policy_call.package_id AND policy_document_refs.package_version=policy_call.package_version;
     FOR source IN SELECT * FROM jsonb_array_elements(pkg.document_refs) LOOP
      INSERT INTO {schema}.policy_document_refs VALUES(t,package_id,package_version,(source->>'document_id')::uuid,(source->>'version_id')::uuid,source->>'content_digest');
     END LOOP;
    ELSE
     IF pkg.id IS NULL THEN RETURN jsonb_build_object('status','missing'); END IF;
     IF pkg.version<>expected OR pkg.publication_state='withdrawn' THEN RETURN jsonb_build_object('status','conflict'); END IF;
     IF operation='prepare' THEN
      IF pkg.publication_state<>'draft' OR pkg.timezone<>tenant_zone THEN RETURN jsonb_build_object('status','conflict'); END IF;
      UPDATE {schema}.policy_packages SET publication_state='prepared',version=version+1 WHERE tenant_id=t AND id=package_id AND policy_packages.package_version=policy_call.package_version RETURNING * INTO pkg;
     ELSIF operation IN ('activate','supersede') THEN
      IF pkg.publication_state<>'prepared' OR pkg.service_ack_digest IS DISTINCT FROM pkg.digest OR pkg.timezone<>tenant_zone THEN RETURN jsonb_build_object('status','conflict'); END IF;
      IF operation='supersede' THEN
       IF NOT payload ?& ARRAY['old_window_id','old_window_version','tenant_version','cutoff']
        OR payload->>'old_window_version' !~ '^[1-9][0-9]*$' OR payload->>'tenant_version' !~ '^[1-9][0-9]*$'
        OR tenant_revision<>(payload->>'tenant_version')::bigint THEN RETURN jsonb_build_object('status','conflict'); END IF;
       SELECT * INTO old_window FROM {schema}.policy_windows WHERE tenant_id=t AND id=(payload->>'old_window_id')::uuid FOR UPDATE;
       cutoff:=(payload->>'cutoff')::date;business_day:=(clock_timestamp() AT TIME ZONE tenant_zone)::date;
       IF old_window.id IS NULL OR old_window.version<>(payload->>'old_window_version')::bigint OR cutoff<=business_day
        OR cutoff<=old_window.starts_on OR (old_window.ends_on IS NOT NULL AND cutoff>=old_window.ends_on)
        OR cutoff<pkg.valid_trip_end_from OR (pkg.valid_trip_end_to IS NOT NULL AND cutoff>=pkg.valid_trip_end_to) THEN RETURN jsonb_build_object('status','conflict'); END IF;
       old_before:=to_jsonb(old_window);
       UPDATE {schema}.policy_windows SET ends_on=cutoff,version=version+1 WHERE tenant_id=t AND id=old_window.id;
      END IF;
      UPDATE {schema}.policy_packages SET publication_state='active',allowed_for_new=true,allowed_for_locked_claims=true,blocked_reason=NULL,published_at=clock_timestamp(),publisher_id=p,version=version+1,eligibility_revision=eligibility_revision+1
       WHERE tenant_id=t AND id=package_id AND policy_packages.package_version=policy_call.package_version RETURNING * INTO pkg;
      INSERT INTO {schema}.policy_windows VALUES(t,gen_random_uuid(),package_id,package_version,CASE WHEN operation='supersede' THEN cutoff ELSE pkg.valid_trip_end_from END,pkg.valid_trip_end_to,1);
      UPDATE {schema}.policy_coordination SET version=version+1 WHERE tenant_id=t;
     ELSIF operation IN ('retire','withdraw','conflict') THEN
      UPDATE {schema}.policy_packages SET publication_state=CASE WHEN operation='withdraw' THEN 'withdrawn' ELSE publication_state END,
       allowed_for_new=false,allowed_for_locked_claims=CASE WHEN operation='retire' THEN allowed_for_locked_claims ELSE false END,
       blocked_reason=CASE WHEN operation='conflict' THEN 'known_conflict' WHEN operation='withdraw' THEN 'withdrawn' ELSE 'retired' END,
       version=version+1,eligibility_revision=eligibility_revision+1
       WHERE tenant_id=t AND id=package_id AND policy_packages.package_version=policy_call.package_version RETURNING * INTO pkg;
     END IF;
    END IF;
    result:={schema}._policy_json(pkg);
    INSERT INTO {schema}.policy_requests VALUES(t,p,intent_key,operation,material_hash,result,clock_timestamp());
    INSERT INTO {schema}.policy_audit VALUES(t,gen_random_uuid(),p,package_id,package_version,operation,CASE WHEN operation='supersede' THEN jsonb_build_object('package',before_state,'old_window',old_before) ELSE before_state END,result,clock_timestamp());
   END IF;
  ELSE
   -- Dispatch and ACK operate on one durable frozen network intent.
   IF pkg.id IS NULL THEN RETURN jsonb_build_object('status','missing'); END IF;
   SELECT * INTO intent FROM {schema}.policy_dispatches WHERE tenant_id=t AND actor_id=p AND key=intent_key FOR UPDATE;
   IF operation='dispatch' THEN
    IF intent.key IS NULL THEN
     IF pkg.publication_state<>'prepared' OR pkg.version<>expected OR EXISTS(SELECT 1 FROM {schema}.policy_requests WHERE tenant_id=t AND actor_id=p AND key=intent_key) THEN RETURN jsonb_build_object('status','conflict'); END IF;
     INSERT INTO {schema}.policy_dispatches(tenant_id,actor_id,key,package_id,package_version,operation_id,digest,base_version)
      VALUES(t,p,intent_key,package_id,package_version,gen_random_uuid(),pkg.digest,expected) RETURNING * INTO intent;
    END IF;
   ELSIF intent.key IS NULL THEN RETURN jsonb_build_object('status','conflict'); END IF;
   IF intent.package_id<>package_id OR intent.package_version<>package_version OR intent.digest<>pkg.digest OR intent.base_version<>expected THEN RETURN jsonb_build_object('status','conflict'); END IF;
   IF operation='dispatch' THEN
    SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) INTO deadline FROM {schema}.sessions WHERE token_hash=token;
    result:=jsonb_build_object('publisher',jsonb_build_object('tenant_id',t,'actor_id',p,'membership_id',identity->>'active_membership_id','access_revision',identity->'access_revision',
      'session_id',(SELECT id FROM {schema}.sessions WHERE token_hash=token),'authority_expires_at',deadline),
      'body',(pkg.canonical::jsonb-'tenant_id'-'package_id'-'package_version')||jsonb_build_object('digest',pkg.digest),'operation_id',intent.operation_id,'ack',intent.ack);
   ELSE
    IF jsonb_typeof(payload->'ack') IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(payload->'ack'))<>5
     OR NOT (payload->'ack') ?& ARRAY['package_id','package_version','digest','received_at','state']
     OR payload->'ack'->>'package_id' IS DISTINCT FROM package_id::text OR payload->'ack'->'package_version' IS DISTINCT FROM to_jsonb(package_version)
     OR payload->'ack'->>'digest' IS DISTINCT FROM pkg.digest OR payload->'ack'->>'state' IS DISTINCT FROM 'received'
     OR jsonb_typeof(payload->'ack'->'package_version') IS DISTINCT FROM 'number'
     OR payload->'ack'->>'package_version' !~ '^[1-9][0-9]*$'
     OR jsonb_typeof(payload->'ack'->'received_at') IS DISTINCT FROM 'number'
     OR payload->'ack'->>'received_at' !~ '^[1-9][0-9]*$' OR (payload->'ack'->>'received_at')::bigint>extract(epoch FROM clock_timestamp())+5
     THEN RETURN jsonb_build_object('status','invalid'); END IF;
    settlement:=payload->'settlement';
    IF jsonb_typeof(settlement) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(settlement))<>2
     OR NOT settlement ?& ARRAY['key_id','proof']
     OR jsonb_typeof(settlement->'key_id') IS DISTINCT FROM 'string' OR settlement->>'key_id' !~ '^[A-Za-z0-9_.-]{{1,64}}$'
     OR jsonb_typeof(settlement->'proof') IS DISTINCT FROM 'string' OR settlement->>'proof' !~ '^[a-f0-9]{{64}}$'
     THEN RETURN jsonb_build_object('status','invalid'); END IF;
    SELECT * INTO receiver_key FROM {schema}.policy_receiver_keys WHERE key_id=settlement->>'key_id' FOR SHARE;
    IF NOT FOUND OR NOT receiver_key.active OR receiver_key.not_before>clock_timestamp() OR receiver_key.not_after<=clock_timestamp()
     THEN RETURN jsonb_build_object('status','forbidden'); END IF;
    proof_material:=jsonb_build_object('tenant_id',t,'actor_id',p,'package_id',package_id,'package_version',package_version,
     'key',intent_key,'operation_id',intent.operation_id,'expected_version',expected,'ack',payload->'ack');
    IF encode({schema}._policy_hmac(decode(receiver_key.key_hash,'hex'),convert_to({schema}._policy_canonical(proof_material),'UTF8')),'hex')
      IS DISTINCT FROM settlement->>'proof' THEN RETURN jsonb_build_object('status','forbidden'); END IF;
    IF intent.ack IS NOT NULL AND intent.ack IS DISTINCT FROM payload->'ack' THEN RETURN jsonb_build_object('status','conflict'); END IF;
    IF intent.ack IS NULL THEN
     IF pkg.publication_state<>'prepared' THEN RETURN jsonb_build_object('status','conflict'); END IF;
     UPDATE {schema}.policy_dispatches SET ack=payload->'ack' WHERE tenant_id=t AND actor_id=p AND key=intent_key;
     IF pkg.service_ack_digest IS NULL THEN
      UPDATE {schema}.policy_packages SET service_ack_digest=digest,version=version+1 WHERE tenant_id=t AND id=package_id AND policy_packages.package_version=policy_call.package_version RETURNING * INTO pkg;
     END IF;
     INSERT INTO {schema}.policy_audit VALUES(t,gen_random_uuid(),p,package_id,package_version,'ack',before_state,{schema}._policy_json(pkg),clock_timestamp());
    END IF;
    result:={schema}._policy_json(pkg);
   END IF;
  END IF;
 END IF;
 context:={schema}.authorization_call(token,'finish',CASE WHEN operation='context' THEN '{{}}'::jsonb ELSE jsonb_build_object('csrf_hash',payload->>'csrf_hash') END);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'Policy authority expired' USING ERRCODE='40001'; END IF;
 -- Revocation is excluded by the registry SHARE lock, but wall-clock expiry
 -- can still cross during final authorization. Roll back all ACK writes.
 IF operation='ack' AND (NOT receiver_key.active OR receiver_key.not_before>clock_timestamp() OR receiver_key.not_after<=clock_timestamp()) THEN
  RAISE EXCEPTION 'Policy receiver authority expired' USING ERRCODE='40001';
 END IF;
 SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) INTO deadline FROM {schema}.sessions WHERE token_hash=token;
 RETURN result||jsonb_build_object('status','ok','authority_expires_at',deadline);
EXCEPTION WHEN check_violation OR unique_violation THEN RETURN jsonb_build_object('status','conflict');
 WHEN data_exception OR foreign_key_violation OR not_null_violation THEN RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}.policy_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.policy_call(text,text,jsonb) TO {access_role};

-- CLAIM-01 replaces this transaction-local port with its real locked relation.
-- POL-01 verification may install a controlled exact-binding fixture in an owned
-- schema; runtime stays unavailable for locked claims until the producer exists.
CREATE FUNCTION {schema}._claim_policy_binding_valid(claims jsonb,request jsonb) RETURNS boolean
LANGUAGE sql VOLATILE SET search_path=pg_catalog AS $$ SELECT false; $$;
REVOKE ALL ON FUNCTION {schema}._claim_policy_binding_valid(jsonb,jsonb) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.policy_eligibility_call(auth jsonb,request jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE actor jsonb;ref jsonb;claims jsonb;original jsonb;policy jsonb;kind text;trip_date date;t uuid;p uuid;
 caller {schema}.internal_caller_credentials%ROWTYPE;ctx {schema}.delegation_verification_contexts%ROWTYPE;
 signing {schema}.delegation_keys%ROWTYPE;s {schema}.sessions%ROWTYPE;member record;pkg {schema}.policy_packages%ROWTYPE;
 enabled boolean;moment timestamptz;epoch bigint;expires bigint;reason text:=NULL;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Policy eligibility rejected.'; END IF;
 IF jsonb_typeof(auth) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(auth))<>3
  OR NOT auth ?& ARRAY['actor','credential_hash','verification_ref'] OR auth->>'credential_hash' !~ '^[a-f0-9]{{64}}$'
  OR jsonb_typeof(request) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(request))<>5
  OR NOT request ?& ARRAY['payload','policy','operation_kind','trip_date','claim_ref'] OR octet_length(request::text)>131072 THEN RETURN jsonb_build_object('status','invalid'); END IF;
 actor:=auth->'actor';ref:=auth->'verification_ref';original:=request->'payload';policy:=request->'policy';kind:=request->>'operation_kind';
 IF jsonb_typeof(actor) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(actor))<>12
  OR NOT actor ?& ARRAY['allow','scope','actor_id','tenant_id','membership_id','access_revision','action','operation_id','request_hash','verified_at','expires_at','replayed']
  OR actor->'allow' IS DISTINCT FROM 'true'::jsonb OR actor->>'scope' IS DISTINCT FROM 'actor_only'
  OR jsonb_typeof(actor->'replayed') IS DISTINCT FROM 'boolean'
  OR jsonb_typeof(ref) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(ref))<>6
  OR NOT ref ?& ARRAY['issuer','jti','binding_hash','credential_hash','key_kid','key_hash']
  OR ref->>'credential_hash' IS DISTINCT FROM auth->>'credential_hash'
  OR ref->>'binding_hash' !~ '^[a-f0-9]{{64}}$' OR ref->>'key_hash' !~ '^[a-f0-9]{{64}}$'
  OR jsonb_typeof(policy) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(policy))<>3
  OR NOT policy ?& ARRAY['id','version','digest'] OR policy->>'digest' !~ '^[a-f0-9]{{64}}$'
  OR jsonb_typeof(policy->'version') IS DISTINCT FROM 'number' OR policy->>'version' !~ '^[1-9][0-9]*$'
  OR request->>'trip_date' !~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'
  OR kind NOT IN ('submission','information','decision') OR kind IS NULL THEN RETURN jsonb_build_object('status','invalid'); END IF;
 IF original->'schema_version' IS DISTINCT FROM '1'::jsonb OR original->>'action' IS DISTINCT FROM (CASE kind WHEN 'submission' THEN 'claim.submit' WHEN 'information' THEN 'claim.information' ELSE 'claim.decide' END) THEN RETURN jsonb_build_object('status','invalid'); END IF;
 IF (kind='submission' AND (request->'claim_ref'<>'null'::jsonb OR original->'data'->'policy' IS DISTINCT FROM policy))
  OR (kind<>'submission' AND request->>'claim_ref' IS DISTINCT FROM original->'object'->>'id')
  OR (kind='information' AND original->'data'->'policy' IS DISTINCT FROM policy) THEN RETURN jsonb_build_object('status','invalid'); END IF;
 trip_date:=(request->>'trip_date')::date;
 SELECT * INTO caller FROM {schema}.internal_caller_credentials WHERE credential_hash=auth->>'credential_hash' FOR SHARE;
 IF NOT FOUND OR NOT caller.active OR NOT caller.allowed_actions ? (original->>'action') THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
 SELECT * INTO ctx FROM {schema}.delegation_verification_contexts
  WHERE issuer=ref->>'issuer' AND jti=(ref->>'jti')::uuid AND credential_hash=ref->>'credential_hash'
   AND key_kid=ref->>'key_kid' AND key_hash=ref->>'key_hash' AND binding_hash=ref->>'binding_hash' FOR SHARE;
 IF NOT FOUND OR ctx.context->>'caller_ref' IS DISTINCT FROM caller.caller_ref OR ctx.context->>'audience' IS DISTINCT FROM caller.audience
  OR ctx.context->>'delegation_caller' IS DISTINCT FROM caller.delegation_caller THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
 claims:=ctx.context->'claims';t:=(claims->>'tenant_id')::uuid;p:=(claims->>'sub')::uuid;
 IF actor->>'actor_id' IS DISTINCT FROM claims->>'sub' OR actor->>'tenant_id' IS DISTINCT FROM claims->>'tenant_id'
  OR actor->>'membership_id' IS DISTINCT FROM claims->>'membership_id' OR actor->'access_revision' IS DISTINCT FROM claims->'access_revision'
  OR actor->>'action' IS DISTINCT FROM claims->>'action' OR actor->>'operation_id' IS DISTINCT FROM claims->>'operation_id'
  OR actor->>'request_hash' IS DISTINCT FROM claims->>'request_hash'
  OR original->>'tenant_id' IS DISTINCT FROM claims->>'tenant_id' OR original->>'actor_id' IS DISTINCT FROM claims->>'sub'
  OR original->>'action' IS DISTINCT FROM claims->>'action' OR original->>'operation_id' IS DISTINCT FROM claims->>'operation_id'
  OR original->'object'->>'kind' IS DISTINCT FROM claims->>'object_kind' OR original->'object'->>'id' IS DISTINCT FROM claims->>'object_id'
  OR original->'object'->'version' IS DISTINCT FROM claims->'object_version'
  OR encode(sha256(convert_to('eai:human-action:v1:'||{schema}._policy_canonical(original),'UTF8')),'hex') IS DISTINCT FROM claims->>'request_hash'
  THEN RETURN jsonb_build_object('status','forbidden'); END IF;
 SELECT active INTO enabled FROM {schema}.principals WHERE id=p FOR SHARE;
 IF enabled IS NOT TRUE THEN RETURN jsonb_build_object('status','forbidden'); END IF;
 SELECT * INTO s FROM {schema}.sessions WHERE id=(claims->>'session_id')::uuid FOR SHARE;
 IF NOT FOUND OR s.principal_id<>p OR s.active_tenant_id IS DISTINCT FROM t OR s.revoked_at IS NOT NULL
  OR s.membership_revision IS DISTINCT FROM (claims->>'access_revision')::bigint THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
 SELECT m.id,m.access_revision,m.active AND tenant.active AS active INTO member FROM {schema}.memberships m JOIN {schema}.tenants tenant ON tenant.id=m.tenant_id
  WHERE m.tenant_id=t AND m.id=(claims->>'membership_id')::uuid AND m.principal_id=p FOR SHARE OF m,tenant;
 IF member.id IS NULL OR member.active IS NOT TRUE OR member.access_revision IS DISTINCT FROM (claims->>'access_revision')::bigint THEN RETURN jsonb_build_object('status','forbidden'); END IF;
 SELECT * INTO signing FROM {schema}.delegation_keys WHERE issuer=ctx.issuer AND kid=ctx.key_kid FOR SHARE;
 IF NOT FOUND OR NOT signing.active OR encode(sha256(convert_to(signing.public_jwk::text,'UTF8')),'hex')<>ctx.key_hash THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
 IF NOT EXISTS(SELECT 1 FROM {schema}.membership_roles r WHERE r.tenant_id=t AND r.membership_id=member.id
  AND {schema}._authorization_scope_valid(r.tenant_id,r.role,r.scope_kind,r.scope_ref)
  AND ((r.role='employee' AND r.scope_kind='tenant' AND r.scope_ref=t::text AND claims->>'action' IN ('claim.submit','claim.information'))
    OR (r.role='approver' AND claims->>'action'='claim.decide'))) THEN RETURN jsonb_build_object('status','forbidden'); END IF;
 -- Writers serialize selection changes on coordination before package locks.
 -- Keep the current selection stable until this eligibility transaction ends.
 PERFORM tenant_id FROM {schema}.policy_coordination WHERE tenant_id=t FOR SHARE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','missing'); END IF;
 -- CLAIM-01's transaction-local implementation must lock the exact live claim
 -- relation (owner/reviewer/revision/stage/policy) until commit. VOLATILE permits
 -- the required row locks; a snapshot-only predicate is insufficient after waits.
 IF kind<>'submission' AND {schema}._claim_policy_binding_valid(claims,request) IS NOT TRUE THEN RETURN jsonb_build_object('status','unavailable'); END IF;
 SELECT * INTO pkg FROM {schema}.policy_packages WHERE tenant_id=t AND id=(policy->>'id')::uuid AND package_version=(policy->>'version')::bigint FOR SHARE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','missing'); END IF;
 IF pkg.digest<>policy->>'digest' THEN RETURN jsonb_build_object('status','forbidden'); END IF;
 IF pkg.publication_state='withdrawn' THEN reason:='withdrawn';
 ELSIF pkg.publication_state<>'active' THEN reason:='not_active';
 ELSIF NOT (CASE WHEN kind='submission' THEN pkg.allowed_for_new ELSE pkg.allowed_for_locked_claims END) THEN reason:=coalesce(pkg.blocked_reason,'not_active');
 ELSIF trip_date<pkg.valid_trip_end_from OR (pkg.valid_trip_end_to IS NOT NULL AND trip_date>=pkg.valid_trip_end_to) THEN reason:='range_mismatch';
 ELSIF kind='submission' AND NOT EXISTS(SELECT 1 FROM {schema}.policy_windows w WHERE w.tenant_id=t AND w.package_id=pkg.id AND w.package_version=pkg.package_version
  AND trip_date>=w.starts_on AND (w.ends_on IS NULL OR trip_date<w.ends_on)) THEN reason:='not_selected';
 ELSIF NOT {schema}._knowledge_policy_sources(t,p,pkg.document_refs,false,false) THEN reason:='source_unavailable'; END IF;
 moment:=clock_timestamp();epoch:=floor(extract(epoch FROM moment))::bigint;
 IF actor->>'expires_at' !~ '^[1-9][0-9]*$' OR actor->>'verified_at' !~ '^[1-9][0-9]*$'
  OR jsonb_typeof(actor->'expires_at') IS DISTINCT FROM 'number' OR jsonb_typeof(actor->'verified_at') IS DISTINCT FROM 'number'
  OR (actor->>'verified_at')::bigint>epoch+5 OR moment<signing.not_before OR NOT caller.active
  OR moment>=least(ctx.expires_at,s.expires_at,s.last_seen_at+interval '1800 seconds',caller.expires_at,signing.not_after,to_timestamp((claims->>'exp')::double precision),to_timestamp((actor->>'expires_at')::double precision))
  THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
 expires:=floor(extract(epoch FROM least(moment+interval '60 seconds',ctx.expires_at,s.expires_at,s.last_seen_at+interval '1800 seconds',caller.expires_at,signing.not_after,to_timestamp((claims->>'exp')::double precision),to_timestamp((actor->>'expires_at')::double precision))))::bigint;
 IF expires<=epoch THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
 RETURN jsonb_build_object('status','ok','allow',reason IS NULL,'policy',policy,'operation_id',claims->>'operation_id','request_hash',claims->>'request_hash',
  'operation_kind',kind,'trip_date',request->>'trip_date','claim_ref',request->'claim_ref','eligibility_revision',pkg.eligibility_revision,'checked_at',epoch,'expires_at',expires,'blocked_reason',reason);
EXCEPTION WHEN data_exception THEN RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}.policy_eligibility_call(jsonb,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.policy_eligibility_call(jsonb,jsonb) TO {access_role};

-- Read-only producer context for the explicit trips.list catalog action. No
-- caller-provided tenant/actor/grant or fictitious trip ID is accepted here.
CREATE FUNCTION {schema}.trip_catalog_call(token text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE authority jsonb;identity jsonb;deadline numeric;session_id uuid;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Trip catalog capability rejected.'; END IF;
 authority:={schema}.authorization_call(token,'context','{{}}'::jsonb);
 IF authority->>'status' IS DISTINCT FROM 'ok' THEN RETURN authority; END IF;
 identity:=authority->'identity';
 IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(authority->'grants') g WHERE g->>'role'='employee'
  AND g->>'scope_kind'='tenant' AND g->>'scope_ref'=identity->>'active_tenant_id') THEN RETURN jsonb_build_object('status','forbidden'); END IF;
 authority:={schema}.authorization_call(token,'finish','{{}}'::jsonb);
 IF authority->>'status' IS DISTINCT FROM 'ok' THEN RETURN authority; END IF;
 SELECT id,extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) INTO session_id,deadline
  FROM {schema}.sessions WHERE token_hash=token;
 RETURN jsonb_build_object('status','ok','tenant_id',identity->>'active_tenant_id','actor_id',identity->>'principal_id',
  'membership_id',identity->>'active_membership_id','access_revision',identity->'access_revision','session_id',session_id,'authority_expires_at',deadline);
END;
$$;
REVOKE ALL ON FUNCTION {schema}.trip_catalog_call(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.trip_catalog_call(text) TO {access_role};

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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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



