-- FILE-02: private durable receipt retention. No automatic deletion/release.
CREATE TABLE {schema}.receipt_receiver_keys (
 credential_hash text NOT NULL REFERENCES {schema}.internal_caller_credentials(credential_hash),
 key_id text NOT NULL CHECK(key_id ~ '^[A-Za-z0-9_-]{{1,64}}$'),
 key_material bytea NOT NULL CHECK(octet_length(key_material)=32),
 active boolean NOT NULL DEFAULT true,not_before timestamptz NOT NULL,not_after timestamptz NOT NULL,
 PRIMARY KEY(credential_hash,key_id),CHECK(not_before<not_after)
);
CREATE TABLE {schema}.receipt_pin_batches (
 tenant_id uuid NOT NULL REFERENCES {schema}.tenants(id),operation_id uuid NOT NULL,
 actor_id uuid NOT NULL REFERENCES {schema}.principals(id),candidate_claim_id uuid NOT NULL,
 caller_ref text NOT NULL,batch jsonb NOT NULL CHECK(jsonb_typeof(batch)='object' AND octet_length(batch::text)<=16384),
 batch_digest text NOT NULL CHECK(batch_digest ~ '^[a-f0-9]{{64}}$'),
 state text NOT NULL DEFAULT 'held' CHECK(state IN ('held','finalised','released')),
 terminal_digest text CHECK(terminal_digest ~ '^[a-f0-9]{{64}}$'),claim_version bigint CHECK(claim_version>0),
 authority_expires_at timestamptz NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,operation_id),
 CHECK((state='held' AND terminal_digest IS NULL AND claim_version IS NULL) OR
       (state='finalised' AND terminal_digest IS NOT NULL AND claim_version IS NOT NULL) OR
       (state='released' AND terminal_digest IS NOT NULL AND claim_version IS NULL))
);
CREATE TABLE {schema}.receipt_pins (
 tenant_id uuid NOT NULL,operation_id uuid NOT NULL,file_id uuid NOT NULL,file_version bigint NOT NULL CHECK(file_version>0),
 file_digest text NOT NULL CHECK(file_digest ~ '^[a-f0-9]{{64}}$'),
 PRIMARY KEY(tenant_id,operation_id,file_id),
 FOREIGN KEY(tenant_id,operation_id) REFERENCES {schema}.receipt_pin_batches(tenant_id,operation_id),
 FOREIGN KEY(tenant_id,file_id,file_digest) REFERENCES {schema}.files(tenant_id,id,digest)
);
CREATE INDEX receipt_pins_file ON {schema}.receipt_pins(tenant_id,file_id);
CREATE UNIQUE INDEX receipt_pin_idempotency ON {schema}.receipt_pin_batches
 (tenant_id,actor_id,(batch->'identity'->>'kind'),(batch->'identity'->>'key'));
ALTER TABLE {schema}.receipt_receiver_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.receipt_receiver_keys FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.receipt_pin_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.receipt_pin_batches FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.receipt_pins ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.receipt_pins FORCE ROW LEVEL SECURITY;
CREATE POLICY maintenance ON {schema}.receipt_receiver_keys TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.receipt_pin_batches TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.receipt_pins TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.receipt_receiver_keys,{schema}.receipt_pin_batches,{schema}.receipt_pins FROM PUBLIC,{access_role};

-- ACT-01 supplies the transaction-local human receipt source. Unbound means
-- deny; owned fixture override is not a production human approval producer.
CREATE FUNCTION {schema}._receipt_human_current(claims jsonb,payload jsonb) RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog AS $$ SELECT false $$;
REVOKE ALL ON FUNCTION {schema}._receipt_human_current(jsonb,jsonb) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._receipt_pin_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF TG_TABLE_NAME='receipt_pins' THEN RAISE EXCEPTION 'Receipt pin identity is immutable' USING ERRCODE='23514';END IF;
 IF TG_OP='DELETE' OR OLD.state<>'held'
  OR ROW(NEW.tenant_id,NEW.operation_id,NEW.actor_id,NEW.candidate_claim_id,NEW.caller_ref,NEW.batch,NEW.batch_digest,NEW.created_at)
    IS DISTINCT FROM ROW(OLD.tenant_id,OLD.operation_id,OLD.actor_id,OLD.candidate_claim_id,OLD.caller_ref,OLD.batch,OLD.batch_digest,OLD.created_at)
 THEN RAISE EXCEPTION 'Receipt pin identity is immutable' USING ERRCODE='23514';END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER receipt_batch_immutable BEFORE UPDATE OR DELETE ON {schema}.receipt_pin_batches FOR EACH ROW EXECUTE FUNCTION {schema}._receipt_pin_guard();
