"""Explicit calibration capability issued by trusted operator composition only.

Not inferred from manifest approval strings or global provider-enable flags.
The CLI must verify the human scope, frozen source and readiness before issue.
"""
from dataclasses import dataclass,field,replace
import math,re,time
from enterprise_ai_platform.evaluation.rag import EvaluationError,configuration_hash
_SEAL=object()
@dataclass
class _Chain:
    configuration_sha256:str
    ledger_path:str|None=None
@dataclass(frozen=True)
class ProviderExecution:
    configuration_sha256:str
    scope_sha256:str
    gold_dataset_sha256:str
    expires_at:float
    verify_sources:object=field(repr=False)
    _seal:object=field(repr=False)
    _chain:object=field(repr=False)
    def __post_init__(self):
        if self._seal is not _SEAL:raise EvaluationError('provider_capability_not_issued')
    def allows(self,manifest,*,egress=True):
        try:
            if self._seal is not _SEAL or self._chain.configuration_sha256!=self.configuration_sha256 or configuration_hash(manifest)!=self.configuration_sha256 or manifest.dataset_sha256!=self.gold_dataset_sha256:return False
            if egress and time.time()>=self.expires_at:return False
            self.verify_sources();return True
        except Exception:return False
    def require(self,manifest):
        if not self.allows(manifest):raise EvaluationError('provider_capability_rejected')
    def bind_journal(self,journal):
        if journal.fingerprint!=self.configuration_sha256 or self._chain.configuration_sha256!=self.configuration_sha256:raise EvaluationError('provider_journal_binding')
        path=str(journal.path.resolve())
        if self._chain.ledger_path is None:self._chain.ledger_path=path
        if self._chain.ledger_path!=path or journal.stop_requested():raise EvaluationError('provider_ledger_retired_or_changed')
    def handoff(self,before,after,corpus_journal,case_journal):
        self.require(before);self.bind_journal(corpus_journal)
        a=before.model_dump(mode='json');b=after.model_dump(mode='json')
        for key in ('preprocessing_attempts','corpus_embedding_provenance_sha256'):a.pop(key);b.pop(key)
        if a!=b or corpus_journal.fingerprint!=configuration_hash(before) or case_journal.fingerprint!=configuration_hash(after):raise EvaluationError('provider_handoff_changed_scope')
        attempts=after.preprocessing_attempts
        with corpus_journal.connect() as db:
            groups=db.execute('SELECT DISTINCT case_id,run FROM attempts').fetchall()
        original=[a for case,run in groups for a in corpus_journal.attempts(case,run)];count=len(original)
        if sorted(a.model_dump_json() for a in original)!=sorted(a.model_dump_json() for a in attempts):raise EvaluationError('provider_handoff_attempt_changed')
        if len(attempts)!=count or len({x.attempt_id for x in attempts})!=count or any(x.cost_usd is None for x in attempts) or corpus_journal.stop_requested() or case_journal.stop_requested():raise EvaluationError('provider_handoff_accounting')
        if not re.fullmatch('[a-f0-9]{64}',after.corpus_embedding_provenance_sha256 or ''):raise EvaluationError('provider_handoff_provenance')
        copied=case_journal.attempts('__preprocessing__',0)
        with case_journal.connect() as db:copied_count=db.execute('SELECT count(*) FROM attempts').fetchone()[0]
        if copied_count!=count or sorted(a.model_dump_json() for a in copied)!=sorted(a.model_dump_json() for a in original):raise EvaluationError('provider_handoff_copy_mismatch')
        corpus_journal.request_stop('corpus_transferred')
        final_hash=configuration_hash(after)
        self._chain.configuration_sha256=final_hash;self._chain.ledger_path=str(case_journal.path.resolve())
        return replace(self,configuration_sha256=final_hash)

def issue_provider_execution(manifest,*,scope_sha256,gold_dataset_sha256,expires_at,verify_sources):
    # Trusted composition hook, not an API accepting arbitrary user JSON.
    if (manifest.mode,manifest.split,manifest.model_id,manifest.embedding_model_id,manifest.document_embedding_model_id,manifest.region,manifest.data_scope)!=(
        'provider','calibration','apac.amazon.nova-pro-v1:0','cohere.embed-multilingual-v3','cohere.embed-multilingual-v3','ap-southeast-1','fictional-only APAC exception'):
        raise EvaluationError('provider_scope')
    if not manifest.provider_egress_approved or not manifest.approved_by or not manifest.budget_approved_by or not manifest.representative_embeddings or not manifest.frozen_at or not manifest.price_snapshot_ref:raise EvaluationError('provider_review_missing')
    if manifest.budget_limit_usd is None or not 0<manifest.budget_limit_usd<=1 or manifest.max_inference_attempts is None or not 1<=manifest.max_inference_attempts<=148 or manifest.case_timeout_ms>120000 or manifest.phase_timeout_ms>900000:raise EvaluationError('provider_limits')
    if type(expires_at) not in (int,float) or not math.isfinite(expires_at) or not time.time()<expires_at<=time.time()+900:raise EvaluationError('provider_expiry')
    if not re.fullmatch('[a-f0-9]{64}',manifest.dataset_content_sha256 or '') or not re.fullmatch('[a-f0-9]{64}',scope_sha256) or gold_dataset_sha256!=manifest.dataset_sha256 or not callable(verify_sources):raise EvaluationError('provider_freeze')
    verify_sources()
    return ProviderExecution(configuration_hash(manifest),scope_sha256,gold_dataset_sha256,expires_at,verify_sources,_SEAL,_Chain(configuration_hash(manifest)))

def permitted(executor,manifest,*,egress=True):
    return type(executor) is ProviderExecution and executor.allows(manifest,egress=egress)
