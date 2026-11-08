"""Explicit single-claim file worker; no automatic credential provisioning or daemon."""
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
from enterprise_ai_platform.infrastructure.database.files import PostgresFileRepository
from enterprise_ai_platform.infrastructure.database.jobs import PostgresJobRepository
from enterprise_ai_platform.infrastructure.storage.s3 import S3Files,S3_CONFIG
from enterprise_ai_platform.infrastructure.storage.parser import DockerFileParser
from enterprise_ai_platform.modules.files.service import FileInspection
from enterprise_ai_platform.modules.jobs.service import JobService


class WorkerSettings(BaseSettings):
    model_config=SettingsConfigDict(extra='ignore',frozen=True,hide_input_in_errors=True)
    database_url:SecretStr=Field(validation_alias='DATABASE_URL',repr=False)
    bucket:str=Field(validation_alias='APP_FILES_BUCKET',pattern=r'^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')
    image:str=Field(validation_alias='APP_FILES_PARSER_IMAGE',pattern=r'^sha256:[a-f0-9]{64}$')
    credential:SecretStr=Field(validation_alias='FILE_EXECUTOR_CREDENTIAL',repr=False)


async def run(settings,claim):
    database=RuntimeDatabase(parse_target(settings.database_url.get_secret_value()))
    storage=S3Files(boto3.Session(region_name='ap-southeast-1').client('s3',config=S3_CONFIG,endpoint_url='https://s3.ap-southeast-1.amazonaws.com'),settings.bucket)
    parser=DockerFileParser(settings.image)
    try:
        await database.open()
        credential=settings.credential.get_secret_value();jobs=JobService(PostgresJobRepository(database),credential)
        worker=FileInspection(PostgresFileRepository(database),storage,parser,jobs,hashlib.sha256(('eai:job-caller:v1:'+credential).encode()).hexdigest())
        result=await worker.process(claim)
        print(json.dumps(dict(passed=True,state=result.get('scan_state',result.get('state')))))
    finally:
        await parser.close();await storage.close();await database.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--env-file',type=Path,default=Path('.env'))
    parser.add_argument('--job-id',required=True);parser.add_argument('--tenant-id',required=True);parser.add_argument('--generation',type=int,required=True);args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        if not args.env_file.is_file():raise ValueError()
        settings=WorkerSettings(_env_file=args.env_file,_env_file_encoding='utf-8')
        claim=dict(id=str(UUID(args.job_id)),tenant_id=str(UUID(args.tenant_id)),generation=args.generation,job_type='file.inspect')
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:runner.run(run(settings,claim))
    except Exception:
        print('{"passed":false,"code":"file_worker_unavailable"}');raise SystemExit(1) from None


if __name__=='__main__':main()
