"""Native workflow planner bridge reusing the reviewed reserve/settle ledger."""
from types import SimpleNamespace
from scripts.evaluation.model_port import BudgetedModels
class BudgetedWorkflowPlanner:
    def __init__(self,journal,case_id,run,rates,authority_observer,delegate_factory):
        self.bridge=BudgetedModels(journal,case_id,run,rates,authority_observer,lambda before:SimpleNamespace(chat=delegate_factory(before),embedding=None),max_calls=1)
    @property
    def last_usage(self):return self.bridge.chat.last_usage
    def propose(self,question,trip,selected):
        delegate=self.bridge.delegate.chat
        return self.bridge.invoke('chat',delegate,lambda:delegate.propose(question,trip,selected))
