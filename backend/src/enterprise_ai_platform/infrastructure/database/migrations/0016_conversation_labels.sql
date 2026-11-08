-- BD-FE-04: display metadata only; no message mutation or model title generation.
ALTER TABLE {schema}.conversations ADD COLUMN manual_name text
 CHECK(manual_name IS NULL OR (length(btrim(manual_name,E' \r\n\t')) BETWEEN 1 AND 4096
  AND length(manual_name)<=4096 AND octet_length(manual_name)<=8192
  AND translate(manual_name,E'\r\n\t','') !~ '[[:cntrl:]]'));

ALTER TABLE {schema}.conversation_requests DROP CONSTRAINT conversation_requests_operation_check;
ALTER TABLE {schema}.conversation_requests DROP CONSTRAINT conversation_requests_check;
ALTER TABLE {schema}.conversation_requests ADD CONSTRAINT conversation_requests_operation_check CHECK(operation IN ('create','feedback','rename'));
ALTER TABLE {schema}.conversation_requests ADD CONSTRAINT conversation_requests_check
 CHECK((operation IN ('create','rename') AND message_id IS NULL) OR (operation='feedback' AND message_id IS NOT NULL));

CREATE OR REPLACE FUNCTION {schema}._conversation_json(c {schema}.conversations) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('id',c.id,'language',c.language,'version',c.version,'active_run_id',c.active_run_id,
  'created_at',c.created_at,'updated_at',c.updated_at,'label',coalesce(c.manual_name,q.body),
  'label_source',CASE WHEN c.manual_name IS NOT NULL THEN 'manual' WHEN q.body IS NOT NULL THEN 'first_question' ELSE 'empty' END)
 FROM (SELECT (SELECT m.body FROM {schema}.messages m WHERE m.tenant_id=c.tenant_id AND m.conversation_id=c.id
   AND m.author_kind='human' ORDER BY m.sequence LIMIT 1) AS body) q;
$$;
REVOKE ALL ON FUNCTION {schema}._conversation_json({schema}.conversations) FROM PUBLIC,{access_role};

-- Keep the reviewed admission/history implementation private. The narrow public
-- capability name/signature and runtime role allowlist do not expand.
ALTER FUNCTION {schema}.conversation_call(text,text,jsonb) RENAME TO _conversation_v11_call;
REVOKE ALL ON FUNCTION {schema}._conversation_v11_call(text,text,jsonb) FROM PUBLIC,{access_role};
CREATE FUNCTION {schema}.conversation_call(token text,operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE context jsonb;identity jsonb;t uuid;p uuid;member uuid;c {schema}.conversations%ROWTYPE;
 e {schema}.conversation_requests%ROWTYPE;request_hash text;result jsonb;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Conversation capability rejected.'; END IF;
 IF operation IS DISTINCT FROM 'rename' THEN RETURN {schema}._conversation_v11_call(token,operation,payload); END IF;
 IF payload IS NULL OR jsonb_typeof(payload)<>'object' OR octet_length(payload::text)>32768
  OR (SELECT count(*) FROM jsonb_object_keys(payload))<>5 OR NOT payload ?& ARRAY['id','name','expected_version','key','csrf_hash']
  OR jsonb_typeof(payload->'id') IS DISTINCT FROM 'string' OR jsonb_typeof(payload->'key') IS DISTINCT FROM 'string'
  OR jsonb_typeof(payload->'name') IS DISTINCT FROM 'string'
  OR length(btrim(payload->>'name',E' \r\n\t')) NOT BETWEEN 1 AND 4096 OR length(payload->>'name')>4096
  OR octet_length(payload->>'name')>8192 OR translate(payload->>'name',E'\r\n\t','') ~ '[[:cntrl:]]'
  OR jsonb_typeof(payload->'expected_version') IS DISTINCT FROM 'number' OR payload->>'expected_version' !~ '^[1-9][0-9]{{0,17}}$'
  THEN RETURN jsonb_build_object('status','invalid'); END IF;
 context:={schema}.authorization_call(token,'context',jsonb_build_object('csrf_hash',payload->>'csrf_hash'));
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RETURN context; END IF;
 identity:=context->'identity';t:=(identity->>'active_tenant_id')::uuid;p:=(identity->>'principal_id')::uuid;member:=(identity->>'active_membership_id')::uuid;
 IF NOT EXISTS(SELECT 1 FROM {schema}.membership_roles g WHERE g.tenant_id=t AND g.membership_id=member
  AND g.role='employee' AND g.scope_kind='tenant' AND g.scope_ref=t::text) THEN RETURN jsonb_build_object('status','missing'); END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('eai:conversation-key:'||t::text||':'||p::text||':'||((payload->>'key')::uuid)::text,0));
 SELECT * INTO c FROM {schema}.conversations WHERE tenant_id=t AND id=(payload->>'id')::uuid AND owner_principal_id=p FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','missing'); END IF;
 request_hash:=encode(sha256(convert_to((payload-'csrf_hash')::text,'UTF8')),'hex');
 SELECT * INTO e FROM {schema}.conversation_requests WHERE tenant_id=t AND actor_id=p AND key=(payload->>'key')::uuid;
 IF FOUND THEN
  IF e.operation<>'rename' OR e.request_hash<>request_hash THEN RETURN jsonb_build_object('status','conflict'); END IF;
  result:=e.response;
 ELSE
  IF c.version<>(payload->>'expected_version')::bigint THEN RETURN jsonb_build_object('status','conflict'); END IF;
  UPDATE {schema}.conversations SET manual_name=payload->>'name',version=version+1,updated_at=clock_timestamp()
   WHERE tenant_id=t AND id=c.id RETURNING * INTO c;
  result:=jsonb_build_object('id',c.id,'label',c.manual_name,'label_source','manual','version',c.version);
  INSERT INTO {schema}.conversation_requests(tenant_id,actor_id,key,operation,request_hash,conversation_id,response)
   VALUES(t,p,(payload->>'key')::uuid,'rename',request_hash,c.id,result);
 END IF;
 context:={schema}.authorization_call(token,'finish',jsonb_build_object('csrf_hash',payload->>'csrf_hash'));
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'Conversation authority expired.' USING ERRCODE='40001'; END IF;
 RETURN result||jsonb_build_object('status','ok','tenant_id',t,'principal_id',p,'authority_expires_at',
  (SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) FROM {schema}.sessions WHERE token_hash=token));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR not_null_violation OR check_violation
 OR unique_violation OR foreign_key_violation OR invalid_parameter_value THEN RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}.conversation_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.conversation_call(text,text,jsonb) TO {access_role};
