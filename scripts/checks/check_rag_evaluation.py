"""Offline framework proof: no environment secrets or provider imports."""
import asyncio
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend/src'))
from enterprise_ai_platform.evaluation.rag import (
    Adjudication,EvaluationError,Manifest,Output,digest,
    load_pack,report,review_hash,run_serial,strict_json,
)

PACK=ROOT/'evaluation/rag/v1'
NAMES={'pack.json','rubric.json','fixture-config.json','workflow-plan.json'}

def load_inputs():
    index=strict_json((PACK/'manifest.json').read_text(encoding='utf-8'))
    if set(index['files'])!=NAMES:raise EvaluationError('pack_file_inventory')
    if any(digest(PACK/name)!=value for name,value in index['files'].items()):raise EvaluationError('pack_file_hash')
    pack=load_pack(PACK/'pack.json')
    manifest=Manifest.model_validate(strict_json((PACK/'fixture-config.json').read_text(encoding='utf-8')))
    if manifest.mode!='fixture':raise EvaluationError('offline_checker_only')
    return pack,manifest

class FixturePort:
    """Gold-driven oracle ONLY for runner/report plumbing, not model quality.

    A future real executor receives questions/identity/source bindings WITHOUT
    gold fields and records actual ctx.hits with source version/digest binding.
    """
    async def execute(self,case,run):
        units=[group[0] for group in case.required_evidence_units]
        return [Output(kind=turn.expected,text='Synthetic framework response',
            final_retrieved_units=units if turn.expected!='clarification' else [],
            cited_units=units[:2] if turn.expected!='clarification' else []) for turn in case.turns]
    async def collect_attempts(self,case_id,run):return [],True

def fixture_reviews(observations,rubric_hash):
    return [Adjudication(configuration_sha256=o.configuration_sha256,output_sha256=review_hash(o),
        case_id=o.case_id,run=o.run,reviewer_kind='fixture',reviewer='offline-oracle',
        rubric_version=rubric_hash,facts_correct=True,complete=True,grounded=True,
        language_correct=True,continuation_complete=True,critical_errors=[],
        note_ref='Framework oracle; never human or model quality evidence.') for o in observations]

async def check():
    pack,manifest=load_inputs();outputs=[]
    for split in ['development','calibration','acceptance']:
        config=manifest.model_copy(update={'split':split})
        observations=await run_serial(pack,config,FixturePort(),dataset_hash=manifest.dataset_sha256,rubric_hash=manifest.rubric_sha256)
        result=report(pack,config,observations,fixture_reviews(observations,manifest.rubric_sha256),dataset_hash=manifest.dataset_sha256,rubric_hash=manifest.rubric_sha256)
        assert result['observed']==result['scheduled'] and not result['acceptance_allowed']
        assert all(r['behavior_match'] for r in result['cases']) and not result['critical_veto']
        assert all(not r['human_review_complete'] for r in result['cases'])
        outputs.append(result)
    out=ROOT/'local/eval01';out.mkdir(parents=True,exist_ok=True)
    (out/'framework-reports.json').write_text(json.dumps(outputs,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    proof={'passed':True,'scope':'offline framework only; gold-driven fixture',
        'cases':len(pack.cases),'acceptance_cases':80,'repeats':3,
        'scheduled':sum(r['scheduled'] for r in outputs),'max_parallel_cases':1,
        'human_gold_review':'pending','provider_calls':0,'paid_calls':0,
        'model_quality_accepted':False,'dataset_sha256':manifest.dataset_sha256,
        'rubric_sha256':manifest.rubric_sha256,
        'configurations':[r['configuration_sha256'] for r in outputs]}
    (out/'framework-proof.json').write_text(json.dumps(proof,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(proof))

if __name__=='__main__':asyncio.run(check())