CREATE TRIGGER receipt_pin_immutable BEFORE UPDATE OR DELETE ON {schema}.receipt_pins FOR EACH ROW EXECUTE FUNCTION {schema}._receipt_pin_guard();
CREATE FUNCTION {schema}._receipt_commit_expiry() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF clock_timestamp()>=NEW.authority_expires_at THEN RAISE EXCEPTION 'Receipt authority expired' USING ERRCODE='40001';END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER receipt_commit_expiry AFTER INSERT OR UPDATE ON {schema}.receipt_pin_batches
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {schema}._receipt_commit_expiry();
CREATE FUNCTION {schema}._receipt_file_retention() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM {schema}.receipt_pins p JOIN {schema}.receipt_pin_batches b USING(tenant_id,operation_id)
   WHERE p.tenant_id=OLD.tenant_id AND p.file_id=OLD.id AND b.state IN ('held','finalised'))
  AND (TG_OP='DELETE' OR ROW(NEW.tenant_id,NEW.id,NEW.owner_principal_id,NEW.purpose,NEW.mime,NEW.size_bytes,NEW.digest,NEW.object_key,NEW.object_version,NEW.version,NEW.scan_state)
    IS DISTINCT FROM ROW(OLD.tenant_id,OLD.id,OLD.owner_principal_id,OLD.purpose,OLD.mime,OLD.size_bytes,OLD.digest,OLD.object_key,OLD.object_version,OLD.version,OLD.scan_state)
    OR NEW.deletion_state NOT IN ('active','retained'))
 THEN RAISE EXCEPTION 'Receipt retained by operation' USING ERRCODE='23514';END IF;
 IF TG_OP='DELETE' THEN RETURN OLD;END IF;RETURN NEW;
END $$;
CREATE TRIGGER receipt_file_retention BEFORE UPDATE OR DELETE ON {schema}.files FOR EACH ROW EXECUTE FUNCTION {schema}._receipt_file_retention();
REVOKE ALL ON FUNCTION {schema}._receipt_pin_guard(),{schema}._receipt_commit_expiry(),{schema}._receipt_file_retention() FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._receipt_authority(auth jsonb,original jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE actor jsonb:=auth->'actor';ref jsonb:=auth->'verification_ref';claims jsonb;t uuid;p uuid;
 caller {schema}.internal_caller_credentials%ROWTYPE;ctx {schema}.delegation_verification_contexts%ROWTYPE;
 signing {schema}.delegation_keys%ROWTYPE;s {schema}.sessions%ROWTYPE;member record;
 enabled boolean;moment timestamptz;epoch bigint;expires bigint;
BEGIN
 IF jsonb_typeof(auth) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(auth))<>3
  OR NOT auth ?& ARRAY['actor','credential_hash','verification_ref']
  OR jsonb_typeof(actor) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(actor))<>12
  OR NOT actor ?& ARRAY['allow','scope','actor_id','tenant_id','membership_id','access_revision','action','operation_id','request_hash','verified_at','expires_at','replayed']
  OR actor->'allow' IS DISTINCT FROM 'true'::jsonb OR actor->>'scope' IS DISTINCT FROM 'actor_only'
  OR jsonb_typeof(actor->'replayed') IS DISTINCT FROM 'boolean'
  OR jsonb_typeof(ref) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(ref))<>6
  OR NOT ref ?& ARRAY['issuer','jti','binding_hash','credential_hash','key_kid','key_hash']
  OR ref->>'credential_hash' IS DISTINCT FROM auth->>'credential_hash'
  OR ref->>'binding_hash' !~ '^[a-f0-9]{{64}}$' OR ref->>'key_hash' !~ '^[a-f0-9]{{64}}$'
  OR original->'schema_version' IS DISTINCT FROM '1'::jsonb OR original->>'action' IS NULL
  OR original->>'action' NOT IN ('claim.submit','claim.information')
 THEN RETURN jsonb_build_object('status','invalid');END IF;
 SELECT * INTO caller FROM {schema}.internal_caller_credentials WHERE credential_hash=auth->>'credential_hash' FOR SHARE;
 IF NOT FOUND OR NOT caller.active OR caller.audience<>'reference-reimbursement' OR caller.delegation_caller<>'platform-dispatcher' OR NOT caller.allowed_actions ? (original->>'action') THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
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
)) THEN RETURN jsonb_build_object('status','forbidden'); END IF;
 moment:=clock_timestamp();epoch:=floor(extract(epoch FROM moment))::bigint;
 IF actor->>'expires_at' !~ '^[1-9][0-9]*$' OR actor->>'verified_at' !~ '^[1-9][0-9]*$'
  OR jsonb_typeof(actor->'expires_at') IS DISTINCT FROM 'number' OR jsonb_typeof(actor->'verified_at') IS DISTINCT FROM 'number'
  OR (actor->>'verified_at')::bigint>epoch+5 OR moment<signing.not_before OR NOT caller.active
  OR moment>=least(ctx.expires_at,s.expires_at,s.last_seen_at+interval '1800 seconds',caller.expires_at,signing.not_after,to_timestamp((claims->>'exp')::double precision),to_timestamp((actor->>'expires_at')::double precision))
  THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
 expires:=floor(extract(epoch FROM least(moment+interval '60 seconds',ctx.expires_at,s.expires_at,s.last_seen_at+interval '1800 seconds',caller.expires_at,signing.not_after,to_timestamp((claims->>'exp')::double precision),to_timestamp((actor->>'expires_at')::double precision))))::bigint;
 IF expires<=epoch THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
 IF {schema}._receipt_human_current(claims,original) IS NOT TRUE THEN RETURN jsonb_build_object('status','unavailable');END IF;
 IF clock_timestamp()>=to_timestamp(expires) THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
 RETURN jsonb_build_object('status','ok','claims',claims,'caller_ref',caller.caller_ref,'expires_at',expires);
