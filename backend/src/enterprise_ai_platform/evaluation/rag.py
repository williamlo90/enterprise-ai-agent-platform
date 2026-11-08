"""Versioned RAG evidence, serial execution and denominator-safe reporting.

This module does not create provider clients. Fixture reports cannot accept model
quality. A provider port must be explicitly supplied after the freeze preflight.
"""
import asyncio
import hashlib
import json
import math
import random
import re
import time
from collections import Counter
from datetime import date,datetime
from pathlib import Path
from typing import Literal,Protocol
from zoneinfo import ZoneInfo
from pydantic import BaseModel,ConfigDict,Field,model_validator

# EVAL-01 prepares the framework. MODEL-SEL-03A installs/reviews the actual
# application/provider executor with per-egress durable reservations separately.
LIVE_EXECUTOR_INSTALLED=False

Split=Literal['development','calibration','acceptance']
Behavior=Literal['answer','clarification','insufficient_evidence','source_conflict']
class EvaluationError(ValueError):pass
class Record(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
class Actor(Record):
    principal_id:str
    membership_id:str
    access_revision:int=Field(ge=1)
class Document(Record):
    unit_id:str
    equivalent_fact:str
    tenant:Literal['A','B']
    language:Literal['id','en']
    split:Split
    version_id:str
    page:int=Field(ge=1)
    chunk:int=Field(ge=0)
    access_revision:int=Field(ge=1)
    publication_state:Literal['published','superseded','withdrawn']
    valid_from:str|None
    valid_until:str|None
    readers:list[str]
    text:str=Field(min_length=1,max_length=384)
    text_digest:str=Field(pattern='^[a-f0-9]{64}$')
    @model_validator(mode='after')
    def check(self):
        if len(self.text.encode())>384 or hashlib.sha256(self.text.encode()).hexdigest()!=self.text_digest:raise EvaluationError('corpus_digest')
        if self.valid_from:date.fromisoformat(self.valid_from)
        if self.valid_until:date.fromisoformat(self.valid_until)
        if self.valid_from and self.valid_until and self.valid_from>=self.valid_until:raise EvaluationError('corpus_period')
        return self
class Turn(Record):
    question:str=Field(min_length=1,max_length=200)
    expected:Behavior
    required_material_facts:list[str]
class Case(Record):
    id:str
    family_id:str
    version:int=Field(ge=1)
    split:Split
    tenant:Literal['A','B']
    language:Literal['id','en']
    actor:Actor
    evaluated_at:str
    turns:list[Turn]=Field(min_length=1,max_length=3)
    required_evidence_units:list[list[str]]
    forbidden_evidence_ids:list[str]
    forbidden_disclosure:list[str]
    @property
    def behavior(self):return self.turns[0].expected
class Pack(Record):
    schema_version:Literal[1]
    version:str
    documents:list[Document]
    cases:list[Case]
    gold_review:Literal['proposed_pending_human','human_reviewed']
    gold_reviewer:str|None
    @model_validator(mode='after')
    def check(self):
        docs={d.unit_id:d for d in self.documents}
        if len(docs)!=len(self.documents) or len({c.id for c in self.cases})!=len(self.cases):raise EvaluationError('duplicate_id')
        families={};scenarios={};facts_across_splits={}
        for c in self.cases:
            clock=datetime.fromisoformat(c.evaluated_at)
            if clock.tzinfo is None:raise EvaluationError('clock_requires_timezone')
            accumulated=[]
            for turn in c.turns:
                accumulated.append(turn.question);query=' '.join(accumulated)
                if len(query)>200 or len(query.encode())>400:raise EvaluationError('question_budget')
            scenario=tuple(' '.join(t.question.casefold().split()) for t in c.turns)
            if scenario in scenarios and scenarios[scenario]!=c.split:raise EvaluationError('scenario_crosses_split')
            scenarios[scenario]=c.split
            if c.family_id in families and families[c.family_id]!=c.split:raise EvaluationError('family_crosses_split')
            families[c.family_id]=c.split
            if len(c.turns[0].question.encode())>400:raise EvaluationError('question_budget')
            if c.behavior=='clarification':
                if len(c.turns)<2 or c.turns[-1].expected!='answer':raise EvaluationError('clarification_needs_continuation')
            elif len(c.turns)!=1:raise EvaluationError('unexpected_continuation')
            groups=[]
            for alternatives in c.required_evidence_units:
                if not alternatives or len(set(alternatives))!=len(alternatives):raise EvaluationError('evidence_alternatives')
                chosen=[docs.get(v) for v in alternatives]
                if any(d is None or not eligible(d,c) for d in chosen):raise EvaluationError('ineligible_gold')
                facts={d.equivalent_fact for d in chosen}
                if len(facts)!=1 or next(iter(facts)) in groups:raise EvaluationError('duplicate_evidence_unit')
                groups.append(next(iter(facts)))
            source_facts=tuple(sorted(' '.join(docs[u].text.casefold().split()) for group in c.required_evidence_units for u in group))
            if source_facts:
                if source_facts in facts_across_splits and facts_across_splits[source_facts]!=c.split:raise EvaluationError('source_facts_cross_split')
                facts_across_splits[source_facts]=c.split
            if c.turns[-1].expected in ('answer','source_conflict') and not groups:raise EvaluationError('missing_gold')
            if c.behavior=='source_conflict' and len(groups)<2:raise EvaluationError('conflict_needs_distinct_units')
            if c.behavior=='insufficient_evidence' and groups:raise EvaluationError('abstain_gold')
            if any(v not in docs or eligible(docs[v],c) for v in c.forbidden_evidence_ids):raise EvaluationError('forbidden_is_not_ineligible')
        counts=Counter((c.tenant,c.language,c.behavior) for c in self.cases if c.split=='acceptance')
        expected=Counter({(t,l,b):n for t in ['A','B'] for l in ['id','en'] for b,n in [('answer',12),('clarification',4),('insufficient_evidence',2),('source_conflict',2)]})
        if counts!=expected:raise EvaluationError('acceptance_80_matrix')
        if self.gold_review=='human_reviewed' and not self.gold_reviewer:raise EvaluationError('missing_gold_reviewer')
        return self

def eligible(document:Document,case:Case):
    now=datetime.fromisoformat(case.evaluated_at)
    if now.tzinfo is None:raise EvaluationError('clock_requires_timezone')
    day=now.astimezone(ZoneInfo('Asia/Singapore')).date().isoformat()
    return document.tenant==case.tenant and document.split==case.split and document.publication_state=='published' and case.actor.principal_id in document.readers and (document.valid_from is None or document.valid_from<=day) and (document.valid_until is None or day<document.valid_until)

class Attempt(Record):
    attempt_id:str=Field(pattern='^[a-zA-Z0-9:_-]{1,150}$')
    kind:Literal['chat','query_embedding','document_embedding']
    outcome:Literal['ok','error','timeout','unknown']
    input_tokens:int|None=Field(default=None,ge=0)
    output_tokens:int|None=Field(default=None,ge=0)
    cost_usd:float|None=Field(default=None,ge=0)
    unknown_reserved_estimate_usd:float|None=Field(default=None,ge=0)
class Output(Record):
    kind:Literal['answer','clarification','insufficient_evidence','source_conflict','service_unavailable']
    text:str=Field(max_length=8192)
    final_retrieved_units:list[str]=Field(max_length=5)
    cited_units:list[str]=Field(max_length=2)
class Observation(Record):
    case_id:str
    configuration_sha256:str=Field(pattern='^[a-f0-9]{64}$')
    run:int=Field(ge=1,le=3)
    status:Literal['ok','error','timeout','missing']
    latency_ms:float|None=Field(default=None,ge=0)
    human_wait_ms:float=Field(default=0,ge=0)
    outputs:list[Output]=Field(default_factory=list,max_length=3)
    attempts:list[Attempt]=Field(default_factory=list)
    accounting_complete:bool=False
class Adjudication(Record):
    configuration_sha256:str=Field(pattern='^[a-f0-9]{64}$')
    output_sha256:str=Field(pattern='^[a-f0-9]{64}$')
    case_id:str
    run:int=Field(ge=1,le=3)
    reviewer_kind:Literal['human','model','fixture']
    reviewer:str=Field(min_length=1)
    rubric_version:str
    facts_correct:bool
    complete:bool
    grounded:bool
    language_correct:bool
    continuation_complete:bool
    critical_errors:list[str]
    note_ref:str
class Manifest(Record):
    schema_version:Literal[1]
    mode:Literal['fixture','provider']
    split:Split
    repeats:Literal[3]
    concurrency:Literal[1]
    max_inference_attempts:int|None=Field(default=None,ge=1,le=2000)
    phase_timeout_ms:int=Field(default=900000,ge=1,le=3600000)
    dataset_sha256:str
    dataset_content_sha256:str|None=None
    rubric_sha256:str
    model_id:str
    embedding_model_id:str
    document_embedding_model_id:str
    source_revision:str
    dependency_lock_sha256:str
    prompt_ref:str
    retrieval_mode:Literal['vector','hybrid','rerank']
    region:str
    data_scope:str
    workload:str
    cache_mode:Literal['cold','warm','mixed']
    case_timeout_ms:int=Field(ge=1,le=120000)
    seed:int
    preprocessing_allocation:Literal['equal_runs']
    preprocessing_attempts:list[Attempt]
    infrastructure_cost_usd:float|None=Field(default=None,ge=0)
    price_snapshot_ref:str|None
    frozen_at:str|None
    approved_by:str|None
    budget_approved_by:str|None
    budget_limit_usd:float|None=Field(default=None,gt=0)
    quality_gates:dict[str,float]|None
    p95_gate_ms:float|None=Field(default=None,gt=0)
    cost_per_success_gate_usd:float|None=Field(default=None,gt=0)
    max_error_fraction:float|None=Field(default=None,ge=0,le=1)
    corpus_embedding_provenance_sha256:str|None=None
    provider_egress_approved:bool=False
    representative_embeddings:bool=False

def digest(path:Path):return hashlib.sha256(path.read_bytes()).hexdigest()
def strict_json(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise EvaluationError('duplicate_json_key')
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(EvaluationError('nonfinite_json')))
def load_pack(path:Path):return Pack.model_validate(strict_json(path.read_text(encoding='utf-8')))

def pack_content_hash(pack:Pack):return hashlib.sha256(json.dumps(pack.model_dump(mode='json'),sort_keys=True,separators=(',',':')).encode()).hexdigest()

def configuration_hash(manifest:Manifest):return hashlib.sha256(json.dumps(manifest.model_dump(mode='json'),sort_keys=True,separators=(',',':')).encode()).hexdigest()

def review_hash(observation:Observation):
    scope={k:v for k,v in observation.model_dump(mode='json').items() if k in ['case_id','run','configuration_sha256','status','outputs']}
    return hashlib.sha256(json.dumps(scope,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def preflight(pack:Pack,manifest:Manifest,dataset_hash:str,rubric_hash:str,*,executor=None):
    missing=[]
    if manifest.dataset_content_sha256 is not None and pack_content_hash(pack)!=manifest.dataset_content_sha256:missing.append('dataset_content_changed')
    if manifest.dataset_sha256!=dataset_hash or manifest.rubric_sha256!=rubric_hash:missing.append('dataset_or_rubric_hash')
    if manifest.mode=='provider':
        from enterprise_ai_platform.evaluation.provider import permitted
        authorized=permitted(executor,manifest,egress=False)
        if not authorized:missing.append('provider_executor_not_installed')
        required=['dataset_content_sha256','model_id','embedding_model_id','document_embedding_model_id','source_revision','dependency_lock_sha256','prompt_ref','region','data_scope','workload','price_snapshot_ref','frozen_at','approved_by','budget_approved_by','budget_limit_usd','max_inference_attempts','corpus_embedding_provenance_sha256']
        if manifest.split=='acceptance':required+=['quality_gates','p95_gate_ms','cost_per_success_gate_usd']
        for name in required:
            if not getattr(manifest,name):missing.append(name)
        if manifest.embedding_model_id!=manifest.document_embedding_model_id or not manifest.representative_embeddings:missing.append('representative_embeddings')
        if manifest.split=='acceptance' and manifest.max_error_fraction is None:missing.append('max_error_fraction')
        if (manifest.split=='acceptance' and manifest.quality_gates is None) or (manifest.quality_gates is not None and (set(manifest.quality_gates)!={'answer','clarification','insufficient_evidence','source_conflict','recall'} or any(not math.isfinite(v) or not 0<=v<=1 for v in manifest.quality_gates.values()))):missing.append('quality_gates')
        for name in ['dataset_sha256','rubric_sha256','dependency_lock_sha256','corpus_embedding_provenance_sha256']:
            if not re.fullmatch('[a-f0-9]{64}',getattr(manifest,name) or ''):missing.append(name)
        if not manifest.provider_egress_approved:missing.append('provider_egress_approval')
        if pack.gold_review!='human_reviewed' and not (authorized and executor.gold_dataset_sha256==dataset_hash):missing.append('human_gold_review')
    return sorted(set(missing))

def percentile(values,quantile):
    if not values:return None
    ordered=sorted(values);return ordered[max(0,math.ceil(len(ordered)*quantile)-1)]
def cost(attempts):
    if len({a.attempt_id for a in attempts})!=len(attempts):raise EvaluationError('duplicate_billed_attempt')
    unknown=sum(a.cost_usd is None for a in attempts)
    return {'known_usd':math.fsum(a.cost_usd for a in attempts if a.cost_usd is not None),'unknown_attempts':unknown,'unknown_reserved_estimate_usd':math.fsum(a.unknown_reserved_estimate_usd for a in attempts if a.unknown_reserved_estimate_usd is not None)}

def report(pack:Pack,manifest:Manifest,observations:list[Observation],reviews:list[Adjudication],*,dataset_hash:str,rubric_hash:str,executor=None):
    if manifest.dataset_content_sha256 is not None and pack_content_hash(pack)!=manifest.dataset_content_sha256:raise EvaluationError('report_gold_changed')
    cases=[c for c in pack.cases if c.split==manifest.split];expected={(run,c.id) for run in range(1,4) for c in cases};docs={d.unit_id:d for d in pack.documents}
    if any(o.configuration_sha256!=configuration_hash(manifest) for o in observations):raise EvaluationError('mixed_configuration')
    observed={(o.run,o.case_id):o for o in observations};adjudicated={(r.run,r.case_id):r for r in reviews}
    if len(observed)!=len(observations) or not set(observed)<=expected or len(adjudicated)!=len(reviews) or not set(adjudicated)<=expected:raise EvaluationError('duplicate_or_unscheduled_observation')
    for key,review in adjudicated.items():
        if review.configuration_sha256!=configuration_hash(manifest) or key not in observed or review.output_sha256!=review_hash(observed[key]):raise EvaluationError('adjudication_provenance_mismatch')
    rows=[]
    for run in range(1,4):
        for case in cases:
            o=observed.get((run,case.id));r=adjudicated.get((run,case.id));outputs=o.outputs if o else [];critical=set(r.critical_errors if r else [])
            for output in outputs:
                if not set(output.cited_units)<=set(output.final_retrieved_units):critical.add('citation_not_in_context')
                if any(v not in docs or not eligible(docs[v],case) for v in output.final_retrieved_units+output.cited_units):critical.add('unauthorized_evidence')
                if any(canary in output.text for canary in case.forbidden_disclosure):critical.add('forbidden_disclosure')
            if r and not r.facts_correct:critical.add('material_fact')
            if r and not r.grounded:critical.add('ungrounded_claim')
            behavior_ok=bool(o and o.status=='ok' and len(outputs)==len(case.turns) and all(output.kind==turn.expected for output,turn in zip(outputs,case.turns)))
            required=case.required_evidence_units
            retrieved=set(outputs[-1].final_retrieved_units if outputs else [])
            recall=sum(bool(retrieved.intersection(group)) for group in required)/len(required) if required else None
            reviewed=bool(r and r.reviewer_kind=='human' and r.rubric_version==rubric_hash)
            passed=behavior_ok and reviewed and r.facts_correct and r.complete and r.grounded and r.language_correct and (case.behavior!='clarification' or r.continuation_complete) and not critical
            rows.append(dict(run=run,case_id=case.id,family_id=case.family_id,tenant=case.tenant,language=case.language,behavior=case.behavior,status=o.status if o else 'missing',pass_rubric=bool(passed),human_review_complete=reviewed,behavior_match=behavior_ok,recall=recall if case.behavior=='answer' else None,latency_ms=o.latency_ms if o else None,human_wait_ms=o.human_wait_ms if o else None,critical_errors=sorted(critical),accounting_complete=bool(o and o.accounting_complete)))
    slices=[]
    for run in range(1,4):
        for tenant in ['A','B']:
            for language in ['id','en']:
                for behavior in ['answer','clarification','insufficient_evidence','source_conflict']:
                    selected=[r for r in rows if (r['run'],r['tenant'],r['language'],r['behavior'])==(run,tenant,language,behavior)]
                    if selected:slices.append(dict(run=run,tenant=tenant,language=language,behavior=behavior,passed=sum(r['pass_rubric'] for r in selected),scheduled=len(selected),pending_human=sum(not r['human_review_complete'] for r in selected),recall_macro=sum(r['recall'] or 0 for r in selected)/len(selected) if behavior=='answer' else None,status_counts=dict(Counter(r['status'] for r in selected)),latency_p95_ms=percentile([r['latency_ms'] for r in selected if r['latency_ms'] is not None],.95),latency_measured=sum(r['latency_ms'] is not None for r in selected)))
    all_attempts=[a for o in observations for a in o.attempts]+manifest.preprocessing_attempts;costs=cost(all_attempts);successes=sum(r['pass_rubric'] for r in rows)
    known_cost=costs['known_usd'];fully_known=not costs['unknown_attempts'] and all(r['accounting_complete'] for r in rows)
    critical=any(r['critical_errors'] for r in rows);gaps=preflight(pack,manifest,dataset_hash,rubric_hash,executor=executor)
    # Gate results remain explicit; high averages cannot hide failed slices.
    quality=bool(manifest.quality_gates) and all(s['passed']/s['scheduled']>=manifest.quality_gates.get(s['behavior'],1.0) and (s['recall_macro'] is None or s['recall_macro']>=manifest.quality_gates.get('recall',1.0)) for s in slices)
    latency=[r['latency_ms'] for r in rows if r['latency_ms'] is not None];p95=percentile(latency,.95)
    model_cost_per_success=known_cost/successes if fully_known and successes else None
    fully_known_all_in=fully_known and manifest.infrastructure_cost_usd is not None
    all_in=known_cost+manifest.infrastructure_cost_usd if fully_known_all_in else None
    cost_per_success=all_in/successes if all_in is not None and successes else None
    def scope_metrics(selected):
        keys={(r['run'],r['case_id']) for r in selected};fraction=len(selected)/len(rows) if rows else 0
        scoped=cost([a for o in observations if (o.run,o.case_id) in keys for a in o.attempts]);prep=cost(manifest.preprocessing_attempts)
        known=scoped['known_usd']+prep['known_usd']*fraction
        complete_accounting=not scoped['unknown_attempts'] and not prep['unknown_attempts'] and all(r['accounting_complete'] for r in selected)
        infra=manifest.infrastructure_cost_usd*fraction if manifest.infrastructure_cost_usd is not None else None
        total=known+infra if complete_accounting and infra is not None else None;passed=sum(r['pass_rubric'] for r in selected)
        values=[r['latency_ms'] for r in selected if r['latency_ms'] is not None]
        return {'scheduled':len(selected),'passed':passed,'latency_p50_ms':percentile(values,.5),'latency_p95_ms':percentile(values,.95),'latency_measured':len(values),'status_counts':dict(Counter(r['status'] for r in selected)),'cost':{'known_model_usd':known,'unknown_attempts':scoped['unknown_attempts']+prep['unknown_attempts'],'unknown_reserved_estimate_usd':scoped['unknown_reserved_estimate_usd']+prep['unknown_reserved_estimate_usd']*fraction,'infrastructure_allocated_usd':infra,'all_in_usd':total,'all_in_per_success_usd':total/passed if total is not None and passed else None,'allocation_fraction':fraction}}
    for scope in slices:
        selected=[r for r in rows if (r['run'],r['tenant'],r['language'],r['behavior'])==(scope['run'],scope['tenant'],scope['language'],scope['behavior'])];scope.update(scope_metrics(selected))
    run_summaries=[{'run':run,**scope_metrics([r for r in rows if r['run']==run])} for run in range(1,4)]
    complete=len(observations)==len(expected) and all(r['human_review_complete'] and r['status']!='missing' and r['latency_ms'] is not None for r in rows)
    latency_gate=manifest.p95_gate_ms is not None and all(s['latency_measured']==s['scheduled'] and s['latency_p95_ms']<=manifest.p95_gate_ms for s in slices)
    error_gate=manifest.max_error_fraction is not None and all((s['scheduled']-s['status_counts'].get('ok',0))/s['scheduled']<=manifest.max_error_fraction for s in slices)
    cost_gate=manifest.cost_per_success_gate_usd is not None and all(scope['cost']['all_in_per_success_usd'] is not None and scope['cost']['all_in_per_success_usd']<=manifest.cost_per_success_gate_usd for scope in slices)
    acceptable=manifest.mode=='provider' and manifest.split=='acceptance' and not gaps and not critical and complete and quality and latency_gate and error_gate and cost_gate
    return dict(schema_version=1,mode=manifest.mode,split=manifest.split,unique_cases=len(cases),unique_families=len({c.family_id for c in cases}),repeats=3,scheduled=len(rows),observed=len(observations),slices=slices,runs=run_summaries,cases=rows,critical_veto=critical,preflight_gaps=gaps,quality_gate_met=quality,acceptance_allowed=bool(acceptable),latency=dict(p50_ms=percentile(latency,.5),p95_ms=p95,measured=len(latency),status_counts=dict(Counter(r['status'] for r in rows)),timeout_limit_ms=manifest.case_timeout_ms,human_wait_separate=True),cost={**costs,'accounting_complete':fully_known,'fully_known_model_cost_usd':known_cost if fully_known else None,'model_cost_per_success_usd':model_cost_per_success,'all_in_cost_usd':all_in,'all_in_cost_per_success_usd':cost_per_success,'all_in_accounting_complete':fully_known_all_in,'successful_tasks':successes,'preprocessing_allocation':'equal_runs; included once in cohort total','preprocessing_attempts':len(manifest.preprocessing_attempts),'infrastructure_cost_usd':manifest.infrastructure_cost_usd},configuration_sha256=configuration_hash(manifest),complete=complete,latency_gate_met=latency_gate,cost_gate_met=cost_gate,error_gate_met=error_gate,note='Fixture results validate the framework only; no provider quality or human pilot claim.' if manifest.mode=='fixture' else 'Provider records require source-bound execution evidence and actual human adjudication; this report does not prove deployment or user ownership.')

class EvaluationPort(Protocol):
    async def execute(self,case:Case,run:int)->list[Output]:...
    async def collect_attempts(self,case_id:str,run:int)->tuple[list[Attempt],bool]:...

async def run_serial(pack:Pack,manifest:Manifest,port:EvaluationPort,*,dataset_hash:str,rubric_hash:str):
    if manifest.mode=='provider':raise EvaluationError('provider_executor_not_installed')
    if preflight(pack,manifest,dataset_hash,rubric_hash):raise EvaluationError('preflight_not_frozen')
    results=[]
    for run in range(1,4):
        cases=[c for c in pack.cases if c.split==manifest.split];random.Random(manifest.seed+run).shuffle(cases)
        for case in cases:
            began=time.perf_counter();outputs=[];status='ok'
            try:
                async with asyncio.timeout(manifest.case_timeout_ms/1000):
                    outputs=await port.execute(case,run)
                    if not isinstance(outputs,list) or any(not isinstance(output,Output) for output in outputs):raise EvaluationError('invalid_port_output')
            except TimeoutError:status='timeout'
            except Exception:status='error'
            elapsed=(time.perf_counter()-began)*1000
            try:
                async with asyncio.timeout(min(5,manifest.case_timeout_ms/1000)):attempts,complete=await port.collect_attempts(case.id,run)
            except Exception:attempts,complete=[],False
            results.append(Observation(case_id=case.id,configuration_sha256=configuration_hash(manifest),run=run,status=status,latency_ms=elapsed,outputs=outputs,attempts=attempts,accounting_complete=complete))
    return results
