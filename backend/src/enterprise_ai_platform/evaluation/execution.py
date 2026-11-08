"""Gold-free execution requests and source-bound measurement records."""
import hashlib
from datetime import datetime
from typing import Literal
from pydantic import Field,model_validator
from enterprise_ai_platform.evaluation.rag import Case,EvaluationError,Output,Record


class ExecutionRequest(Record):
    case_id:str
    tenant:Literal['A','B']
    principal_id:str
    membership_id:str
    access_revision:int=Field(ge=1)
    language:Literal['id','en']
    evaluated_at:str
    questions:list[str]=Field(min_length=1,max_length=3)

    @model_validator(mode='after')
    def validate_limits(self):
        if datetime.fromisoformat(self.evaluated_at).tzinfo is None:
            raise EvaluationError('execution_clock_requires_timezone')
        query=' '.join(self.questions)
        if any(not q.strip() for q in self.questions) or len(query)>200 or len(query.encode())>400:
            raise EvaluationError('execution_question_budget')
        return self

    @classmethod
    def from_case(cls,case:Case):
        # Explicit allowlist; model_dump(case) must never become runtime input.
        return cls(case_id=case.id,tenant=case.tenant,principal_id=case.actor.principal_id,
            membership_id=case.actor.membership_id,access_revision=case.actor.access_revision,
            language=case.language,evaluated_at=case.evaluated_at,
            questions=[t.question for t in case.turns])


class SourceBinding(Record):
    unit_id:str
    version_id:str
    version_no:int=Field(ge=1)
    page:int=Field(ge=1)
    chunk_no:int=Field(ge=0)
    access_revision:int=Field(ge=1)
    index_generation:int=Field(ge=1)
    text_digest:str=Field(pattern='^[a-f0-9]{64}$')


class ActorBinding(Record):
    """Frozen translation from authored fixture to actual initialized membership.

    Role assignment bumps the application's revision. Preserve that actual
    revision and reject subsequent drift; never reset DB revisions to fit gold.
    """
    tenant:Literal['A','B']
    tenant_id:str
    principal_id:str
    membership_id:str
    authored_access_revision:int=Field(ge=1)
    runtime_access_revision:int=Field(ge=1)

    def verify(self,request:ExecutionRequest,tenant_id:str,current_revision:int):
        if (request.tenant,request.principal_id,request.membership_id,request.access_revision)!=(self.tenant,self.principal_id,self.membership_id,self.authored_access_revision):
            raise EvaluationError('authored_actor_binding_changed')
        if tenant_id!=self.tenant_id or current_revision!=self.runtime_access_revision:
            raise EvaluationError('runtime_actor_binding_changed')


class SourceCatalog:
    """Maps actual prepared source identities, never expected answers/citations.

    Bindings are recorded after ingestion and current-source checks, with unit
    IDs from the frozen corpus. Current authority is still enforced by the app.
    """
    def __init__(self,bindings:list[SourceBinding]):
        self.bindings={self.key(b.model_dump()):b for b in bindings}
        if len(self.bindings)!=len(bindings) or len({b.unit_id for b in bindings})!=len(bindings):
            raise EvaluationError('duplicate_runtime_source_binding')

    @staticmethod
    def key(value):return tuple(value.get(k) for k in ('version_id','version_no','page','chunk_no'))

    def measure(self,ctx,buffer):
        hits=ctx.hits if ctx is not None else ()
        units=[];known={}
        for hit in hits:
            binding=self.bindings.get(self.key(hit))
            if binding is None:raise EvaluationError('unbound_runtime_context')
            if any(hit.get(k)!=getattr(binding,k) for k in ('access_revision','index_generation','text_digest')):
                raise EvaluationError('runtime_source_revision_changed')
            text=hit.get('text')
            if not isinstance(text,str) or hashlib.sha256(text.encode()).hexdigest()!=binding.text_digest:
                raise EvaluationError('runtime_context_digest')
            units.append(binding.unit_id);known[self.key(hit)]=binding
        citations=[]
        for citation in buffer.get('citations',[]):
            binding=known.get(self.key(citation))
            if binding is None or citation.get('text_digest')!=binding.text_digest:
                raise EvaluationError('runtime_citation_not_in_context')
            citations.append(binding.unit_id)
        return Output(kind=buffer['kind'],text=buffer['text'],final_retrieved_units=units,cited_units=citations)