EXCEPTION WHEN data_exception THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}._receipt_authority(jsonb,jsonb) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.receipt_pin_prepare(auth jsonb,request jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE batch jsonb;original jsonb;ident jsonb;permission jsonb;claims jsonb;t uuid;op uuid;person uuid;candidate uuid;
 hash text;entry jsonb;refs jsonb;expires bigint;result jsonb;f {schema}.files%ROWTYPE;
 existing {schema}.receipt_pin_batches%ROWTYPE;k {schema}.receipt_receiver_keys%ROWTYPE;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Receipt capability rejected';END IF;
 IF jsonb_typeof(request) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(request))<>2
  OR NOT request ?& ARRAY['batch','payload'] OR octet_length(request::text)>131072 THEN RETURN jsonb_build_object('status','invalid');END IF;
 batch:=request->'batch';original:=request->'payload';ident:=batch->'identity';
 IF jsonb_typeof(batch) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(batch))<>3 OR NOT batch ?& ARRAY['identity','claim_id','receipts']
  OR jsonb_typeof(ident) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(ident))<>10
  OR NOT ident ?& ARRAY['tenant_id','operation_id','actor_id','caller','kind','key','request_hash','object_kind','object_id','object_version']
  OR ident->>'caller' IS DISTINCT FROM 'platform-dispatcher'
  OR ident->>'kind' IS DISTINCT FROM original->>'action' OR ident->>'tenant_id' IS DISTINCT FROM original->>'tenant_id'
  OR ident->>'operation_id' IS DISTINCT FROM original->>'operation_id' OR ident->>'actor_id' IS DISTINCT FROM original->>'actor_id'
  OR ident->>'object_kind' IS DISTINCT FROM original->'object'->>'kind' OR ident->>'object_id' IS DISTINCT FROM original->'object'->>'id'
  OR ident->'object_version' IS DISTINCT FROM original->'object'->'version'
  OR ident->>'request_hash' IS DISTINCT FROM encode(sha256(convert_to('eai:human-action:v1:'||{schema}._policy_canonical(original),'UTF8')),'hex')
  OR (ident->>'key')::uuid='00000000-0000-0000-0000-000000000000'::uuid
  OR jsonb_typeof(batch->'receipts') IS DISTINCT FROM 'array' OR jsonb_array_length(batch->'receipts')>10
  OR batch->'receipts' IS DISTINCT FROM original->'data'->'receipts'
 THEN RETURN jsonb_build_object('status','invalid');END IF;
 t:=(ident->>'tenant_id')::uuid;op:=(ident->>'operation_id')::uuid;person:=(ident->>'actor_id')::uuid;candidate:=(batch->>'claim_id')::uuid;
 IF t IS NULL OR op IS NULL OR person IS NULL OR candidate IS NULL OR candidate='00000000-0000-0000-0000-000000000000'::uuid
  OR ident->>'kind' NOT IN ('claim.submit','claim.information')
  OR ident->>'object_kind' IS DISTINCT FROM (CASE WHEN ident->>'kind'='claim.submit' THEN 'draft' ELSE 'claim' END)
  OR (ident->>'kind'='claim.information' AND candidate<>(ident->>'object_id')::uuid)
  THEN RETURN jsonb_build_object('status','invalid');END IF;
 permission:={schema}._receipt_authority(auth,original);
 IF permission->>'status' IS DISTINCT FROM 'ok' THEN RETURN permission-'claims';END IF;
 claims:=permission->'claims';
 PERFORM pg_advisory_xact_lock(hashtextextended('receipt-pin:'||t::text||':'||op::text,0));
 hash:=encode(sha256(convert_to('eai:receipt-pin-batch:v1:'||{schema}._policy_canonical(batch),'UTF8')),'hex');
 SELECT * INTO existing FROM {schema}.receipt_pin_batches WHERE tenant_id=t AND operation_id=op FOR UPDATE;
 IF FOUND AND (existing.batch IS DISTINCT FROM batch OR existing.batch_digest<>hash OR existing.caller_ref<>permission->>'caller_ref' OR existing.state<>'held')
  THEN RETURN jsonb_build_object('status','conflict');END IF;
 -- Lock bytes in deterministic order; no concurrent deletion can pass the pin.
 refs:='[]'::jsonb;
 FOR entry IN SELECT value FROM jsonb_array_elements(batch->'receipts') ORDER BY value->>'id' LOOP
  IF jsonb_typeof(entry) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(entry))<>3 OR NOT entry ?& ARRAY['id','version','digest']
   OR jsonb_typeof(entry->'version') IS DISTINCT FROM 'number' OR entry->>'version' !~ '^[1-9][0-9]*$' OR entry->>'digest' !~ '^[a-f0-9]{{64}}$'
   THEN RETURN jsonb_build_object('status','invalid');END IF;
  SELECT * INTO f FROM {schema}.files WHERE tenant_id=t AND id=(entry->>'id')::uuid FOR UPDATE;
  IF NOT FOUND OR f.owner_principal_id<>person OR f.purpose<>'receipt' OR f.scan_state<>'ready'
   OR f.deletion_state NOT IN ('active','retained') OR f.object_version IS NULL OR f.version<>(entry->>'version')::bigint OR f.digest<>entry->>'digest'
   THEN RETURN jsonb_build_object('status','missing');END IF;
  refs:=refs||jsonb_build_array(entry);
 END LOOP;
 IF refs IS DISTINCT FROM batch->'receipts' OR jsonb_array_length(refs)<>(SELECT count(DISTINCT value->>'id') FROM jsonb_array_elements(refs))
  THEN RETURN jsonb_build_object('status','invalid');END IF;
 SELECT * INTO k FROM {schema}.receipt_receiver_keys WHERE credential_hash=auth->>'credential_hash' AND active
  AND clock_timestamp()>=not_before AND clock_timestamp()<not_after ORDER BY not_before DESC,key_id LIMIT 1 FOR SHARE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','unavailable');END IF;
 -- Recheck the original identity/human source after every business lock wait.
 permission:={schema}._receipt_authority(auth,original);
 IF permission->>'status' IS DISTINCT FROM 'ok' THEN RETURN permission-'claims';END IF;
 expires:=least((permission->>'expires_at')::bigint,floor(extract(epoch FROM k.not_after))::bigint);
 IF extract(epoch FROM clock_timestamp())>=expires THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
 IF existing.operation_id IS NULL THEN
  INSERT INTO {schema}.receipt_pin_batches(tenant_id,operation_id,actor_id,candidate_claim_id,caller_ref,batch,batch_digest,authority_expires_at)
   VALUES(t,op,person,candidate,permission->>'caller_ref',batch,hash,to_timestamp(expires));
  INSERT INTO {schema}.receipt_pins(tenant_id,operation_id,file_id,file_version,file_digest)
   SELECT t,op,(value->>'id')::uuid,(value->>'version')::bigint,value->>'digest' FROM jsonb_array_elements(refs);
 ELSE
  UPDATE {schema}.receipt_pin_batches SET authority_expires_at=to_timestamp(expires) WHERE tenant_id=t AND operation_id=op;
 END IF;
 result:=jsonb_build_object('status','ok','batch_digest',hash,'state','held','expires_at',expires,'pins',
  (SELECT coalesce(jsonb_agg(value||jsonb_build_object('pin_operation_ref',op,'operation_id',op,'request_hash',ident->>'request_hash','candidate_claim_id',candidate,'expires_at',expires,
    'attestation_digest',encode({schema}._policy_hmac(k.key_material,convert_to('eai:receipt-attestation:v1:'||{schema}._policy_canonical(
      jsonb_build_object('batch_digest',hash,'receipt',value,'expires_at',expires)),'UTF8')),'hex')) ORDER BY value->>'id'),'[]'::jsonb) FROM jsonb_array_elements(refs)));
 IF extract(epoch FROM clock_timestamp())>=expires THEN RAISE EXCEPTION 'Receipt authority expired' USING ERRCODE='40001';END IF;
 RETURN result;
