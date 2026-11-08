import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4
import pytest
from enterprise_ai_platform.modules.rag.domain import Context,RagFailure,context,parse_proposal,render,bindings,sha,packed

from enterprise_ai_platform.modules.rag.service import sources

def hit(text='Travel claims must be submitted within 7 calendar days after returning.',**values):
    return dict(version_id=str(uuid4()),version_no=1,index_generation=1,access_revision=1,chunk_no=0,page=1,start=0,text=text,text_digest=sha(text),**values)

def proposal(kind='answer',quote=None,id='S1'):
    return packed(dict(kind=kind,citations=[] if quote is None else [dict(id=id,quote=quote)]))

@pytest.mark.parametrize('change',[dict(question=''),dict(question='x'*201),dict(language='fr'),dict(question='あ'*150)])
def test_invalid_or_oversized_question_not_silently_truncated(change):
    values=dict(question='What is the deadline?',language='en',hits=[hit()]);values.update(change)
    with pytest.raises(RagFailure):context(**values)

def test_context_bounded_unique_evidence_and_all_dependencies():
    h=hit();rows=[h,dict(h),hit('Klaim harus diajukan dalam tujuh hari setelah pulang.')]
    ctx=context('Apa tenggatnya?','id',rows)
    assert len(ctx.hits)==2 and len(ctx.prompt.encode())<=1900
    assert len(sources(ctx.hits))==2 and len(bindings(ctx.hits))==2
    output=render(parse_proposal(proposal(quote=h['text']),ctx),ctx,'id')
    assert output['text'].startswith('Berdasarkan sumber:') and h['text'] in output['text']
    assert output['citations'][0]['version_id']==h['version_id']

@pytest.mark.parametrize('value',[
    '{"kind":"answer","kind":"clarification","citations":[]}',
    '{"kind":"answer","citations":[],"approval":true}',
    '{"kind":"answer","citations":[]}',
    '{"kind":"insufficient_evidence","citations":[{"id":"S1","quote":"x"}]}',
    '{"kind":"paid","citations":[]}',
    '```json\n{}\n```',
])
def test_model_structure_rejects_duplicate_keys_free_claims_unsupported_types(value):
    with pytest.raises(RagFailure):parse_proposal(value,context('Question?','en',[hit()]))

@pytest.mark.parametrize('quote,id',[
 ('Claims must be submitted within 14 days.','S1'),
 ('7 calendar days','S1'),
 ('Travel claims must be submitted within 7 calendar days after returning.','S2'),
 ('Travel claims must be submitted within 7 calendar days after returning.','S6'),
])
def test_citations_require_context_id_and_exact_complete_sentence(quote,id):
    with pytest.raises(RagFailure):parse_proposal(proposal(quote=quote,id=id),context('Question?','en',[hit()]))

def test_dropping_negation_or_condition_cannot_form_valid_quote():
    h=hit('Claims are not payable unless receipts are verified.')
    with pytest.raises(RagFailure):parse_proposal(proposal(quote='Claims are payable.'),context('Question?','en',[h]))
    with pytest.raises(RagFailure):parse_proposal(proposal(quote='receipts are verified.'),context('Question?','en',[h]))

def test_conflict_requires_two_distinct_version_sources():
    h=hit('Claims must be submitted within 7 days.');ctx=context('Question?','en',[h,hit('Claims must be submitted within 14 days.')])
    p=dict(kind='source_conflict',citations=[dict(id='S1',quote=ctx.hits[0]['text']),dict(id='S2',quote=ctx.hits[1]['text'])])
    assert parse_proposal(packed(p),ctx)['kind']=='source_conflict'
    p['citations'][1]['id']='S1'
    with pytest.raises(RagFailure):parse_proposal(packed(p),ctx)

def test_context_digest_tampering_fails_before_model():
    h=hit();h['text']='altered'
    with pytest.raises(RagFailure):context('Question?','en',[h])

def test_prompt_budget_drops_entire_chunks_never_mutates_evidence():
    rows=[hit(('text'+str(i)+' ')*59+'.') for i in range(5)]
    ctx=context('Question?','en',rows)
    assert len(ctx.prompt.encode())<=1900 and len(ctx.hits)<5
    assert all(v in rows for v in ctx.hits)

def test_split_conditional_sentence_fragments_never_reach_model_or_answer():
    left=hit('Travel claims must be submitted within 7 days only when')
    right=hit('the employee has received written authorization.');right['start']=384
    ctx=context('Deadline?','en',[left,right])
    assert not ctx.hits and 'written authorization' not in ctx.prompt
    with pytest.raises(RagFailure):parse_proposal(proposal(quote=right['text']),ctx)

@pytest.mark.parametrize('deadline',[10,float('nan'),True,None])
def test_observer_rechecks_expiry_in_provider_thread_before_actual_sdk_call(deadline):
    from unittest.mock import Mock,AsyncMock
    from enterprise_ai_platform.modules.rag.service import RagTurn
    from enterprise_ai_platform.integrations.models.nova import NovaChat
    from enterprise_ai_platform.modules.models.ports import Message,ModelFailure
    async def run():
        repo=SimpleNamespace(call=AsyncMock(return_value=dict(authority_expires_at=deadline,settlement_receipt='a'*64)))
        turn=RagTurn(repo,None,None,None,None,clock=lambda:20);turn.active_kind='chat';turn.ctx=Context('hello',(),sha('empty'))
        client=SimpleNamespace(converse=Mock())
        adapter=NovaChat(client,turn.observer(asyncio.get_running_loop()))
        with pytest.raises((ModelFailure,RagFailure)):await asyncio.to_thread(adapter.chat,[Message('user','hello')])
        client.converse.assert_not_called()
    asyncio.run(run())

def test_real_nova_cohere_adapters_accept_bounded_rag_protocol_without_network():
    import io,json
    from unittest.mock import Mock
    from enterprise_ai_platform.composition import build_model_adapters
    from enterprise_ai_platform.modules.models.ports import Message
    text='Travel claims must be submitted within 7 calendar days.'
    ctx=context('Apa tenggat klaim?','id',[hit(text)])
    client=Mock();session=Mock();session.client.return_value=client;observed=[]
    client.converse.return_value={'output':{'message':{'role':'assistant','content':[{'text':proposal(quote=text)}]}},'stopReason':'end_turn','usage':{'inputTokens':250,'outputTokens':45,'totalTokens':295},'ResponseMetadata':{'RetryAttempts':0}}
    client.invoke_model.return_value={'body':io.BytesIO(json.dumps({'embeddings':{'float':[[1.0]+[0.0]*1023]}}).encode()),'ResponseMetadata':{'RetryAttempts':0,'HTTPHeaders':{'x-amzn-bedrock-input-token-count':'12'}}}
    with build_model_adapters(lambda *v:observed.append(v),fictional_apac=True,session=session) as models:
        vector=models.embedding.embed(['Apa tenggat klaim?'],input_type='search_query')
        answer=models.chat.chat([Message('user',ctx.prompt)],tools=False)
        assert parse_proposal(answer.text,ctx)['kind']=='answer' and len(vector.vectors[0])==1024
    request=json.loads(client.invoke_model.call_args.kwargs['body'])
    assert request['input_type']=='search_query' and request['truncate']=='NONE'
    assert 'toolConfig' not in client.converse.call_args.kwargs and observed==[('embedding',512),('chat',4096)]
