-- SEC09: authenticated source context and claim-bound local receipt gates.
-- Legacy callers still receive false for every service ref; only the staged
-- application verifier can combine remote authority with these local checks.
ALTER FUNCTION {schema}.document_sources_call(jsonb,jsonb,boolean) RENAME TO _document_sources_base;
REVOKE ALL ON FUNCTION {schema}._document_sources_base(jsonb,jsonb,boolean) FROM PUBLIC,{access_role};
CREATE FUNCTION {schema}.document_sources_call(auth jsonb,refs jsonb,history boolean) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE result jsonb;ctx jsonb;proofs jsonb:=coalesce(auth->'service_proofs','[]'::jsonb);original jsonb:=auth-'service_proofs';
 s {schema}.sessions%ROWTYPE;r {schema}.runs%ROWTYPE;item jsonb;anchor jsonb;envelope jsonb;file_result jsonb;eligible jsonb:='[]'::jsonb;ok boolean;
BEGIN
 IF jsonb_typeof(proofs) IS DISTINCT FROM 'array' OR jsonb_array_length(proofs)>8 THEN RETURN jsonb_build_object('status','invalid');END IF;
 result:={schema}._document_sources_base(original,refs,history);
 IF result->>'status' IS DISTINCT FROM 'ok' THEN RETURN result;END IF;
 IF original ? 'token' THEN
  SELECT * INTO s FROM {schema}.sessions WHERE token_hash=original->>'token';
  ctx:=jsonb_build_object('tenant_id',s.active_tenant_id,'actor_id',s.principal_id,'membership_id',(SELECT id FROM {schema}.memberships WHERE tenant_id=s.active_tenant_id AND principal_id=s.principal_id),'session_id',s.id,'access_revision',s.membership_revision,'authority_expires_at',result->'authority_expires_at');
 ELSE
  SELECT * INTO r FROM {schema}.runs WHERE tenant_id=(result->>'tenant_id')::uuid AND id=(original->>'run_id')::uuid;
  SELECT * INTO s FROM {schema}.sessions WHERE id=r.session_id;
  ctx:=jsonb_build_object('tenant_id',r.tenant_id,'actor_id',r.actor_id,'membership_id',r.membership_id,'session_id',r.session_id,'access_revision',r.access_revision,'authority_expires_at',result->'authority_expires_at');
 END IF;
 FOR item IN SELECT value FROM jsonb_array_elements(refs) LOOP
  ok:=false;
  IF item->>'system'='service' AND item->>'kind'='claim' AND item->>'relation' IN ('owner','active_reviewer') AND item->>'version' ~ '^[1-9][0-9]{{0,17}}$' AND item->>'observed_revision' ~ '^[1-9][0-9]{{0,17}}$' THEN ok:=true;END IF;
  IF item->>'system'='service' AND item->>'kind'='receipt' AND item->>'relation' IN ('owner','active_reviewer') THEN
   SELECT value INTO anchor FROM jsonb_array_elements(refs) WHERE value->>'system'='service' AND value->>'kind'='claim' AND value->>'relation'=item->>'relation' AND value->>'observed_revision'=item->>'observed_revision';
   IF (SELECT count(*) FROM jsonb_array_elements(refs) WHERE value->>'system'='service' AND value->>'kind'='claim' AND value->>'relation'=item->>'relation')=1 THEN
    FOR envelope IN SELECT value FROM jsonb_array_elements(proofs) LOOP
     IF envelope->'material'->'access'->>'claim_id'=anchor->>'resource_id' AND envelope->'material'->'access'->>'claim_version'=anchor->>'version' AND envelope->'material'->'access'->>'revision'=anchor->>'observed_revision' THEN
      file_result:={schema}.receipt_file_call(s.token_hash,'reference',jsonb_build_object('id',item->>'resource_id','claim_id',anchor->>'resource_id','attestation',envelope));
      IF file_result->>'status'='ok' AND file_result->>'version'=item->>'version' THEN ok:=true;END IF;
     END IF;
    END LOOP;
   END IF;
  END IF;
  eligible:=eligible||jsonb_build_array(ok);
 END LOOP;
 RETURN result||jsonb_build_object('service_context',ctx,'service_eligible',eligible);
EXCEPTION WHEN data_exception THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.document_sources_call(jsonb,jsonb,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.document_sources_call(jsonb,jsonb,boolean) TO {access_role};
