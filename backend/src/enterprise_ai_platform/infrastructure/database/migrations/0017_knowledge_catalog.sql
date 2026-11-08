-- BD-FE-02: manager workspace reads, separate from published reader search.
CREATE FUNCTION {schema}._knowledge_workspace_version(d {schema}.documents,v {schema}.document_versions) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT ({schema}._knowledge_json(d,v)-'status')||jsonb_build_object('file_id',v.file_id,'file_digest',v.file_digest,
  'content_digest',v.content_digest,'index_generation',v.index_generation,'created_at',v.created_at);
$$;
REVOKE ALL ON FUNCTION {schema}._knowledge_workspace_version({schema}.documents,{schema}.document_versions) FROM PUBLIC,{access_role};
CREATE FUNCTION {schema}._knowledge_workspace_document(d {schema}.documents) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('document_id',d.id,'title',d.title,'collection_id',d.collection_id,'document_version',d.version,
  'access_revision',d.access_revision,'active_version_id',d.active_version_id,'created_at',d.created_at,
  'active_version',(SELECT {schema}._knowledge_workspace_version(d,v) FROM {schema}.document_versions v WHERE v.tenant_id=d.tenant_id AND v.id=d.active_version_id),
  'latest_version',(SELECT {schema}._knowledge_workspace_version(d,v) FROM {schema}.document_versions v WHERE v.tenant_id=d.tenant_id AND v.document_id=d.id ORDER BY v.version_no DESC LIMIT 1));
