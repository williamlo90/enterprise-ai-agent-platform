-- CLAIM-01: normalized claim history and authoritative expense reservations.
-- Forward-only. Existing service migrations0001-0003 remain immutable.
ALTER TABLE {schema}.trip_expense_items ADD UNIQUE(tenant_id,id,trip_id,owner_principal_ref);

CREATE TABLE {schema}.claims (
 tenant_id uuid NOT NULL REFERENCES {schema}.tenant_registry(tenant_id),
 id uuid NOT NULL,
 public_reference text NOT NULL CHECK(length(public_reference) BETWEEN 1 AND 64),
 owner_principal_ref uuid NOT NULL,
 trip_id uuid NOT NULL,
 policy_id uuid NOT NULL,
 policy_version bigint NOT NULL CHECK(policy_version>0),
 policy_digest text NOT NULL CHECK(policy_digest ~ '^[a-f0-9]{{64}}$'),
 state text NOT NULL CHECK(state IN ('pending_approval','needs_information','approved','rejected')),
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 current_revision_no bigint NOT NULL CHECK(current_revision_no>0),
 active_stage_no integer,
 submitted_at timestamptz NOT NULL,
 previous_claim_id uuid,
 PRIMARY KEY(tenant_id,id),
 UNIQUE(tenant_id,public_reference),
 UNIQUE(tenant_id,id,trip_id,owner_principal_ref),
 FOREIGN KEY(tenant_id,trip_id,owner_principal_ref) REFERENCES {schema}.trips(tenant_id,id,owner_principal_ref),
 FOREIGN KEY(tenant_id,policy_id,policy_version,policy_digest)
   REFERENCES {schema}.service_policy_packages(tenant_id,package_id,package_version,digest),
 FOREIGN KEY(tenant_id,previous_claim_id,trip_id,owner_principal_ref)
   REFERENCES {schema}.claims(tenant_id,id,trip_id,owner_principal_ref),
 CHECK(previous_claim_id IS NULL OR previous_claim_id<>id),
 CHECK((state='pending_approval' AND active_stage_no IS NOT NULL AND active_stage_no BETWEEN 1 AND 10)
    OR (state<>'pending_approval' AND active_stage_no IS NULL))
);
CREATE INDEX claims_owner_status ON {schema}.claims(tenant_id,owner_principal_ref,state,submitted_at,id);

