import json
from enterprise_ai_platform.modules.rag.domain import context,parse_proposal,RagFailure,sha
import pytest

def hit(text):return dict(text=text,text_digest=sha(text),start=0,page=1,version_id='v1')

def test_current_source_id_still_requires_its_own_exact_quote_after_prompt_revision():
    ctx=context('Baggage limit?','en',[hit('Fictional baggage limit is8kg.'),dict(hit('Fictional baggage limit is9kg.'),version_id='v2')])
    with pytest.raises(RagFailure):parse_proposal(json.dumps(dict(kind='answer',citations=[dict(id='S2',quote='Fictional baggage limit is8kg.')])),ctx)
    assert len(ctx.prompt.encode())<=1900
