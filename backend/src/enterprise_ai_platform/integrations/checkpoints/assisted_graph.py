"""Real persistent interrupt; UI ACT is the only source of business consent."""
from typing import TypedDict
from langgraph.graph import StateGraph,START,END
from langgraph.types import interrupt

class State(TypedDict):
    phase:str

def build_assisted_graph(turn):
    async def prepare(state):return {'phase':await turn.prepare()}
    async def gate(state):
        status=await turn.gate()
        if status=='human_confirmation_required':
            interrupt({'kind':'human_confirmation_required'})
            # Resuming an interrupt is scheduling, never consent. Recheck on
            # entry/replay then route back for a fresh interrupt if still absent.
            status=await turn.gate()
        return {'phase':status}
    graph=StateGraph(State)
    graph.add_node('prepare',prepare);graph.add_node('gate',gate)
    graph.add_edge(START,'prepare')
    graph.add_conditional_edges('prepare',lambda s:END if s['phase']=='clarification' else 'gate')
    graph.add_conditional_edges('gate',lambda s:END if s['phase']=='acknowledged' else 'gate')
    return graph.compile(checkpointer=turn.saver)