CREATE TABLE {schema}.claim_revisions (
 tenant_id uuid NOT NULL,
 claim_id uuid NOT NULL,
 revision_no bigint NOT NULL CHECK(revision_no>0),
 payload_hash text NOT NULL CHECK(payload_hash ~ '^[a-f0-9]{{64}}$'),
 operation_id uuid NOT NULL,
 claim_version bigint NOT NULL CHECK(claim_version>0),
 total_minor bigint NOT NULL CHECK(total_minor>0),
 currency text NOT NULL CHECK(currency IN ('IDR','SGD','USD')),
 consent_ref uuid NOT NULL,
 human_action_ref uuid NOT NULL,
 submitted_by_ref uuid NOT NULL,
 source_draft_id uuid NOT NULL,
 source_draft_version bigint NOT NULL CHECK(source_draft_version>0),
 received_at timestamptz NOT NULL,
 PRIMARY KEY(tenant_id,claim_id,revision_no),
 UNIQUE(tenant_id,consent_ref),
 UNIQUE(tenant_id,human_action_ref),
 UNIQUE(tenant_id,operation_id),
 FOREIGN KEY(tenant_id,operation_id) REFERENCES {schema}.service_operations(tenant_id,operation_id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,claim_id) REFERENCES {schema}.claims(tenant_id,id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE {schema}.claim_revision_items (
 tenant_id uuid NOT NULL,
 claim_id uuid NOT NULL,
 revision_no bigint NOT NULL,
 line_id uuid NOT NULL,
 trip_item_id uuid NOT NULL,
 trip_id uuid NOT NULL,
 owner_principal_ref uuid NOT NULL,
 amount_minor bigint NOT NULL CHECK(amount_minor>0),
 currency text NOT NULL CHECK(currency IN ('IDR','SGD','USD')),
 receipt_ids uuid[] NOT NULL CHECK(cardinality(receipt_ids)<=10),
 description text NOT NULL DEFAULT '' CHECK(octet_length(description)<=2048),
 PRIMARY KEY(tenant_id,claim_id,revision_no,line_id),
 UNIQUE(tenant_id,claim_id,revision_no,trip_item_id),
 FOREIGN KEY(tenant_id,claim_id,revision_no) REFERENCES {schema}.claim_revisions(tenant_id,claim_id,revision_no),
 FOREIGN KEY(tenant_id,claim_id,trip_id,owner_principal_ref) REFERENCES {schema}.claims(tenant_id,id,trip_id,owner_principal_ref),
 FOREIGN KEY(tenant_id,trip_item_id,trip_id,owner_principal_ref)
   REFERENCES {schema}.trip_expense_items(tenant_id,id,trip_id,owner_principal_ref)
);
CREATE TABLE {schema}.claim_receipts (
 tenant_id uuid NOT NULL,
 claim_id uuid NOT NULL,
 revision_no bigint NOT NULL,
 file_ref uuid NOT NULL,
 file_version bigint NOT NULL CHECK(file_version>0),
 file_digest text NOT NULL CHECK(file_digest ~ '^[a-f0-9]{{64}}$'),
 pin_operation_ref uuid NOT NULL,
 attestation_digest text NOT NULL CHECK(attestation_digest ~ '^[a-f0-9]{{64}}$'),
 PRIMARY KEY(tenant_id,claim_id,revision_no,file_ref),
 FOREIGN KEY(tenant_id,claim_id,revision_no) REFERENCES {schema}.claim_revisions(tenant_id,claim_id,revision_no)
);
CREATE TABLE {schema}.claim_stages (
 tenant_id uuid NOT NULL,
 claim_id uuid NOT NULL,
 revision_no bigint NOT NULL,
 stage_no integer NOT NULL CHECK(stage_no BETWEEN 1 AND 10),
 approver_principal_ref uuid NOT NULL,
 scope_ref text NOT NULL CHECK(scope_ref='reimbursement'),
 stage_state text NOT NULL CHECK(stage_state IN ('pending','approved','rejected','information_requested','void')),
 decided_at timestamptz,
 PRIMARY KEY(tenant_id,claim_id,revision_no,stage_no),
 FOREIGN KEY(tenant_id,claim_id,revision_no) REFERENCES {schema}.claim_revisions(tenant_id,claim_id,revision_no),
 CHECK((stage_state IN ('pending','void') AND decided_at IS NULL)
    OR (stage_state IN ('approved','rejected','information_requested') AND decided_at IS NOT NULL))
);
CREATE INDEX claim_stage_reviewer ON {schema}.claim_stages(tenant_id,approver_principal_ref,stage_state,claim_id);
CREATE TABLE {schema}.decisions (
 tenant_id uuid NOT NULL,
 claim_id uuid NOT NULL,
 revision_no bigint NOT NULL,
 stage_no integer NOT NULL,
 actor_ref uuid NOT NULL,
 decision text NOT NULL CHECK(decision IN ('approve','reject','request_information')),
 reason text NOT NULL CHECK(octet_length(reason)<=4096 AND (decision='approve' OR length(btrim(reason))>0)),
 operation_id uuid NOT NULL,
 human_action_ref uuid NOT NULL,
 payload_hash text NOT NULL CHECK(payload_hash ~ '^[a-f0-9]{{64}}$'),
 claim_version bigint NOT NULL CHECK(claim_version>1),
 created_at timestamptz NOT NULL,
 PRIMARY KEY(tenant_id,claim_id,revision_no,stage_no),
 UNIQUE(tenant_id,operation_id),
 UNIQUE(tenant_id,human_action_ref),
 FOREIGN KEY(tenant_id,operation_id) REFERENCES {schema}.service_operations(tenant_id,operation_id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,claim_id,revision_no,stage_no) REFERENCES {schema}.claim_stages(tenant_id,claim_id,revision_no,stage_no)
);
CREATE TABLE {schema}.expense_reservations (
 tenant_id uuid NOT NULL,
 trip_item_id uuid NOT NULL,
 claim_id uuid NOT NULL,
 reservation_state text NOT NULL CHECK(reservation_state IN ('held','approved')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,trip_item_id),
 FOREIGN KEY(tenant_id,trip_item_id) REFERENCES {schema}.trip_expense_items(tenant_id,id),
 FOREIGN KEY(tenant_id,claim_id) REFERENCES {schema}.claims(tenant_id,id)
);
CREATE INDEX claim_reservations_parent ON {schema}.expense_reservations(tenant_id,claim_id,trip_item_id);
ALTER TABLE {schema}.claims ADD FOREIGN KEY(tenant_id,id,current_revision_no)
 REFERENCES {schema}.claim_revisions(tenant_id,claim_id,revision_no) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE {schema}.claims ADD FOREIGN KEY(tenant_id,id,current_revision_no,active_stage_no)
 REFERENCES {schema}.claim_stages(tenant_id,claim_id,revision_no,stage_no) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE {schema}.claims ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.claims FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.claim_revisions ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.claim_revisions FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.claim_revision_items ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.claim_revision_items FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.claim_receipts ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.claim_receipts FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.claim_stages ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.claim_stages FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.decisions ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.decisions FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.expense_reservations ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.expense_reservations FORCE ROW LEVEL SECURITY;
CREATE POLICY maintenance ON {schema}.claims TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.claim_revisions TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.claim_revision_items TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.claim_receipts TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.claim_stages TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.decisions TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.expense_reservations TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.claims,{schema}.claim_revisions,{schema}.claim_revision_items,{schema}.claim_receipts,
 {schema}.claim_stages,{schema}.decisions,{schema}.expense_reservations FROM PUBLIC,{runtime};

CREATE FUNCTION {schema}._claim_immutable() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $fn$
BEGIN RAISE EXCEPTION 'Claim history is immutable' USING ERRCODE='23514';END $fn$;
CREATE TRIGGER revision_immutable BEFORE UPDATE OR DELETE ON {schema}.claim_revisions FOR EACH ROW EXECUTE FUNCTION {schema}._claim_immutable();
CREATE TRIGGER revision_items_immutable BEFORE UPDATE OR DELETE ON {schema}.claim_revision_items FOR EACH ROW EXECUTE FUNCTION {schema}._claim_immutable();
CREATE TRIGGER claim_receipts_immutable BEFORE UPDATE OR DELETE ON {schema}.claim_receipts FOR EACH ROW EXECUTE FUNCTION {schema}._claim_immutable();
CREATE TRIGGER decision_immutable BEFORE UPDATE OR DELETE ON {schema}.decisions FOR EACH ROW EXECUTE FUNCTION {schema}._claim_immutable();

CREATE FUNCTION {schema}._claim_header_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $fn$
BEGIN
 IF TG_OP='DELETE' OR OLD.state IN ('approved','rejected') THEN RAISE EXCEPTION 'Terminal claim cannot change' USING ERRCODE='23514';END IF;
 IF ROW(NEW.tenant_id,NEW.id,NEW.public_reference,NEW.owner_principal_ref,NEW.trip_id,NEW.policy_id,NEW.policy_version,NEW.policy_digest,NEW.submitted_at,NEW.previous_claim_id)
  IS DISTINCT FROM ROW(OLD.tenant_id,OLD.id,OLD.public_reference,OLD.owner_principal_ref,OLD.trip_id,OLD.policy_id,OLD.policy_version,OLD.policy_digest,OLD.submitted_at,OLD.previous_claim_id)
  OR NEW.version<>OLD.version+1 THEN RAISE EXCEPTION 'Locked claim material cannot change' USING ERRCODE='23514';END IF;
 IF NEW.current_revision_no=OLD.current_revision_no+1 THEN
  IF OLD.state<>'needs_information' OR NEW.state<>'pending_approval' OR NEW.active_stage_no<>1 THEN RAISE EXCEPTION 'Invalid information transition' USING ERRCODE='23514';END IF;
 ELSIF NEW.current_revision_no=OLD.current_revision_no AND OLD.state='pending_approval' THEN
  IF NEW.state='pending_approval' AND NEW.active_stage_no<>OLD.active_stage_no+1 THEN RAISE EXCEPTION 'Approval stage out of order' USING ERRCODE='23514';END IF;
  IF NEW.state='approved' AND EXISTS(SELECT 1 FROM {schema}.claim_stages WHERE tenant_id=OLD.tenant_id AND claim_id=OLD.id AND revision_no=OLD.current_revision_no AND stage_state<>'approved') THEN RAISE EXCEPTION 'Incomplete approvals' USING ERRCODE='23514';END IF;
 ELSE RAISE EXCEPTION 'Invalid claim transition' USING ERRCODE='23514';END IF;
 RETURN NEW;
END $fn$;
CREATE TRIGGER claim_header_guard BEFORE UPDATE OR DELETE ON {schema}.claims FOR EACH ROW EXECUTE FUNCTION {schema}._claim_header_guard();

CREATE FUNCTION {schema}._claim_stage_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $fn$
BEGIN
 IF TG_OP='DELETE' OR OLD.stage_state<>'pending' OR NEW.stage_state='pending'
  OR ROW(NEW.tenant_id,NEW.claim_id,NEW.revision_no,NEW.stage_no,NEW.approver_principal_ref,NEW.scope_ref)
   IS DISTINCT FROM ROW(OLD.tenant_id,OLD.claim_id,OLD.revision_no,OLD.stage_no,OLD.approver_principal_ref,OLD.scope_ref)
 THEN RAISE EXCEPTION 'Approval history cannot change' USING ERRCODE='23514';END IF;
 RETURN NEW;
END $fn$;
CREATE TRIGGER claim_stage_guard BEFORE UPDATE OR DELETE ON {schema}.claim_stages FOR EACH ROW EXECUTE FUNCTION {schema}._claim_stage_guard();
REVOKE ALL ON FUNCTION {schema}._claim_immutable(),{schema}._claim_header_guard(),{schema}._claim_stage_guard() FROM PUBLIC,{runtime};

-- Claim effects cannot commit without the matching committed REF-03 ledger and
-- audit in the SAME transaction. An absent/sealed/rejected terminal is invalid.
CREATE FUNCTION {schema}._claim_ledger_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $fn$
DECLARE op {schema}.service_operations%ROWTYPE;actor uuid;
BEGIN
 SELECT * INTO op FROM {schema}.service_operations WHERE tenant_id=NEW.tenant_id AND operation_id=NEW.operation_id;
 actor:=CASE WHEN TG_TABLE_NAME='decisions' THEN (to_jsonb(NEW)->>'actor_ref')::uuid ELSE (to_jsonb(NEW)->>'submitted_by_ref')::uuid END;
 IF op.operation_id IS NULL OR op.state<>'committed' OR op.actor_id<>actor
  OR op.request_hash<>NEW.payload_hash OR op.result->>'ref' IS DISTINCT FROM NEW.claim_id::text
  OR op.result->>'version' IS DISTINCT FROM NEW.claim_version::text
  OR NOT EXISTS(SELECT 1 FROM {schema}.service_audit WHERE tenant_id=NEW.tenant_id AND operation_id=NEW.operation_id AND outcome='committed' AND actor_id=actor)
 THEN RAISE EXCEPTION 'Claim effect requires committed operation and audit' USING ERRCODE='23514';END IF;
 IF TG_TABLE_NAME='decisions' THEN
  IF op.kind<>'claim.decide' OR op.object_kind<>'claim' OR op.object_id<>NEW.claim_id OR op.object_version<>NEW.claim_version-1 THEN RAISE EXCEPTION 'Decision operation mismatch' USING ERRCODE='23514';END IF;
 ELSIF NEW.revision_no=1 THEN
  IF op.kind<>'claim.submit' OR op.object_kind<>'draft' OR op.object_id<>NEW.source_draft_id OR op.object_version<>NEW.source_draft_version THEN RAISE EXCEPTION 'Submission operation mismatch' USING ERRCODE='23514';END IF;
 ELSE
  IF op.kind<>'claim.information' OR op.object_kind<>'claim' OR op.object_id<>NEW.claim_id OR op.object_version<>NEW.claim_version-1 THEN RAISE EXCEPTION 'Information operation mismatch' USING ERRCODE='23514';END IF;
 END IF;
 RETURN NULL;
END $fn$;
CREATE CONSTRAINT TRIGGER claim_revision_ledger AFTER INSERT ON {schema}.claim_revisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {schema}._claim_ledger_guard();
CREATE CONSTRAINT TRIGGER decision_ledger AFTER INSERT ON {schema}.decisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {schema}._claim_ledger_guard();
REVOKE ALL ON FUNCTION {schema}._claim_ledger_guard() FROM PUBLIC,{runtime};

CREATE FUNCTION {schema}.claim_call(operation text,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $fn$
#variable_conflict use_column
<<mutation>>
DECLARE t uuid:={schema}.context_tenant();a uuid:={schema}.context_actor();expiry numeric;
 c {schema}.claims%ROWTYPE;prior {schema}.claims%ROWTYPE;tr {schema}.trips%ROWTYPE;pkg {schema}.service_policy_packages%ROWTYPE;
 ident jsonb;body jsonb;data jsonb;permit jsonb;elig jsonb;entry jsonb;pin jsonb;category jsonb;source {schema}.trip_expense_items%ROWTYPE;
 claim_id uuid;trip_id uuid;rev bigint;expected bigint;received timestamptz;canonical text;hash text;ids uuid[];total numeric;idx integer;
 stage {schema}.claim_stages%ROWTYPE;outcome text;next_state text;next_stage integer;next_version bigint;draft_id uuid;draft_version bigint;result jsonb;
BEGIN
 IF NOT {schema}.runtime_ready() THEN RAISE EXCEPTION 'Service runtime rejected';END IF;
 IF t IS NULL OR a IS NULL OR NOT EXISTS(SELECT 1 FROM {schema}.tenant_registry WHERE tenant_id=t AND active)
  THEN RETURN jsonb_build_object('status','missing');END IF;
 IF payload IS NULL OR jsonb_typeof(payload)<>'object' OR octet_length(payload::text)>131072
  OR jsonb_typeof(payload->'expires_at') IS DISTINCT FROM 'number' THEN RETURN jsonb_build_object('status','invalid');END IF;
 expiry:=(payload->>'expires_at')::numeric;
 IF expiry<=extract(epoch FROM clock_timestamp()) OR expiry>extract(epoch FROM clock_timestamp())+65
  THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
 IF operation='facts' THEN
  SELECT * INTO tr FROM {schema}.trips WHERE tenant_id=t AND id=(payload->>'trip_id')::uuid AND owner_principal_ref=a;
  IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
  SELECT * INTO pkg FROM {schema}.service_policy_packages WHERE tenant_id=t AND package_id=(payload->'policy'->>'id')::uuid
   AND package_version=(payload->'policy'->>'version')::bigint AND digest=payload->'policy'->>'digest';
  IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
  RETURN jsonb_build_object('status','ok','previous_claim_id',(SELECT p.id FROM {schema}.claims p WHERE p.tenant_id=t AND p.owner_principal_ref=a AND p.trip_id=tr.id AND p.state='rejected'
   AND EXISTS(SELECT 1 FROM {schema}.claim_revision_items i WHERE i.tenant_id=t AND i.claim_id=p.id AND i.revision_no=p.current_revision_no
    AND i.trip_item_id::text IN (SELECT value FROM jsonb_array_elements_text(coalesce(payload->'item_ids','[]'::jsonb))))
   ORDER BY p.submitted_at DESC,p.id DESC LIMIT 1),'trip',to_jsonb(tr)||jsonb_build_object('items',
   (SELECT coalesce(jsonb_agg(to_jsonb(i) ORDER BY id),'[]'::jsonb) FROM {schema}.trip_expense_items i WHERE tenant_id=t AND trip_id=tr.id AND owner_principal_ref=a)),
   'policy',jsonb_build_object('tenant_id',t,'package_id',pkg.package_id,'package_version',pkg.package_version,'digest',pkg.digest,
    'valid_trip_end_from',pkg.valid_trip_end_from,'valid_trip_end_to',pkg.valid_trip_end_to,'timezone',pkg.timezone,'rules',pkg.rules_json));
 ELSIF operation IN ('read','relation') THEN
  SELECT * INTO c FROM {schema}.claims WHERE tenant_id=t AND id=(payload->>'claim_id')::uuid FOR SHARE;
  IF NOT FOUND OR (c.owner_principal_ref<>a AND NOT EXISTS(SELECT 1 FROM {schema}.claim_stages WHERE tenant_id=t AND claim_id=c.id
   AND revision_no=c.current_revision_no AND stage_no=c.active_stage_no AND approver_principal_ref=a AND stage_state='pending' AND c.state='pending_approval'))
   THEN RETURN jsonb_build_object('status','missing');END IF;
  SELECT * INTO tr FROM {schema}.trips WHERE tenant_id=t AND id=c.trip_id;
  result:=jsonb_build_object('tenant_id',t,'id',c.id,'owner_id',c.owner_principal_ref,'trip_id',c.trip_id,'trip_date',tr.end_date,
   'policy',jsonb_build_object('id',c.policy_id,'version',c.policy_version,'digest',c.policy_digest),'state',c.state,
   'version',c.version,'revision',c.current_revision_no,'active_stage',c.active_stage_no,
   'reviewer_id',(SELECT approver_principal_ref FROM {schema}.claim_stages WHERE tenant_id=t AND claim_id=c.id AND revision_no=c.current_revision_no AND stage_no=c.active_stage_no));
  IF operation='read' THEN
   result:=result||jsonb_build_object('public_reference',c.public_reference,'submitted_at',c.submitted_at,'previous_claim_id',c.previous_claim_id,
    'financial',(SELECT jsonb_build_object('total_minor',r.total_minor,'currency',r.currency) FROM {schema}.claim_revisions r WHERE tenant_id=t AND claim_id=c.id AND revision_no=c.current_revision_no),
    'items',(SELECT coalesce(jsonb_agg(jsonb_build_object('id',i.line_id,'trip_item_id',i.trip_item_id,'amount_minor',i.amount_minor,'receipt_ids',i.receipt_ids) ORDER BY i.line_id),'[]'::jsonb) FROM {schema}.claim_revision_items i WHERE tenant_id=t AND claim_id=c.id AND revision_no=c.current_revision_no),
    'receipts',(SELECT coalesce(jsonb_agg(jsonb_build_object('id',r.file_ref,'version',r.file_version,'digest',r.file_digest) ORDER BY r.file_ref),'[]'::jsonb) FROM {schema}.claim_receipts r WHERE tenant_id=t AND claim_id=c.id AND revision_no=c.current_revision_no),
    'stages',(SELECT coalesce(jsonb_agg(jsonb_build_object('stage',s.stage_no,'approver_id',s.approver_principal_ref,'scope_ref',s.scope_ref,'state',s.stage_state) ORDER BY s.stage_no),'[]'::jsonb) FROM {schema}.claim_stages s WHERE tenant_id=t AND claim_id=c.id AND revision_no=c.current_revision_no));
  END IF;
  IF expiry<=extract(epoch FROM clock_timestamp()) THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
  RETURN jsonb_build_object('status','ok','claim',result);
 END IF;
 IF operation NOT IN ('submit','information','decision') OR NOT payload ?& ARRAY['identity','canonical','permit','claim_id','received_at','previous_claim_id']
  OR (SELECT count(*) FROM jsonb_object_keys(payload))<>7 OR jsonb_typeof(payload->'canonical') IS DISTINCT FROM 'string'
  THEN RETURN jsonb_build_object('status','invalid');END IF;
 ident:=payload->'identity';canonical:=payload->>'canonical';permit:=payload->'permit';body:=canonical::jsonb;data:=body->'data';
 hash:=encode(sha256(convert_to('eai:human-action:v1:'||canonical,'UTF8')),'hex');
 IF jsonb_typeof(ident)<>'object' OR jsonb_typeof(permit)<>'object' OR jsonb_typeof(body)<>'object'
  OR NOT body ?& ARRAY['schema_version','action','tenant_id','actor_id','operation_id','object','data']
  OR (SELECT count(*) FROM jsonb_object_keys(body))<>7 OR body->'schema_version' IS DISTINCT FROM '1'::jsonb OR body->>'schema_version' IS DISTINCT FROM '1'
  OR NOT ident ?& ARRAY['tenant_id','actor_id','operation_id','caller','kind','key','request_hash','object_kind','object_id','object_version']
  OR NOT permit ?& ARRAY['request_hash','operation_id','expires_at','human_receipt_id','human_action_ref','eligibility','receipt_pins']
  OR octet_length(canonical)>65536 OR ident->>'tenant_id' IS DISTINCT FROM t::text OR ident->>'actor_id' IS DISTINCT FROM a::text
  OR ident->>'request_hash' IS DISTINCT FROM hash OR ident->>'caller' IS DISTINCT FROM 'platform-dispatcher'
  OR body->>'tenant_id' IS DISTINCT FROM t::text OR body->>'actor_id' IS DISTINCT FROM a::text
  OR body->>'operation_id' IS DISTINCT FROM ident->>'operation_id' OR body->>'action' IS DISTINCT FROM ident->>'kind'
  OR ident->>'object_kind' IS DISTINCT FROM body->'object'->>'kind' OR ident->>'object_id' IS DISTINCT FROM body->'object'->>'id'
  OR ident->>'object_version' IS DISTINCT FROM body->'object'->>'version'
  OR ident->>'kind' IS DISTINCT FROM (CASE operation WHEN 'submit' THEN 'claim.submit' WHEN 'information' THEN 'claim.information' ELSE 'claim.decide' END)
  OR permit->>'request_hash' IS DISTINCT FROM hash OR permit->>'operation_id' IS DISTINCT FROM ident->>'operation_id'
  OR jsonb_typeof(permit->'expires_at') IS DISTINCT FROM 'number' OR (permit->>'expires_at')::numeric<expiry
  THEN RETURN jsonb_build_object('status','invalid');END IF;
 claim_id:=(payload->>'claim_id')::uuid;expected:=(ident->>'object_version')::bigint;received:=(payload->>'received_at')::timestamptz;
 IF claim_id IS NULL OR claim_id='00000000-0000-0000-0000-000000000000'::uuid OR received IS NULL OR received>clock_timestamp() OR expected<1
  OR permit->>'human_receipt_id' IS NULL OR permit->>'human_action_ref' IS NULL THEN RETURN jsonb_build_object('status','invalid');END IF;
 PERFORM (permit->>'human_receipt_id')::uuid,(permit->>'human_action_ref')::uuid;
 elig:=permit->'eligibility';
 IF elig->'allow' IS DISTINCT FROM 'true'::jsonb OR elig->>'operation_id' IS DISTINCT FROM ident->>'operation_id' OR elig->>'request_hash' IS DISTINCT FROM hash
  OR elig->'blocked_reason' IS DISTINCT FROM 'null'::jsonb OR jsonb_typeof(elig->'checked_at') IS DISTINCT FROM 'number'
  OR jsonb_typeof(elig->'eligibility_revision') IS DISTINCT FROM 'number' OR elig->>'eligibility_revision' !~ '^[1-9][0-9]*$'
  OR (elig->>'checked_at')::numeric>extract(epoch FROM clock_timestamp())+5
  OR elig->>'operation_kind' IS DISTINCT FROM (CASE operation WHEN 'submit' THEN 'submission' WHEN 'information' THEN 'information' ELSE 'decision' END)
  OR jsonb_typeof(elig->'expires_at') IS DISTINCT FROM 'number' OR (elig->>'expires_at')::numeric<expiry THEN RETURN jsonb_build_object('status','forbidden');END IF;
 IF operation<>'submit' THEN
  SELECT * INTO c FROM {schema}.claims WHERE tenant_id=t AND id=claim_id FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
  IF ident->>'object_kind'<>'claim' OR ident->>'object_id'<>claim_id::text OR c.version<>expected OR c.current_revision_no<>(data->>'revision')::bigint
   THEN RETURN jsonb_build_object('status','conflict','code','stale_claim');END IF;
  IF operation='information' AND (c.state<>'needs_information' OR c.owner_principal_ref<>a) THEN RETURN jsonb_build_object('status','forbidden');END IF;
  IF operation='decision' AND c.state<>'pending_approval' THEN RETURN jsonb_build_object('status','conflict','code','terminal_claim');END IF;
  trip_id:=c.trip_id;
 ELSE
  IF ident->>'object_kind'<>'draft' OR EXISTS(SELECT 1 FROM {schema}.claims WHERE tenant_id=t AND id=claim_id) THEN RETURN jsonb_build_object('status','conflict','code','claim_exists');END IF;
  trip_id:=(data->>'trip_id')::uuid;
 END IF;
 SELECT * INTO tr FROM {schema}.trips WHERE tenant_id=t AND id=trip_id FOR SHARE;
 IF NOT FOUND OR tr.state<>'completed' OR (operation<>'decision' AND tr.owner_principal_ref<>a) THEN RETURN jsonb_build_object('status','missing');END IF;
 IF operation='submit' THEN
  SELECT * INTO pkg FROM {schema}.service_policy_packages WHERE tenant_id=t AND package_id=(data->'policy'->>'id')::uuid
   AND package_version=(data->'policy'->>'version')::bigint AND digest=data->'policy'->>'digest';
 ELSE SELECT * INTO pkg FROM {schema}.service_policy_packages WHERE tenant_id=t AND package_id=c.policy_id AND package_version=c.policy_version AND digest=c.policy_digest;END IF;
 IF pkg.package_id IS NULL OR elig->'policy' IS DISTINCT FROM jsonb_build_object('id',pkg.package_id,'version',pkg.package_version,'digest',pkg.digest)
  OR elig->>'trip_date' IS DISTINCT FROM tr.end_date::text OR (operation='submit' AND elig->'claim_ref' IS DISTINCT FROM 'null'::jsonb)
  OR (operation<>'submit' AND elig->>'claim_ref' IS DISTINCT FROM claim_id::text) THEN RETURN jsonb_build_object('status','forbidden');END IF;
 IF operation='decision' THEN
  SELECT * INTO stage FROM {schema}.claim_stages WHERE tenant_id=t AND claim_id=c.id AND revision_no=c.current_revision_no AND stage_no=c.active_stage_no FOR UPDATE;
  IF stage.stage_state<>'pending' OR stage.approver_principal_ref<>a OR c.owner_principal_ref=a THEN RETURN jsonb_build_object('status','forbidden');END IF;
  IF (data->>'stage')::integer<>c.active_stage_no THEN RETURN jsonb_build_object('status','conflict','code','stale_stage');END IF;
  IF NOT data ?& ARRAY['revision','stage','decision','reason'] OR (SELECT count(*) FROM jsonb_object_keys(data))<>4
   OR data->>'decision' NOT IN ('approve','reject','request_information') OR jsonb_typeof(data->'reason')<>'string' OR octet_length(data->>'reason')>4096
   OR (data->>'decision'<>'approve' AND length(btrim(data->>'reason'))=0) THEN RETURN jsonb_build_object('status','invalid');END IF;
  next_version:=c.version+1;
  IF data->>'decision'='approve' THEN
   SELECT min(stage_no) INTO next_stage FROM {schema}.claim_stages WHERE tenant_id=t AND claim_id=c.id AND revision_no=c.current_revision_no AND stage_no>c.active_stage_no;
   next_state:=CASE WHEN next_stage IS NULL THEN 'approved' ELSE 'pending_approval' END;
   outcome:=CASE WHEN next_stage IS NULL THEN 'claim_approved' ELSE 'stage_approved' END;
  ELSE next_stage:=NULL;next_state:=CASE WHEN data->>'decision'='reject' THEN 'rejected' ELSE 'needs_information' END;
   outcome:=CASE WHEN data->>'decision'='reject' THEN 'claim_rejected' ELSE 'information_requested' END;END IF;
  INSERT INTO {schema}.decisions VALUES(t,c.id,c.current_revision_no,c.active_stage_no,a,data->>'decision',data->>'reason',
   (ident->>'operation_id')::uuid,(permit->>'human_action_ref')::uuid,hash,next_version,clock_timestamp());
  UPDATE {schema}.claim_stages SET stage_state=CASE data->>'decision' WHEN 'approve' THEN 'approved' WHEN 'reject' THEN 'rejected' ELSE 'information_requested' END,
   decided_at=clock_timestamp() WHERE tenant_id=t AND claim_id=c.id AND revision_no=c.current_revision_no AND stage_no=c.active_stage_no;
  IF next_state='rejected' THEN
   UPDATE {schema}.claim_stages SET stage_state='void' WHERE tenant_id=t AND claim_id=c.id AND revision_no=c.current_revision_no AND stage_state='pending';
   DELETE FROM {schema}.expense_reservations WHERE tenant_id=t AND expense_reservations.claim_id=c.id;
  ELSIF next_state='approved' THEN UPDATE {schema}.expense_reservations SET reservation_state='approved' WHERE tenant_id=t AND expense_reservations.claim_id=c.id;END IF;
  UPDATE {schema}.claims SET state=next_state,version=next_version,active_stage_no=next_stage WHERE tenant_id=t AND id=c.id;
 ELSE
  IF (SELECT count(*) FROM jsonb_object_keys(data))<>(CASE WHEN operation='submit' THEN 8 ELSE 10 END)
   OR NOT data ?& ARRAY['trip_id','currency','items','total_minor','receipts','policy','approval_route','destination'] THEN RETURN jsonb_build_object('status','invalid');END IF;
  IF tr.end_date<pkg.valid_trip_end_from OR (pkg.valid_trip_end_to IS NOT NULL AND tr.end_date>=pkg.valid_trip_end_to)
   OR data->'policy' IS DISTINCT FROM jsonb_build_object('id',pkg.package_id,'version',pkg.package_version,'digest',pkg.digest)
   OR data->'approval_route' IS DISTINCT FROM pkg.rules_json->'approval_route' OR data->>'currency' IS DISTINCT FROM pkg.rules_json->>'currency'
   OR data->>'trip_id' IS DISTINCT FROM trip_id::text OR data->>'destination' IS DISTINCT FROM 'reference-reimbursement'
   THEN RETURN jsonb_build_object('status','invalid');END IF;
  IF operation='submit' AND received>=((tr.end_date+(pkg.rules_json->>'submission_deadline_days')::integer+1)::timestamp AT TIME ZONE pkg.timezone)
   THEN RETURN jsonb_build_object('status','conflict','code','deadline_passed');END IF;
  IF operation='submit' AND payload->'previous_claim_id' IS DISTINCT FROM 'null'::jsonb THEN
   SELECT * INTO prior FROM {schema}.claims WHERE tenant_id=t AND id=(payload->>'previous_claim_id')::uuid AND owner_principal_ref=a AND claims.trip_id=mutation.trip_id FOR SHARE;
   IF NOT FOUND OR prior.state<>'rejected' THEN RETURN jsonb_build_object('status','conflict','code','invalid_previous_claim');END IF;
  END IF;
  IF jsonb_typeof(data->'items') IS DISTINCT FROM 'array' OR jsonb_array_length(data->'items') NOT BETWEEN 1 AND 100
   OR jsonb_typeof(data->'receipts') IS DISTINCT FROM 'array' OR jsonb_array_length(data->'receipts')>10
   OR jsonb_typeof(permit->'receipt_pins') IS DISTINCT FROM 'array' OR jsonb_array_length(permit->'receipt_pins')<>jsonb_array_length(data->'receipts')
   OR (SELECT count(DISTINCT v->>'id') FROM jsonb_array_elements(data->'items') v)<>jsonb_array_length(data->'items')
   OR (SELECT count(DISTINCT v->>'trip_item_id') FROM jsonb_array_elements(data->'items') v)<>jsonb_array_length(data->'items')
   OR (SELECT count(DISTINCT v->>'id') FROM jsonb_array_elements(data->'receipts') v)<>jsonb_array_length(data->'receipts')
   THEN RETURN jsonb_build_object('status','invalid');END IF;
  -- Lock authoritative items in the same order, including old items that an
  -- information revision may release. Different operation keys share this lock.
  ids:=ARRAY(SELECT (v->>'trip_item_id')::uuid FROM jsonb_array_elements(data->'items') v);
  FOR source IN SELECT * FROM {schema}.trip_expense_items i WHERE i.tenant_id=t AND i.id IN
   (SELECT unnest(ids) UNION SELECT trip_item_id FROM {schema}.claim_revision_items WHERE tenant_id=t AND claim_id=c.id AND revision_no=c.current_revision_no)
   ORDER BY i.id FOR UPDATE LOOP NULL;END LOOP;
  IF EXISTS(SELECT 1 FROM {schema}.expense_reservations r WHERE r.tenant_id=t AND r.trip_item_id=ANY(ids) AND r.claim_id<>mutation.claim_id)
   THEN RETURN jsonb_build_object('status','conflict','code','business_duplicate');END IF;
  total:=0;
  FOR entry IN SELECT * FROM jsonb_array_elements(data->'items') LOOP
   IF jsonb_typeof(entry->'amount_minor') IS DISTINCT FROM 'number' OR entry->>'amount_minor' !~ '^[1-9][0-9]*$'
    OR (entry->>'amount_minor')::numeric>9223372036854775807 OR jsonb_typeof(entry->'receipt_ids') IS DISTINCT FROM 'array' OR jsonb_array_length(entry->'receipt_ids')>10
    THEN RETURN jsonb_build_object('status','invalid');END IF;
   SELECT * INTO source FROM {schema}.trip_expense_items i WHERE i.tenant_id=t AND i.id=(entry->>'trip_item_id')::uuid AND i.trip_id=mutation.trip_id AND i.owner_principal_ref=a;
   IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
   SELECT value INTO category FROM jsonb_array_elements(pkg.rules_json->'categories') WHERE value->>'code'=source.category;
   IF category IS NULL OR source.currency<>data->>'currency' OR (category->'receipt_required'='true'::jsonb AND jsonb_array_length(entry->'receipt_ids')=0)
    OR (category->'max_amount_minor'<>'null'::jsonb AND (entry->>'amount_minor')::numeric>(category->>'max_amount_minor')::numeric)
    THEN RETURN jsonb_build_object('status','invalid');END IF;
   IF (SELECT count(DISTINCT value) FROM jsonb_array_elements_text(entry->'receipt_ids'))<>jsonb_array_length(entry->'receipt_ids')
    OR EXISTS(SELECT 1 FROM jsonb_array_elements_text(entry->'receipt_ids') r WHERE NOT EXISTS(SELECT 1 FROM jsonb_array_elements(data->'receipts') f WHERE f->>'id'=r.value))
    THEN RETURN jsonb_build_object('status','invalid');END IF;
   total:=total+(entry->>'amount_minor')::numeric;
  END LOOP;
  IF jsonb_typeof(data->'total_minor') IS DISTINCT FROM 'number' OR data->>'total_minor' !~ '^[1-9][0-9]*$' OR total<>(data->>'total_minor')::numeric OR total>9223372036854775807
   OR (pkg.rules_json->'max_total_minor'<>'null'::jsonb AND total>(pkg.rules_json->>'max_total_minor')::numeric)
   THEN RETURN jsonb_build_object('status','invalid');END IF;
  FOR entry IN SELECT * FROM jsonb_array_elements(data->'receipts') LOOP
   SELECT value INTO pin FROM jsonb_array_elements(permit->'receipt_pins') WHERE value->>'id'=entry->>'id' AND value->'version'=entry->'version' AND value->>'digest'=entry->>'digest';
   IF pin IS NULL OR pin->>'attestation_digest' !~ '^[a-f0-9]{{64}}$' OR pin->>'pin_operation_ref' IS NULL
    OR pin->>'operation_id' IS DISTINCT FROM ident->>'operation_id' OR pin->>'request_hash' IS DISTINCT FROM hash
    OR jsonb_typeof(pin->'expires_at') IS DISTINCT FROM 'number' OR (pin->>'expires_at')::numeric<expiry
    OR NOT EXISTS(SELECT 1 FROM jsonb_array_elements(data->'items') i,jsonb_array_elements_text(i->'receipt_ids') r WHERE r.value=entry->>'id')
    THEN RETURN jsonb_build_object('status','invalid');END IF;
   PERFORM (pin->>'pin_operation_ref')::uuid;
  END LOOP;
  IF EXISTS(SELECT 1 FROM jsonb_array_elements(pkg.rules_json->'approval_route') r WHERE r->>'approver_id'=a::text) THEN RETURN jsonb_build_object('status','forbidden');END IF;
  IF operation='submit' THEN
   rev:=1;next_version:=1;draft_id:=(ident->>'object_id')::uuid;draft_version:=expected;
   INSERT INTO {schema}.claims VALUES(t,claim_id,'CLM-'||upper(replace(claim_id::text,'-','')),a,trip_id,pkg.package_id,pkg.package_version,pkg.digest,'pending_approval',1,1,1,received,(payload->>'previous_claim_id')::uuid);
  ELSE
   IF (data->>'revision')::bigint<>c.current_revision_no OR jsonb_typeof(data->'draft') IS DISTINCT FROM 'object'
    OR (data->'draft'->>'version')::bigint<1 THEN RETURN jsonb_build_object('status','conflict','code','stale_draft');END IF;
   rev:=c.current_revision_no+1;next_version:=c.version+1;draft_id:=(data->'draft'->>'id')::uuid;draft_version:=(data->'draft'->>'version')::bigint;
   UPDATE {schema}.claim_stages SET stage_state='void' WHERE tenant_id=t AND claim_stages.claim_id=c.id AND revision_no=c.current_revision_no AND stage_state='pending';
  END IF;
  INSERT INTO {schema}.claim_revisions VALUES(t,claim_id,rev,hash,(ident->>'operation_id')::uuid,next_version,total::bigint,data->>'currency',
   (permit->>'human_receipt_id')::uuid,(permit->>'human_action_ref')::uuid,a,draft_id,draft_version,received);
  FOR entry IN SELECT * FROM jsonb_array_elements(data->'items') LOOP
   INSERT INTO {schema}.claim_revision_items VALUES(t,claim_id,rev,(entry->>'id')::uuid,(entry->>'trip_item_id')::uuid,trip_id,a,(entry->>'amount_minor')::bigint,data->>'currency',ARRAY(SELECT value::uuid FROM jsonb_array_elements_text(entry->'receipt_ids')),'');
  END LOOP;
  FOR pin IN SELECT * FROM jsonb_array_elements(permit->'receipt_pins') LOOP
   INSERT INTO {schema}.claim_receipts VALUES(t,claim_id,rev,(pin->>'id')::uuid,(pin->>'version')::bigint,pin->>'digest',(pin->>'pin_operation_ref')::uuid,pin->>'attestation_digest');
  END LOOP;
  FOR entry IN SELECT * FROM jsonb_array_elements(pkg.rules_json->'approval_route') LOOP
   INSERT INTO {schema}.claim_stages VALUES(t,claim_id,rev,(entry->>'stage')::integer,(entry->>'approver_id')::uuid,'reimbursement','pending',NULL);
  END LOOP;
  IF operation='information' THEN
   DELETE FROM {schema}.expense_reservations r WHERE r.tenant_id=t AND r.claim_id=c.id AND NOT r.trip_item_id=ANY(ids);
   UPDATE {schema}.claims SET state='pending_approval',version=next_version,current_revision_no=rev,active_stage_no=1 WHERE tenant_id=t AND id=c.id;
  END IF;
  INSERT INTO {schema}.expense_reservations(tenant_id,trip_item_id,claim_id,reservation_state) SELECT t,id,claim_id,'held' FROM unnest(ids) id ON CONFLICT DO NOTHING;
  outcome:=CASE WHEN operation='submit' THEN 'claim_submitted' ELSE 'information_received' END;
 END IF;
 IF expiry<=extract(epoch FROM clock_timestamp()) THEN RAISE EXCEPTION 'Claim authority expired' USING ERRCODE='40001';END IF;
 RETURN jsonb_build_object('status','ok','result',jsonb_build_object('ref',claim_id,'version',next_version,'code',outcome));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR datetime_field_overflow OR invalid_parameter_value THEN
 RETURN jsonb_build_object('status','invalid');
 WHEN unique_violation THEN RETURN jsonb_build_object('status','conflict','code','business_duplicate');
END $fn$;
REVOKE ALL ON FUNCTION {schema}.claim_call(text,jsonb) FROM PUBLIC,{runtime};
GRANT EXECUTE ON FUNCTION {schema}.claim_call(text,jsonb) TO {runtime};

-- Exact service4 runtime privilege/object manifest.
CREATE OR REPLACE FUNCTION {schema}.runtime_ready() RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $fn$
DECLARE r record; n oid; c record; p record; privilege text;
BEGIN
    SELECT roles.* INTO r FROM pg_catalog.pg_roles roles
      JOIN {schema}.runtime_binding b ON b.role_oid=roles.oid AND b.role_name=roles.rolname
      WHERE roles.rolname=SESSION_USER;
    IF NOT FOUND OR NOT r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreatedb
        OR r.rolcreaterole OR r.rolreplication OR r.rolinherit THEN RETURN false; END IF;
    IF EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members WHERE member=r.oid) THEN RETURN false; END IF;
    SELECT oid INTO n FROM pg_catalog.pg_namespace WHERE nspname={schema_name};
    IF NOT has_schema_privilege(r.oid,n,'USAGE') OR has_schema_privilege(r.oid,n,'CREATE')
      OR EXISTS(SELECT 1 FROM pg_catalog.pg_namespace WHERE nspowner=r.oid)
      OR EXISTS(SELECT 1 FROM pg_catalog.pg_class WHERE relowner=r.oid)
      OR EXISTS(SELECT 1 FROM pg_catalog.pg_proc WHERE proowner=r.oid) THEN RETURN false; END IF;
    IF (SELECT count(*) FROM {schema}.schema_migrations)<>4 OR NOT EXISTS(
        SELECT 1 FROM {schema}.schema_migrations WHERE version=1 AND name='0001_foundation.sql'
    ) OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=2 AND name='0002_operations.sql'
    ) OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=3 AND name='0003_reference_data.sql'
    ) OR NOT EXISTS(SELECT 1 FROM {schema}.schema_migrations WHERE version=4 AND name='0004_claims.sql') THEN RETURN false; END IF;
    FOR c IN SELECT * FROM pg_catalog.pg_class WHERE relnamespace=n AND relkind IN ('r','p','v','m','S','f') LOOP
        IF c.relkind<>'r' OR c.relname NOT IN ('schema_migrations','runtime_binding','tenant_registry','service_operations','service_audit','trips','trip_expense_items','service_policy_packages','package_receipts','claims','claim_revisions','claim_revision_items','claim_receipts','claim_stages','decisions','expense_reservations')
          OR NOT c.relrowsecurity OR NOT c.relforcerowsecurity THEN RETURN false; END IF;
        IF has_table_privilege(r.oid,c.oid,'SELECT')<>(c.relname='tenant_registry') THEN RETURN false; END IF;
        FOREACH privilege IN ARRAY ARRAY['INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER','SELECT WITH GRANT OPTION'] LOOP
            IF has_table_privilege(r.oid,c.oid,privilege) THEN RETURN false; END IF;
        END LOOP;
        FOREACH privilege IN ARRAY ARRAY['INSERT','UPDATE','REFERENCES','SELECT WITH GRANT OPTION'] LOOP
            IF has_any_column_privilege(r.oid,c.oid,privilege) THEN RETURN false; END IF;
        END LOOP;
        IF c.relname<>'tenant_registry' AND has_any_column_privilege(r.oid,c.oid,'SELECT') THEN RETURN false; END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_catalog.pg_class WHERE relnamespace=n AND relkind='r')<>16 THEN RETURN false; END IF;
    FOR p IN SELECT * FROM pg_catalog.pg_proc WHERE pronamespace=n LOOP
        IF p.proname IN ('context_tenant','context_actor','runtime_ready','operation_call','trip_call','package_call','claim_call') THEN
            IF p.pronargs<>(CASE WHEN p.proname IN ('operation_call','trip_call','package_call','claim_call') THEN 2 ELSE 0 END)
              OR NOT has_function_privilege(r.oid,p.oid,'EXECUTE') OR has_function_privilege(r.oid,p.oid,'EXECUTE WITH GRANT OPTION') THEN RETURN false;END IF;
        ELSIF p.proname IN ('_claim_immutable','_claim_header_guard','_claim_stage_guard','_claim_ledger_guard') THEN
            IF p.pronargs<>0 OR has_function_privilege(r.oid,p.oid,'EXECUTE') THEN RETURN false;END IF;
        ELSE RETURN false;END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_catalog.pg_proc WHERE pronamespace=n)<>11 THEN RETURN false;END IF;
    RETURN true;
END $fn$;


