"""Tool-owned bridge around application model ports. Never creates a client.

Actual provider execution is still disabled in the evaluation runner. This
bridge is tested with controlled SDKs before a future reviewed executor uses it.
"""
import math
import threading
import time
from decimal import Decimal
from types import SimpleNamespace

from enterprise_ai_platform.evaluation.journal import BudgetExceeded,CallGuard
from enterprise_ai_platform.evaluation.rag import EvaluationError
from enterprise_ai_platform.modules.models.domain import MAX_INPUT_TOKENS,MAX_OUTPUT_TOKENS,Usage


class BudgetedModels:
    """Retains application authority observer AND reserves evaluation exposure.

    delegate_factory receives our before-call observer; existing Nova/Cohere
    adapters invoke it immediately before SDK egress. The factory must not make
    calls on creation. It owns a single request-scoped client lifecycle.
    """
    def __init__(self,journal,case_id,run,rates,authority_observer,delegate_factory,*,max_calls=8,clock=time.time):
        if type(max_calls) is not int or not 1<=max_calls<=16:raise EvaluationError('evaluation_call_limit')
        self.journal,self.case_id,self.run=journal,case_id,run
        self.guard=CallGuard(journal,case_id,run,rates)
        self.rates=dict(rates)
        if set(self.rates)!={'chat_input','chat_output','embedding_input','embedding_output'} or any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in self.rates.values()):
            raise EvaluationError('evaluation_price_snapshot')
        self.authority=authority_observer;self.clock=clock;self.max_calls=max_calls
        self.lock=threading.Lock();self.active=None;self.attempt=None;self.egress_permitted=False
        self.delegate=delegate_factory(self.before_call)
        self.chat=SimpleNamespace(chat=self.chat_call,last_usage=None)
        self.embedding=SimpleNamespace(embed=self.embed_call,last_usage=None)

    def before_call(self,kind,tokens):
        if self.journal.stop_requested():raise EvaluationError('evaluation_phase_stopped')
        expected='chat' if self.active=='chat' else 'embedding'
        if self.active is None or kind!=expected or self.attempt is not None:
            raise EvaluationError('unexpected_evaluation_egress')
        bound=MAX_INPUT_TOKENS if kind=='chat' else 2048
        if type(tokens) is not int or not 1<=tokens<=bound:raise EvaluationError('evaluation_token_reservation')
        if len(self.journal.attempts(self.case_id,self.run))>=self.max_calls:
            raise EvaluationError('evaluation_call_limit')
        deadline=self.authority(kind,tokens)
        if type(deadline) not in (int,float) or not math.isfinite(deadline) or self.clock()>=deadline:
            raise EvaluationError('evaluation_authority_expired')
        try:self.attempt=self.guard.reserve(self.active,tokens,MAX_OUTPUT_TOKENS if kind=='chat' else 0)
        except BudgetExceeded as error:
            self.journal.request_stop(str(error));raise
        except Exception:
            self.journal.request_stop('ledger_failure');raise
        # Durable reservation may take time. Do not grant egress past authority.
        if self.clock()>=deadline:raise EvaluationError('evaluation_authority_expired')
        self.egress_permitted=True
        return deadline

    def invoke(self,kind,delegate,function):
        if not self.lock.acquire(blocking=False):raise EvaluationError('evaluation_models_must_be_serial')
        self.active=kind;self.attempt=None;self.egress_permitted=False;outcome='error'
        target=self.chat if kind=='chat' else self.embedding;target.last_usage=None
        try:
            value=function()
            if self.attempt is None or not self.egress_permitted:raise EvaluationError('evaluation_observer_bypassed')
            outcome='ok';return value
        except BaseException as error:
            if getattr(error,'code',None)=='timeout':outcome='timeout'
            raise
        finally:
            try:
                if self.attempt is not None:
                    usage=delegate.last_usage
                    if isinstance(usage,Usage) and type(usage.input_tokens) is int and type(usage.output_tokens) is int and usage.input_tokens>=0 and usage.output_tokens>=0:
                        # Outer application finally must retain trusted usage even
                        # when the financial ledger subsequently cannot settle.
                        target.last_usage=usage
                        prefix='chat' if kind=='chat' else 'embedding'
                        amount=float((Decimal(str(self.rates[prefix+'_input']))*usage.input_tokens+Decimal(str(self.rates[prefix+'_output']))*usage.output_tokens)/1000000)
                        self.guard.settle(self.attempt,cost_usd=amount,input_tokens=usage.input_tokens,output_tokens=usage.output_tokens,outcome=outcome)
                    else:
                        self.guard.settle(self.attempt,cost_usd=None if self.egress_permitted else 0.0,outcome=outcome)
            except Exception:
                self.journal.request_stop('ledger_failure');raise
            finally:
                self.active=None;self.attempt=None;self.egress_permitted=False;self.lock.release()

    def chat_call(self,messages,*,tools=False):
        return self.invoke('chat',self.delegate.chat,lambda:self.delegate.chat.chat(messages,tools=tools))

    def embed_call(self,texts,*,input_type):
        kind={'search_query':'query_embedding','search_document':'document_embedding'}.get(input_type)
        if kind is None:
            def reject():raise EvaluationError('evaluation_embedding_input_type')
            return self.invoke('invalid_embedding',self.delegate.embedding,reject)
        return self.invoke(kind,self.delegate.embedding,lambda:self.delegate.embedding.embed(texts,input_type=input_type))
