"""Serial, durable evaluation coordination. No provider executor is enabled."""
import asyncio
import hashlib
import math
import random
import time
from typing import Protocol,AsyncIterator
from enterprise_ai_platform.evaluation.execution import ExecutionRequest
from enterprise_ai_platform.evaluation.rag import EvaluationError,Observation,Output,configuration_hash,preflight


class ExecutionPort(Protocol):
    def execute(self,request:ExecutionRequest,run:int)->AsyncIterator[Output]:...


async def drain(task):
    """Await owned execution cleanup, including repeated caller cancellation."""
    cancelled=None
    initial_cancellations=asyncio.current_task().cancelling()
    while not task.done():
        try:await asyncio.shield(task)
        except asyncio.CancelledError as error:
            # A cancelled child also raises here after its cleanup finishes;
            # that is not an additional cancellation of this coordinator.
            if not task.done() or asyncio.current_task().cancelling()>initial_cancellations:cancelled=error
        except BaseException:break
    # Retrieve the outcome so no owned task exception is left unobserved.
    try:task.result()
    except BaseException:pass
    return cancelled


async def run_case(request,run,manifest,journal,port,*,executor=None):
    if manifest.mode!='fixture':
        from enterprise_ai_platform.evaluation.provider import permitted
        if not permitted(executor,manifest):raise EvaluationError('provider_executor_not_installed')
    if type(run) is not int or not 1<=run<=3:raise EvaluationError('invalid_evaluation_run')
    if journal.fingerprint!=configuration_hash(manifest):raise EvaluationError('journal_configuration_mismatch')
    request_hash=hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    claimed=journal.claim_case(request.case_id,run,request_hash)
    previous=journal.get(request.case_id,run)
    if previous is not None:return previous
    if not claimed:raise EvaluationError('case_execution_claimed_review_required')
    prior_attempts=journal.attempts(request.case_id,run)
    if prior_attempts:
        # A crash may occur after SDK egress but before output publication. Never
        # automatically replay such a case, even if usage later became known.
        observation=Observation(case_id=request.case_id,configuration_sha256=journal.fingerprint,
            run=run,status='error',attempts=prior_attempts,accounting_complete=all(a.cost_usd is not None for a in prior_attempts))
        journal.append(observation);return observation
    began=time.perf_counter();outputs=[];status='ok';cancelled=None
    async def consume():
        nonlocal status
        async for output in port.execute(request,run):
            if not isinstance(output,Output) or len(outputs)>=len(request.questions):raise EvaluationError('invalid_execution_output')
            journal.save_partial(request.case_id,run,output)
            outputs.append(output)
            if output.kind=='service_unavailable':status='error'
        if len(outputs)!=len(request.questions):raise EvaluationError('incomplete_execution_output')
    task=asyncio.create_task(consume())
    try:
        async with asyncio.timeout(manifest.case_timeout_ms/1000):await asyncio.shield(task)
    except TimeoutError:
        status='timeout';task.cancel();cancelled=await drain(task)
    except asyncio.CancelledError as error:
        status='error';task.cancel();cancelled=await drain(task) or error
    except Exception:
        status='error'
    except BaseException:
        status='error';raise
    finally:
        if not task.done():
            task.cancel();cancelled=await drain(task) or cancelled
        attempts=journal.attempts(request.case_id,run)
        observation=Observation(case_id=request.case_id,configuration_sha256=journal.fingerprint,
            run=run,status=status,latency_ms=(time.perf_counter()-began)*1000,outputs=outputs,
            attempts=attempts,accounting_complete=all(a.cost_usd is not None for a in attempts))
        # Synchronous transaction has no cancellation point. Attempts were already
        # durable before SDK calls; a journal failure propagates and halts the run.
        journal.append(observation)
    if cancelled is not None:raise cancelled
    return observation


async def run_dataset(pack,manifest,journal,port,*,dataset_hash,rubric_hash,phase_deadline=None,executor=None):
    if manifest.mode!='fixture':
        from enterprise_ai_platform.evaluation.provider import permitted
        if not permitted(executor,manifest):raise EvaluationError('provider_executor_not_installed')
    if preflight(pack,manifest,dataset_hash,rubric_hash,executor=executor):raise EvaluationError('preflight_not_frozen')
    deadline=time.monotonic()+manifest.phase_timeout_ms/1000
    if phase_deadline is not None:
        if type(phase_deadline) not in (int,float) or not math.isfinite(phase_deadline):raise EvaluationError('invalid_phase_deadline')
        deadline=min(deadline,phase_deadline)
    async with asyncio.timeout(max(0,deadline-time.monotonic())):
        return await _execute_dataset(pack,manifest,journal,port,executor=executor)


async def _execute_dataset(pack,manifest,journal,port,*,executor=None):
    results=[]
    for run in range(1,4):
        requests=[ExecutionRequest.from_case(case) for case in pack.cases if case.split==manifest.split]
        random.Random(manifest.seed+run).shuffle(requests)
        for request in requests:
            if journal.get(request.case_id,run) is None and journal.stop_requested():raise EvaluationError('phase_budget_stopped')
            results.append(await run_case(request,run,manifest,journal,port,executor=executor))
    return results


def finalize_interrupted_case(request,run,manifest,journal,*,reason):
    """Offline operator recovery, never a provider replay or claim deletion.

    The entry point must exclusively lock the stopped worker's run directory.
    """
    from enterprise_ai_platform.evaluation.rag import Output
    if reason not in ('process_interrupted','operator_reviewed_timeout') or type(run) is not int or not 1<=run<=3 or journal.fingerprint!=configuration_hash(manifest):raise EvaluationError('recovery_scope')
    digest=hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    with journal.connect() as db:claim=db.execute('SELECT request_sha256 FROM case_claims WHERE case_id=? AND run=?',(request.case_id,run)).fetchone()
    if claim!=(digest,):raise EvaluationError('recovery_claim_mismatch')
    previous=journal.get(request.case_id,run)
    if previous is not None:return previous
    outputs=journal.partials(request.case_id,run)
    if len(outputs)>len(request.questions):raise EvaluationError('recovery_partial_mismatch')
    attempts=journal.attempts(request.case_id,run)
    result=Observation(case_id=request.case_id,configuration_sha256=journal.fingerprint,run=run,status='timeout' if reason=='operator_reviewed_timeout' else 'error',outputs=outputs,attempts=attempts,accounting_complete=all(a.cost_usd is not None for a in attempts))
    journal.append(result);return result
