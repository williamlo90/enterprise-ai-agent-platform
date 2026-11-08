"""Explicit one-claim ingestion worker; no daemon or credential provisioning."""
import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path
from uuid import UUID
import boto3
from pydantic import SecretStr,Field
from pydantic_settings import BaseSettings,SettingsConfigDict
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase,parse_target
from enterprise_ai_platform.infrastructure.database.knowledge import PostgresKnowledgeRepository
from enterprise_ai_platform.infrastructure.database.jobs import PostgresJobRepository
from enterprise_ai_platform.infrastructure.storage.s3 import S3Files,S3_CONFIG
from enterprise_ai_platform.infrastructure.storage.parser import DockerFileParser
from enterprise_ai_platform.integrations.models.credentials import bound_session
from enterprise_ai_platform.integrations.models.nova import CohereEmbedding,NOVA_SDK_CONFIG
from enterprise_ai_platform.integrations.models.bedrock import ENDPOINT
from enterprise_ai_platform.modules.jobs.service import JobService
from enterprise_ai_platform.modules.knowledge.service import KnowledgeIngestion

class WorkerSettings(BaseSettings):
    model_config=SettingsConfigDict(extra='ignore',frozen=True,hide_input_in_errors=True)
    database_url:SecretStr=Field(validation_alias='DATABASE_URL',repr=False)
    bucket:str=Field(validation_alias='APP_FILES_BUCKET',pattern=r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')
    image:str=Field(validation_alias='APP_FILES_PARSER_IMAGE',pattern=r'^sha256:[a-f0-9]{64}$')
    credential:SecretStr=Field(validation_alias='KNOWLEDGE_EXECUTOR_CREDENTIAL',repr=False)

async def run(settings,claim):
    database=RuntimeDatabase(parse_target(settings.database_url.get_secret_value()))
    session=bound_session()
    storage=S3Files(session.client('s3',config=S3_CONFIG,endpoint_url='https://s3.ap-southeast-1.amazonaws.com'),settings.bucket)
    parser=DockerFileParser(settings.image);client=None
    try:
        await database.open()
        control=session.client('bedrock',config=NOVA_SDK_CONFIG,endpoint_url='https://bedrock.ap-southeast-1.amazonaws.com')
        try:
            if control.get_account_data_retention().get('mode')!='none' or control.get_model_invocation_logging_configuration().get('loggingConfig'):raise ValueError()
        finally:control.close()
        credential=settings.credential.get_secret_value();jobs=JobService(PostgresJobRepository(database),credential)
        worker=KnowledgeIngestion(PostgresKnowledgeRepository(database),storage,parser,jobs,None,hashlib.sha256(('eai:job-caller:v1:'+credential).encode()).hexdigest())
        client=session.client('bedrock-runtime',config=NOVA_SDK_CONFIG,endpoint_url=ENDPOINT)
        worker.embedding=CohereEmbedding(client,worker.observer(asyncio.get_running_loop()))
        result=await worker.process(claim)
        print(json.dumps(dict(passed=True,index_state=result['index_state'],publication_state=result['publication_state'])))
    finally:
        if client is not None:client.close()
        await parser.close();await storage.close();await database.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--env-file',type=Path,default=Path('.env'))
    parser.add_argument('--job-id',required=True);parser.add_argument('--tenant-id',required=True);parser.add_argument('--generation',type=int,required=True);args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        if not args.env_file.is_file():raise ValueError()
        settings=WorkerSettings(_env_file=args.env_file,_env_file_encoding='utf-8')
        claim=dict(id=str(UUID(args.job_id)),tenant_id=str(UUID(args.tenant_id)),generation=args.generation,job_type='knowledge.ingest')
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:runner.run(run(settings,claim))
    except Exception:
        print('{"passed":false,"code":"knowledge_worker_unavailable"}');raise SystemExit(1) from None

if __name__=='__main__':main()
