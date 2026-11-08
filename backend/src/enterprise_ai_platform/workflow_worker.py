"""Explicit one-lease workflow worker; no daemon or automatic AWS mutation."""
import argparse
import asyncio
import json
import logging
from pathlib import Path
from uuid import UUID
from pydantic import SecretStr,Field
from pydantic_settings import BaseSettings,SettingsConfigDict
from enterprise_ai_platform.composition import workflow_worker,database_loop_factory
from enterprise_ai_platform.config import load_settings
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase,parse_target
from enterprise_ai_platform.modules.jobs.ports import Lease


class Secret(BaseSettings):
    model_config=SettingsConfigDict(extra='ignore',hide_input_in_errors=True)
    credential:SecretStr=Field(validation_alias='ACTION_EXECUTOR_CREDENTIAL',repr=False)


async def run(settings,credential,lease):
    database=RuntimeDatabase(parse_target(settings.database_url.get_secret_value()));worker=None
    try:
        await database.open();worker=workflow_worker(settings,database,credential)
        result=await worker.process(lease);print(json.dumps(dict(passed=True,state=result.get('state','unknown'))))
    finally:
        if worker is not None:await worker.transport.close()
        await database.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--env-file',type=Path,default=Path('.env'))
    parser.add_argument('--job-id',required=True);parser.add_argument('--tenant-id',required=True);parser.add_argument('--generation',required=True,type=int);args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        settings=load_settings(args.env_file,require_file=True);secret=Secret(_env_file=args.env_file,_env_file_encoding='utf-8')
        lease=Lease(UUID(args.job_id),UUID(args.tenant_id),args.generation)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:runner.run(run(settings,secret.credential.get_secret_value(),lease))
    except Exception:print('{"passed":false,"code":"workflow_unavailable"}');raise SystemExit(1) from None


if __name__=='__main__':main()
