"""Sequential graph; raw prompts, source text and generated quotes stay out of checkpoints."""
from typing import TypedDict
from langgraph.graph import StateGraph,START,END

class State(TypedDict):
    phase:str

def build_rag_graph(turn,models):
    async def retrieve(state):
        await turn.retrieve(models.embedding);return {'phase':'retrieved'}
    async def generate(state):
        await turn.generate(models.chat);return {'phase':'generated'}
    graph=StateGraph(State);graph.add_node('retrieve',retrieve);graph.add_node('generate',generate)
    graph.add_edge(START,'retrieve');graph.add_edge('retrieve','generate');graph.add_edge('generate',END)
    return graph.compile(checkpointer=turn.saver)