EXCEPTION WHEN unique_violation THEN RETURN jsonb_build_object('status','conflict');
 WHEN data_exception OR check_violation THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.receipt_pin_prepare(jsonb,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.receipt_pin_prepare(jsonb,jsonb) TO {access_role};

CREATE FUNCTION {schema}.receipt_pin_lookup(credential text,tenant uuid,operation uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE caller {schema}.internal_caller_credentials%ROWTYPE;b {schema}.receipt_pin_batches%ROWTYPE;expires bigint;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Receipt capability rejected';END IF;
 SELECT * INTO caller FROM {schema}.internal_caller_credentials WHERE credential_hash=credential FOR SHARE;
 IF NOT FOUND OR NOT caller.active OR caller.audience<>'reference-reimbursement' OR caller.delegation_caller<>'platform-dispatcher'
  OR clock_timestamp()>=caller.expires_at THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
 SELECT * INTO b FROM {schema}.receipt_pin_batches WHERE tenant_id=tenant AND operation_id=operation AND caller_ref=caller.caller_ref FOR SHARE;
 IF NOT FOUND OR NOT caller.allowed_actions ? (b.batch->'identity'->>'kind') THEN RETURN jsonb_build_object('status','missing');END IF;
 expires:=floor(extract(epoch FROM least(clock_timestamp()+interval '60 seconds',caller.expires_at)))::bigint;
 IF extract(epoch FROM clock_timestamp())>=expires THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
 RETURN jsonb_build_object('status','ok','batch',b.batch,'state',b.state,'terminal_digest',b.terminal_digest,'claim_version',b.claim_version,'expires_at',expires);
END $$;
REVOKE ALL ON FUNCTION {schema}.receipt_pin_lookup(text,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.receipt_pin_lookup(text,uuid,uuid) TO {access_role};

CREATE FUNCTION {schema}.receipt_pin_finalise(credential text,settlement jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE caller {schema}.internal_caller_credentials%ROWTYPE;k {schema}.receipt_receiver_keys%ROWTYPE;b {schema}.receipt_pin_batches%ROWTYPE;
 material jsonb;batch jsonb;term jsonb;ident jsonb;target text;expires bigint;expected numeric;ver bigint;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Receipt capability rejected';END IF;
 IF jsonb_typeof(settlement) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(settlement))<>3
  OR NOT settlement ?& ARRAY['key_id','proof','material'] OR octet_length(settlement::text)>32768
  OR settlement->>'proof' !~ '^[a-f0-9]{{64}}$' THEN RETURN jsonb_build_object('status','invalid');END IF;
 material:=settlement->'material';batch:=material->'batch';term:=material->'terminal';ident:=batch->'identity';
 IF jsonb_typeof(material) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(material))<>4
  OR NOT material ?& ARRAY['schema_version','batch','batch_digest','terminal'] OR material->'schema_version' IS DISTINCT FROM '1'::jsonb
  OR jsonb_typeof(term) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(term))<>8
  OR NOT term ?& ARRAY['identity','state','result_ref','result_version','result_code','completed_at','terminal_digest','expires_at']
  OR term->'identity' IS DISTINCT FROM ident OR term->>'terminal_digest' !~ '^[a-f0-9]{{64}}$'
  OR jsonb_typeof(term->'completed_at') IS DISTINCT FROM 'number' OR term->>'completed_at' !~ '^[1-9][0-9]*$'
  OR jsonb_typeof(term->'expires_at') IS DISTINCT FROM 'number' OR term->>'expires_at' !~ '^[1-9][0-9]*$'
  OR (term->>'completed_at')::numeric>extract(epoch FROM clock_timestamp())+5
  OR (term->>'completed_at')::numeric>=(term->>'expires_at')::numeric
  OR material->>'batch_digest' IS DISTINCT FROM encode(sha256(convert_to('eai:receipt-pin-batch:v1:'||{schema}._policy_canonical(batch),'UTF8')),'hex')
  THEN RETURN jsonb_build_object('status','invalid');END IF;
 SELECT * INTO caller FROM {schema}.internal_caller_credentials WHERE credential_hash=credential FOR SHARE;
 IF NOT FOUND OR NOT caller.active OR caller.audience<>'reference-reimbursement' OR caller.delegation_caller<>'platform-dispatcher'
  OR clock_timestamp()>=caller.expires_at THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
 SELECT * INTO k FROM {schema}.receipt_receiver_keys WHERE credential_hash=credential AND key_id=settlement->>'key_id' FOR SHARE;
 IF NOT FOUND OR NOT k.active OR clock_timestamp()<k.not_before OR clock_timestamp()>=k.not_after
  OR settlement->>'proof' IS DISTINCT FROM encode({schema}._policy_hmac(k.key_material,
   convert_to('eai:receipt-settlement:v1:'||{schema}._policy_canonical(material),'UTF8')),'hex')
  THEN RETURN jsonb_build_object('status','forbidden');END IF;
 SELECT * INTO b FROM {schema}.receipt_pin_batches WHERE tenant_id=(ident->>'tenant_id')::uuid AND operation_id=(ident->>'operation_id')::uuid FOR UPDATE;
 IF NOT FOUND OR b.caller_ref<>caller.caller_ref OR b.batch IS DISTINCT FROM batch OR b.batch_digest<>material->>'batch_digest'
  OR NOT caller.allowed_actions ? (ident->>'kind') THEN RETURN jsonb_build_object('status','missing');END IF;
 IF term->>'state'='committed' THEN
  expected:=CASE WHEN ident->>'kind'='claim.submit' THEN 1 ELSE (ident->>'object_version')::numeric+1 END;
  IF term->>'result_ref' IS DISTINCT FROM b.candidate_claim_id::text
   OR term->>'result_code' IS DISTINCT FROM (CASE WHEN ident->>'kind'='claim.submit' THEN 'claim_submitted' ELSE 'information_received' END)
   OR jsonb_typeof(term->'result_version') IS DISTINCT FROM 'number' OR term->>'result_version' !~ '^[1-9][0-9]*$'
   OR (term->>'result_version')::numeric<>expected THEN RETURN jsonb_build_object('status','forbidden');END IF;
  ver:=(term->>'result_version')::bigint;target:='finalised';
 ELSIF term->>'state'='sealed_no_commit' AND term->'result_ref'='null'::jsonb AND term->'result_version'='null'::jsonb AND term->>'result_code'='sealed_no_commit' THEN
  target:='released';ver:=NULL;
 ELSE RETURN jsonb_build_object('status','forbidden');END IF;
 expires:=least((term->>'expires_at')::bigint,floor(extract(epoch FROM k.not_after))::bigint,
   floor(extract(epoch FROM caller.expires_at))::bigint,floor(extract(epoch FROM clock_timestamp()))::bigint+60);
 IF extract(epoch FROM clock_timestamp())>=expires THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
 IF b.state<>'held' THEN
  IF ROW(b.state,b.terminal_digest,b.claim_version) IS DISTINCT FROM ROW(target,term->>'terminal_digest',ver) THEN RETURN jsonb_build_object('status','conflict');END IF;
 ELSE
  UPDATE {schema}.receipt_pin_batches SET state=target,terminal_digest=term->>'terminal_digest',claim_version=ver,authority_expires_at=to_timestamp(expires)
   WHERE tenant_id=b.tenant_id AND operation_id=b.operation_id;
 END IF;
 IF extract(epoch FROM clock_timestamp())>=expires THEN RAISE EXCEPTION 'Receipt settlement expired' USING ERRCODE='40001';END IF;
 RETURN jsonb_build_object('status','ok','state',target,'terminal_digest',term->>'terminal_digest','claim_version',ver,'expires_at',expires);
EXCEPTION WHEN data_exception THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.receipt_pin_finalise(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.receipt_pin_finalise(text,jsonb) TO {access_role};

-- Preserve prior runtime privileges, adding only exact receipt capabilities.
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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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



