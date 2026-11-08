"""Trusted provider runner support: exclusive operator lock and gated SDK ports."""
import os,json,hashlib,time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from enterprise_ai_platform.evaluation.provider import issue_provider_execution
from enterprise_ai_platform.evaluation.rag import EvaluationError
from enterprise_ai_platform.integrations.models.nova import NovaChat,CohereEmbedding
from scripts.evaluation.model_port import BudgetedModels

@contextmanager
def run_lock(folder):
    path=Path(folder)/'worker.lock';path.parent.mkdir(parents=True,exist_ok=True)
    f=path.open('a+b');f.seek(0);f.write(b'0');f.flush();f.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except (OSError,BlockingIOError):f.close();raise EvaluationError('provider_worker_still_active') from None
    try:yield
    finally:
        f.seek(0)
        if os.name=='nt':msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
        else:fcntl.flock(f.fileno(),fcntl.LOCK_UN)
        f.close()

class ProviderModels:
    """One capability shared by corpus and case egress, with no fallback."""
    def __init__(self,manifest,permit,client,rates):self.manifest,self.permit,self.client,self.rates=manifest,permit,client,rates
    def build(self,journal,case,run,authority):
        self.permit.bind_journal(journal)
        def checked_authority(kind,tokens):
            deadline=authority(kind,tokens)
            try:self.permit.require(self.manifest)
            except Exception:
                journal.request_stop('provider_scope_rejected');raise
            if journal.fingerprint!=self.permit.configuration_sha256:raise EvaluationError('provider_journal_binding')
            return min(deadline,self.permit.expires_at)
        return BudgetedModels(journal,case,run,self.rates,checked_authority,lambda before:SimpleNamespace(chat=NovaChat(self.client,before),embedding=CohereEmbedding(self.client,before)))
    def handoff(self,manifest,corpus_journal,case_journal):
        self.permit=self.permit.handoff(self.manifest,manifest,corpus_journal,case_journal);self.manifest=manifest
