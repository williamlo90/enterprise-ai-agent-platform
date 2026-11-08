-- DATA01: preserve immutable v1 history; reject approval that expires during lock waits.
CREATE OR REPLACE FUNCTION {schema}.privacy_policy_call(token text,operation text,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
#variable_conflict use_column
<<privacy>>
DECLARE auth jsonb;i jsonb;t uuid;person uuid;key uuid;deadline numeric;hash text;latest bigint;record jsonb;result jsonb;moment timestamptz;prior {schema}.privacy_policy_decisions%ROWTYPE;writing boolean;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Privacy capability rejected';END IF;
 IF operation IS NULL OR operation NOT IN ('lock','current','effective','publish') OR jsonb_typeof(payload) IS DISTINCT FROM 'object' OR octet_length(payload::text)>65536 THEN RETURN jsonb_build_object('status','invalid');END IF;
 writing:=operation IN ('lock','publish');
 SELECT principal_id,active_tenant_id INTO person,t FROM {schema}.sessions WHERE token_hash=token;
 IF person IS NULL OR t IS NULL THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
 IF writing THEN
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>5 OR NOT payload ?& ARRAY['key','csrf_hash','revision','expires_at','rules']
   OR jsonb_typeof(payload->'key') IS DISTINCT FROM 'string' OR (payload->>'key')::uuid='00000000-0000-0000-0000-000000000000'::uuid
   OR jsonb_typeof(payload->'revision') IS DISTINCT FROM 'number' OR (payload->>'revision') !~ '^[0-9]+$' OR (payload->>'revision')::numeric NOT BETWEEN 1 AND 999999999999999999
   OR jsonb_typeof(payload->'expires_at') IS DISTINCT FROM 'number' OR (payload->>'expires_at')::numeric>253402300799
   OR (payload->>'expires_at')::numeric<=extract(epoch FROM clock_timestamp()) OR NOT {schema}._privacy_rules_valid(payload->'rules') THEN RETURN jsonb_build_object('status','invalid');END IF;
  key:=(payload->>'key')::uuid;
  PERFORM pg_advisory_xact_lock(hashtextextended('authorization-role:'||t::text,0));
  PERFORM id FROM {schema}.principals WHERE id=person FOR UPDATE;
 ELSIF payload<>'{{}}'::jsonb THEN RETURN jsonb_build_object('status','invalid');END IF;
 auth:={schema}.authorization_call(token,'context',CASE WHEN writing THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
 IF auth->>'status' IS DISTINCT FROM 'ok' THEN RETURN auth;END IF;
 i:=auth->'identity';
 IF i->>'active_tenant_id' IS DISTINCT FROM t::text OR i->>'principal_id' IS DISTINCT FROM person::text THEN RETURN jsonb_build_object('status','conflict');END IF;
 IF operation<>'effective' AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(auth->'grants') g WHERE g->>'role'='operator_scoped' AND g->>'scope_kind'='operations' AND g->>'scope_ref'='config') THEN RETURN jsonb_build_object('status','forbidden');END IF;
 SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) INTO deadline FROM {schema}.sessions WHERE token_hash=token;
 IF operation='lock' THEN RETURN jsonb_build_object('status','ok','authority_expires_at',deadline);END IF;
 IF NOT writing THEN
  SELECT policy,revision INTO record,latest FROM {schema}.privacy_policies WHERE tenant_id=t ORDER BY revision DESC LIMIT 1;
  IF operation='effective' AND (record IS NULL OR (record->>'expires_at')::numeric<=extract(epoch FROM clock_timestamp())) THEN RETURN jsonb_build_object('status','held');END IF;
  RETURN jsonb_build_object('status','ok','policy',record,'revision',coalesce(latest,0),'authority_expires_at',deadline);
 END IF;
 hash:=encode(sha256(convert_to((payload-'csrf_hash')::text,'UTF8')),'hex');
 SELECT * INTO prior FROM {schema}.privacy_policy_decisions WHERE tenant_id=t AND actor_id=person AND privacy_policy_decisions.key=privacy.key;
 IF FOUND THEN
  IF prior.request_hash<>hash THEN RETURN jsonb_build_object('status','conflict');END IF;
  RETURN prior.response||jsonb_build_object('status','ok','replayed',true,'authority_expires_at',deadline);
 END IF;
 SELECT coalesce(max(revision),0) INTO latest FROM {schema}.privacy_policies WHERE tenant_id=t;
 IF (payload->>'revision')::bigint<>latest+1 THEN RETURN jsonb_build_object('status','conflict');END IF;
 moment:=clock_timestamp();
 IF (payload->>'expires_at')::numeric<=extract(epoch FROM moment) THEN RETURN jsonb_build_object('status','invalid');END IF;
 record:=jsonb_build_object('tenant_id',t,'revision',latest+1,'approved_by',person,'approved_at',extract(epoch FROM moment),'expires_at',payload->'expires_at','rules',payload->'rules');
 result:=jsonb_build_object('policy',record,'revision',latest+1,'receipt',jsonb_build_object('key',key,'request_hash',hash,'policy_hash',encode(sha256(convert_to(record::text,'UTF8')),'hex')));
 INSERT INTO {schema}.privacy_policies(tenant_id,revision,policy,approved_by,approved_at) VALUES(t,latest+1,record,person,moment);
 INSERT INTO {schema}.privacy_policy_decisions(tenant_id,actor_id,key,request_hash,revision,response) VALUES(t,person,key,hash,latest+1,result);
 RETURN result||jsonb_build_object('status','ok','replayed',false,'authority_expires_at',deadline);
EXCEPTION WHEN data_exception THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.privacy_policy_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.privacy_policy_call(text,text,jsonb) TO {access_role};