$$;
REVOKE ALL ON FUNCTION {schema}._knowledge_workspace_document({schema}.documents) FROM PUBLIC,{access_role};
ALTER FUNCTION {schema}.knowledge_call(text,text,jsonb) RENAME TO _knowledge_v10_call;
REVOKE ALL ON FUNCTION {schema}._knowledge_v10_call(text,text,jsonb) FROM PUBLIC,{access_role};
CREATE FUNCTION {schema}.knowledge_call(token text,operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE context jsonb;identity jsonb;t uuid;p uuid;d {schema}.documents%ROWTYPE;a {schema}.documents%ROWTYPE;
 v {schema}.document_versions%ROWTYPE;av {schema}.document_versions%ROWTYPE;f {schema}.files%ROWTYPE;
 result jsonb;items jsonb;limit_rows integer;cursor_id uuid;entry record;collection uuid;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Knowledge capability rejected.'; END IF;
 IF operation IS NULL OR operation NOT IN ('catalog','detail','versions','preview','preview_recheck') THEN
  RETURN {schema}._knowledge_v10_call(token,operation,payload);
 END IF;
 IF payload IS NULL OR jsonb_typeof(payload)<>'object' OR octet_length(payload::text)>4096 THEN RETURN jsonb_build_object('status','invalid'); END IF;
 context:={schema}.authorization_call(token,'context','{{}}'::jsonb);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RETURN context; END IF;
 identity:=context->'identity';t:=(identity->>'active_tenant_id')::uuid;p:=(identity->>'principal_id')::uuid;
 IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(context->'grants') g WHERE g->>'role'='knowledge_manager') THEN
  RETURN jsonb_build_object('status',CASE WHEN operation='catalog' THEN 'forbidden' ELSE 'missing' END);
 END IF;
 IF operation='catalog' THEN
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>5 OR NOT payload ?& ARRAY['after','limit','query','collection_id','tenant_only']
   OR jsonb_typeof(payload->'limit') IS DISTINCT FROM 'number' OR payload->>'limit' !~ '^([1-9]|[1-4][0-9]|50)$'
   OR (payload->'after'<>'null'::jsonb AND jsonb_typeof(payload->'after') IS DISTINCT FROM 'string')
   OR (payload->'collection_id'<>'null'::jsonb AND jsonb_typeof(payload->'collection_id') IS DISTINCT FROM 'string')
   OR jsonb_typeof(payload->'tenant_only') IS DISTINCT FROM 'boolean'
   OR (payload->'query'<>'null'::jsonb AND (jsonb_typeof(payload->'query') IS DISTINCT FROM 'string'
     OR length(btrim(payload->>'query')) NOT BETWEEN 1 AND 200 OR octet_length(payload->>'query')>800 OR payload->>'query' ~ '[[:cntrl:]]'))
   THEN RETURN jsonb_build_object('status','invalid'); END IF;
  collection:=(payload->>'collection_id')::uuid;limit_rows:=(payload->>'limit')::integer;
  IF collection IS NOT NULL AND (payload->>'tenant_only')::boolean THEN RETURN jsonb_build_object('status','invalid'); END IF;
  IF (collection IS NOT NULL OR (payload->>'tenant_only')::boolean) AND NOT {schema}._files_allowed(t,p,'knowledge',collection) THEN RETURN jsonb_build_object('status','missing'); END IF;
  IF payload->'after'<>'null'::jsonb THEN
   SELECT * INTO a FROM {schema}.documents WHERE tenant_id=t AND id=(payload->>'after')::uuid AND withdrawn_at IS NULL
    AND {schema}._files_allowed(t,p,'knowledge',collection_id)
    AND (collection IS NULL OR collection_id=collection) AND (NOT (payload->>'tenant_only')::boolean OR collection_id IS NULL)
    AND (payload->'query'='null'::jsonb OR strpos(lower(title),lower(payload->>'query'))>0) FOR SHARE;
   IF NOT FOUND THEN RETURN jsonb_build_object('status','missing'); END IF;
   IF a.collection_id IS NOT NULL THEN PERFORM scope_ref FROM {schema}.authorization_scopes WHERE tenant_id=t AND scope_ref=a.collection_id FOR SHARE; END IF;
   IF NOT {schema}._files_allowed(t,p,'knowledge',a.collection_id) THEN RETURN jsonb_build_object('status','conflict'); END IF;
  END IF;
  items:='[]'::jsonb;
  FOR entry IN SELECT dd.id FROM {schema}.documents dd WHERE dd.tenant_id=t AND dd.withdrawn_at IS NULL
   AND {schema}._files_allowed(t,p,'knowledge',dd.collection_id)
   AND (collection IS NULL OR dd.collection_id=collection) AND (NOT (payload->>'tenant_only')::boolean OR dd.collection_id IS NULL)
   AND (payload->'query'='null'::jsonb OR strpos(lower(dd.title),lower(payload->>'query'))>0)
   AND (a.id IS NULL OR ROW(dd.created_at,dd.id)<ROW(a.created_at,a.id))
   ORDER BY dd.created_at DESC,dd.id DESC LIMIT limit_rows+1 FOR SHARE OF dd
  LOOP
   SELECT * INTO d FROM {schema}.documents WHERE tenant_id=t AND id=entry.id;
   IF d.collection_id IS NOT NULL THEN PERFORM scope_ref FROM {schema}.authorization_scopes WHERE tenant_id=t AND scope_ref=d.collection_id FOR SHARE; END IF;
   -- A scope changed while its lock was pending. Reject the whole page rather
   -- than dropping rows after LIMIT and inventing an end-of-list cursor.
   IF NOT {schema}._files_allowed(t,p,'knowledge',d.collection_id) THEN RETURN jsonb_build_object('status','conflict'); END IF;
   items:=items||jsonb_build_array({schema}._knowledge_workspace_document(d));
  END LOOP;
  IF jsonb_array_length(items)>limit_rows THEN items:=items-limit_rows;cursor_id:=(items->(limit_rows-1)->>'document_id')::uuid; END IF;
  result:=jsonb_build_object('items',items,'next_after',cursor_id);
 ELSE
  IF NOT payload ? 'id' OR jsonb_typeof(payload->'id') IS DISTINCT FROM 'string' THEN RETURN jsonb_build_object('status','invalid'); END IF;
  SELECT * INTO d FROM {schema}.documents WHERE tenant_id=t AND id=(payload->>'id')::uuid FOR SHARE;
  IF d.id IS NULL OR d.withdrawn_at IS NOT NULL THEN RETURN jsonb_build_object('status','missing'); END IF;
  IF d.collection_id IS NOT NULL THEN PERFORM scope_ref FROM {schema}.authorization_scopes WHERE tenant_id=t AND scope_ref=d.collection_id FOR SHARE; END IF;
  IF NOT {schema}._files_allowed(t,p,'knowledge',d.collection_id) THEN RETURN jsonb_build_object('status','missing'); END IF;
  IF operation='detail' THEN
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>1 THEN RETURN jsonb_build_object('status','invalid'); END IF;
   result:={schema}._knowledge_workspace_document(d);
  ELSIF operation='versions' THEN
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>3 OR NOT payload ?& ARRAY['after','limit']
    OR jsonb_typeof(payload->'limit') IS DISTINCT FROM 'number' OR payload->>'limit' !~ '^([1-9]|[1-4][0-9]|50)$'
    OR (payload->'after'<>'null'::jsonb AND jsonb_typeof(payload->'after') IS DISTINCT FROM 'string') THEN RETURN jsonb_build_object('status','invalid'); END IF;
   limit_rows:=(payload->>'limit')::integer;
   IF payload->'after'<>'null'::jsonb THEN
    SELECT * INTO av FROM {schema}.document_versions WHERE tenant_id=t AND document_id=d.id AND id=(payload->>'after')::uuid;
    IF NOT FOUND THEN RETURN jsonb_build_object('status','missing'); END IF;
   END IF;
   SELECT coalesce(jsonb_agg(value ORDER BY version_no DESC),'[]'::jsonb) INTO items FROM (
    SELECT {schema}._knowledge_workspace_version(d,vv) value,vv.version_no FROM {schema}.document_versions vv
     WHERE vv.tenant_id=t AND vv.document_id=d.id AND (av.id IS NULL OR vv.version_no<av.version_no)
     ORDER BY vv.version_no DESC LIMIT limit_rows+1) q;
   IF jsonb_array_length(items)>limit_rows THEN items:=items-limit_rows;cursor_id:=(items->(limit_rows-1)->>'version_id')::uuid; END IF;
   result:=jsonb_build_object('document_id',d.id,'document_version',d.version,'items',items,'next_after',cursor_id);
  ELSE
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>(CASE WHEN operation='preview' THEN 2 ELSE 5 END)
    OR NOT payload ? 'version_id' OR jsonb_typeof(payload->'version_id') IS DISTINCT FROM 'string' THEN RETURN jsonb_build_object('status','invalid'); END IF;
   SELECT * INTO v FROM {schema}.document_versions WHERE tenant_id=t AND document_id=d.id AND id=(payload->>'version_id')::uuid FOR SHARE;
   IF NOT FOUND THEN RETURN jsonb_build_object('status','missing'); END IF;
   SELECT * INTO f FROM {schema}.files WHERE tenant_id=t AND id=v.file_id FOR SHARE;
   IF f.id IS NULL OR f.purpose<>'knowledge' OR f.scan_state<>'ready' OR f.deletion_state NOT IN ('active','retained')
    OR ROW(f.digest,f.object_version,f.size_bytes,f.mime,f.collection_id) IS DISTINCT FROM ROW(v.file_digest,v.object_version,v.size_bytes,v.mime,d.collection_id)
    THEN RETURN jsonb_build_object('status','missing'); END IF;
   IF NOT {schema}._knowledge_complete(v) THEN RETURN jsonb_build_object('status','conflict'); END IF;
   -- Workers lock job before document. Never wait in the reverse order: pending
   -- versions already returned409, and a contested completed job also returns409.
   PERFORM id FROM {schema}.outbox_jobs WHERE tenant_id=t AND id=v.job_id FOR SHARE NOWAIT;
   IF NOT {schema}._knowledge_complete(v) THEN RETURN jsonb_build_object('status','conflict'); END IF;
   IF operation='preview_recheck' AND (NOT payload ?& ARRAY['access_revision','index_generation','content_digest']
    OR jsonb_typeof(payload->'access_revision') IS DISTINCT FROM 'number' OR jsonb_typeof(payload->'index_generation') IS DISTINCT FROM 'number'
    OR jsonb_typeof(payload->'content_digest') IS DISTINCT FROM 'string'
    OR ROW(v.index_generation,v.content_digest,d.access_revision) IS DISTINCT FROM ROW((payload->>'index_generation')::bigint,payload->>'content_digest',(payload->>'access_revision')::bigint))
    THEN RETURN jsonb_build_object('status','conflict'); END IF;
   result:={schema}._knowledge_source(d,v)||jsonb_build_object('preview',true);
  END IF;
 END IF;
 context:={schema}.authorization_call(token,'finish','{{}}'::jsonb);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'Knowledge authority expired.' USING ERRCODE='40001'; END IF;
 RETURN result||jsonb_build_object('status','ok','authority_expires_at',
  (SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) FROM {schema}.sessions WHERE token_hash=token));
EXCEPTION WHEN lock_not_available THEN RETURN jsonb_build_object('status','conflict');
 WHEN invalid_text_representation OR numeric_value_out_of_range OR not_null_violation OR check_violation
 OR unique_violation OR foreign_key_violation OR invalid_parameter_value THEN RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}.knowledge_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.knowledge_call(text,text,jsonb) TO {access_role};
