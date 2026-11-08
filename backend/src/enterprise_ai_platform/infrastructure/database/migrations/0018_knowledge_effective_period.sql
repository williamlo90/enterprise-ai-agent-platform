-- BD-FE-02: manager-supplied RAG source period, not business-transaction eligibility.
-- Reuse valid_from/valid_until from0013: start inclusive, end exclusive.
ALTER TABLE {schema}.knowledge_requests ADD COLUMN effective_period jsonb NOT NULL DEFAULT '{{}}'::jsonb
 CHECK(jsonb_typeof(effective_period)='object' AND effective_period-'valid_from'-'valid_until'='{{}}'::jsonb AND octet_length(effective_period::text)<=100);

CREATE OR REPLACE FUNCTION {schema}._knowledge_json(d {schema}.documents,v {schema}.document_versions) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('status','ok','document_id',d.id,'document_version',d.version,'version_id',v.id,'version_no',v.version_no,
 'parse_state',v.parse_state,'index_state',v.index_state,'publication_state',v.publication_state,'chunk_count',v.chunk_count,
 'embedding_model',v.embedding_model,'dimensions',v.dimensions,'chunk_config',v.chunk_config,'parser_ref',v.parser_ref,'error_code',v.error_code,
 'valid_from',v.valid_from,'valid_until',v.valid_until);
$$;
REVOKE ALL ON FUNCTION {schema}._knowledge_json({schema}.documents,{schema}.document_versions) FROM PUBLIC,{access_role};
CREATE OR REPLACE FUNCTION {schema}._knowledge_source(d {schema}.documents,v {schema}.document_versions) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('document_id',d.id,'title',d.title,'version_id',v.id,'version_no',v.version_no,'publication_state',v.publication_state,
  'access_revision',d.access_revision,'index_generation',v.index_generation,'content_digest',v.content_digest,'file_digest',v.file_digest,
  'embedding_model',v.embedding_model,'chunk_config',v.chunk_config,'parser_ref',v.parser_ref,'format','extracted_chunks',
  'valid_from',v.valid_from,'valid_until',v.valid_until,
  'chunks',coalesce((SELECT jsonb_agg(jsonb_build_object('chunk_no',c.chunk_no,'page',c.page,'start',c.start_offset,'end',c.end_offset,'text',c.text,'text_digest',c.text_digest) ORDER BY c.chunk_no)
   FROM {schema}.chunks c WHERE c.tenant_id=v.tenant_id AND c.document_version_id=v.id AND c.index_generation=v.index_generation),'[]'::jsonb));
$$;
REVOKE ALL ON FUNCTION {schema}._knowledge_source({schema}.documents,{schema}.document_versions) FROM PUBLIC,{access_role};

ALTER FUNCTION {schema}.knowledge_call(text,text,jsonb) RENAME TO _knowledge_v17_call;
REVOKE ALL ON FUNCTION {schema}._knowledge_v17_call(text,text,jsonb) FROM PUBLIC,{access_role};
CREATE FUNCTION {schema}.knowledge_call(token text,operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE context jsonb;identity jsonb;t uuid;p uuid;period jsonb;prior {schema}.knowledge_requests%ROWTYPE;
 d {schema}.documents%ROWTYPE;v {schema}.document_versions%ROWTYPE;result jsonb;start_day date;end_day date;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Knowledge capability rejected.'; END IF;
 IF operation IS NULL OR operation NOT IN ('create','version') THEN RETURN {schema}._knowledge_v17_call(token,operation,payload); END IF;
 IF payload IS NULL OR jsonb_typeof(payload)<>'object' OR octet_length(payload::text)>16600 THEN RETURN jsonb_build_object('status','invalid'); END IF;
 IF (payload ? 'valid_from' AND payload->'valid_from'<>'null'::jsonb AND
    (jsonb_typeof(payload->'valid_from') IS DISTINCT FROM 'string' OR payload->>'valid_from' !~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'))
  OR (payload ? 'valid_until' AND payload->'valid_until'<>'null'::jsonb AND
    (jsonb_typeof(payload->'valid_until') IS DISTINCT FROM 'string' OR payload->>'valid_until' !~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'))
  THEN RETURN jsonb_build_object('status','invalid'); END IF;
 start_day:=(payload->>'valid_from')::date;end_day:=(payload->>'valid_until')::date;
 IF start_day IS NOT NULL AND end_day IS NOT NULL AND end_day<=start_day THEN RETURN jsonb_build_object('status','invalid'); END IF;
 period:=jsonb_strip_nulls(jsonb_build_object('valid_from',start_day,'valid_until',end_day));
 context:={schema}.authorization_call(token,'context',jsonb_build_object('csrf_hash',payload->>'csrf_hash'));
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RETURN context; END IF;
 identity:=context->'identity';t:=(identity->>'active_tenant_id')::uuid;p:=(identity->>'principal_id')::uuid;
 PERFORM pg_advisory_xact_lock(hashtextextended('eai:knowledge-key:'||t::text||':'||p::text||':'||((payload->>'key')::uuid)::text,0));
 SELECT * INTO prior FROM {schema}.knowledge_requests WHERE tenant_id=t AND actor_id=p AND key=(payload->>'key')::uuid;
 -- The reviewed capability first reauthorizes the file/document and ordinary
 -- request material. No receipt escapes just because this metadata matches.
 result:={schema}._knowledge_v17_call(token,operation,payload-'valid_from'-'valid_until');
 IF result->>'status' IS DISTINCT FROM 'ok' THEN RETURN result; END IF;
 IF prior.key IS NOT NULL THEN
  IF prior.effective_period IS DISTINCT FROM period THEN RETURN jsonb_build_object('status','conflict'); END IF;
 ELSE
  UPDATE {schema}.document_versions SET valid_from=start_day,valid_until=end_day
   WHERE tenant_id=t AND id=(result->>'version_id')::uuid RETURNING * INTO v;
  IF NOT FOUND THEN RAISE EXCEPTION 'Knowledge metadata admission rejected.' USING ERRCODE='40001'; END IF;
  UPDATE {schema}.knowledge_requests SET effective_period=period WHERE tenant_id=t AND actor_id=p AND key=(payload->>'key')::uuid;
  SELECT * INTO d FROM {schema}.documents WHERE tenant_id=t AND id=v.document_id;
  result:={schema}._knowledge_json(d,v);
 END IF;
 context:={schema}.authorization_call(token,'finish',jsonb_build_object('csrf_hash',payload->>'csrf_hash'));
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'Knowledge authority expired.' USING ERRCODE='40001'; END IF;
 RETURN result||jsonb_build_object('authority_expires_at',
  (SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) FROM {schema}.sessions WHERE token_hash=token));
EXCEPTION WHEN invalid_text_representation OR invalid_datetime_format OR datetime_field_overflow OR numeric_value_out_of_range
 OR not_null_violation OR check_violation OR unique_violation OR foreign_key_violation OR invalid_parameter_value
 THEN RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}.knowledge_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.knowledge_call(text,text,jsonb) TO {access_role};
