"""Serial scheduler/executor. Explicit runtime flags; never starts from API import."""
import asyncio,contextlib,json,logging,os,signal,time
from uuid import UUID
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.composition import build_resources,database_loop_factory,build_rag_turn,build_assisted_turn,build_model_adapters,build_workflow_model,workflow_worker
from enterprise_ai_platform.config import load_settings
from enterprise_ai_platform.modules.jobs.service import JobService
from enterprise_ai_platform.infrastructure.database.jobs import PostgresJobRepository
from enterprise_ai_platform.modules.jobs.ports import Lease
from enterprise_ai_platform.modules.files.service import FileInspection
from enterprise_ai_platform.modules.knowledge.service import KnowledgeIngestion
from enterprise_ai_platform.infrastructure.database.files import PostgresFileRepository
from enterprise_ai_platform.infrastructure.database.knowledge import PostgresKnowledgeRepository
from enterprise_ai_platform.infrastructure.storage.kubernetes_parser import KubernetesFileParser
from enterprise_ai_platform.integrations.models.nova import CohereEmbedding,NOVA_SDK_CONFIG
from enterprise_ai_platform.integrations.models.credentials import bound_session
from enterprise_ai_platform.integrations.models.bedrock import ENDPOINT
from enterprise_ai_platform.integrations.checkpoints.rag_graph import build_rag_graph
from enterprise_ai_platform.integrations.checkpoints.assisted_graph import build_assisted_graph

async def main_loop():
 settings=load_settings(None);resources=build_resources(settings);resources.background_refresh=False;stop=asyncio.Event();loop=asyncio.get_running_loop()
 for signum in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(signum,stop.set)
 parser=KubernetesFileParser(os.environ['EAI_PARSER_IMAGE_URI']);repo=PostgresJobRepository(resources.database)
 scheduler=JobService(repo,os.environ['SCHEDULER_CREDENTIAL']);executors=json.loads(os.environ['EXECUTORS_JSON'])
 try:
  async with resources.lifecycle():
   async with resources.database.transaction() as c:
    inventory=(await (await c.execute(sql.SQL('SELECT {}.worker_route(%s)').format(sql.Identifier(resources.database.schema)),(Jsonb(dict(credential_hash=scheduler._credential_hash,inventory=True)),))).fetchone())[0]
   if inventory.get('status')!='ok' or set(inventory['workers'])!=set(executors):raise ValueError('Unconfigured executor')
   while not stop.is_set():
    try:
     claim=await scheduler.claim()
     if claim.get('status') in ('empty','yield'):
      try:await asyncio.wait_for(stop.wait(),timeout=2)
      except TimeoutError:pass
      continue
     key=executors.get(claim['worker_id'])
     if not key:raise ValueError('Unconfigured executor')
     jobs=JobService(repo,key);lease=Lease(UUID(claim['id']),UUID(claim['tenant_id']),claim['generation'])
     ref=await jobs.reference(lease)
     async def work():
      kind=ref['job_type']
      if kind=='file.inspect':
       return await FileInspection(PostgresFileRepository(resources.database),resources.storage,parser,jobs,jobs._credential_hash).process(claim)
      if kind in ('knowledge.ingest','agent.resume') and os.environ.get('EAI_ENABLE_MODEL_EXECUTION')!='true':
       await jobs.fail(lease,'forbidden');return
      if kind=='knowledge.ingest':
       client=bound_session().client('bedrock-runtime',region_name='ap-southeast-1',endpoint_url=ENDPOINT,config=NOVA_SDK_CONFIG)
       try:
        control=bound_session().client('bedrock',region_name='ap-southeast-1',config=NOVA_SDK_CONFIG)
        try:
         if control.get_account_data_retention().get('mode')!='none' or control.get_model_invocation_logging_configuration().get('loggingConfig'):raise ValueError('Provider retention not enabled')
        finally:control.close()
        worker=KnowledgeIngestion(PostgresKnowledgeRepository(resources.database),resources.storage,parser,jobs,None,jobs._credential_hash)
        worker.embedding=CohereEmbedding(client,worker.observer(asyncio.get_running_loop()))
        return await worker.process(claim)
       finally:client.close()
      if kind=='agent.resume':
       if os.environ.get('EAI_FICTIONAL_APAC_ENABLED')!='true':raise ValueError('Model scope not enabled')
       async with resources.database.transaction() as c:
        route=(await (await c.execute(sql.SQL('SELECT {}.worker_route(%s)').format(sql.Identifier(resources.database.schema)),(Jsonb(dict(credential_hash=jobs._credential_hash,id=claim['id'],generation=claim['generation'])),))).fetchone())[0]
       if route.get('status')!='ok':raise ValueError()
       if route['agent_kind']=='assisted':
        turn=await build_assisted_turn(resources.database,jobs,claim,source=resources.drafts.source,model_factory=lambda before:build_workflow_model(before,fictional_apac=True))
        from langgraph.types import Command
        return await turn.run(build_assisted_graph,resume_input=Command(resume={'continue':True}) if route.get('resuming') else None)
       turn=await build_rag_turn(resources.database,jobs,claim,model_factory=lambda before:build_model_adapters(before,fictional_apac=True))
       return await turn.run(build_rag_graph)
      if kind in ('action.dispatch','action.reconcile'):
       worker=workflow_worker(settings,resources.database,key)
       try:return await worker.process(lease)
       finally:await worker.transport.close()
      raise ValueError('Unconfigured job type')
     await work()
    except Exception as error:
     if isinstance(error,ValueError) and str(error)=='Unconfigured executor':raise
     # The owning worker/lease recovery decides whether retry is safe. Never
     # complete or blindly retry a possibly committed financial/model effect.
     print('{"event":"worker_held"}',flush=True)
     try:await asyncio.wait_for(stop.wait(),timeout=2)
     except TimeoutError:pass
 finally:await parser.close()

def main():
 logging.disable(logging.CRITICAL)
 with asyncio.Runner(loop_factory=database_loop_factory) as runner:runner.run(main_loop())
if __name__=='__main__':main()
