"""Operator-run Singapore account retention change; no IAM/model activation.

The installed AWS CLI lacks PutAccountDataRetention. The locked SDK supports it.
Without --apply this helper is read-only. Lead never executes --apply.
"""
import argparse
import json
from pathlib import Path
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION='ap-southeast-1'


def configure(setup,control,identity,*,apply=False):
    if setup.get('region')!=REGION or identity.get_caller_identity()['Account']!=setup.get('account'):
        raise ValueError('account_or_region_mismatch')
    current=control.get_account_data_retention().get('mode')
    if current not in ('inherit','none'):raise ValueError('existing_policy_requires_review')
    result=dict(region=REGION,scope='account_wide_in_this_region',previous_mode=current,desired_mode='none',applied=False)
    if apply and current!='none':
        response=control.put_account_data_retention(mode='none')
        if response.get('mode')!='none':raise ValueError('unexpected_result_do_not_retry')
        result['applied']=True
    if apply:
        result['verified']=control.get_account_data_retention().get('mode')=='none'
        if not result['verified']:raise ValueError('verification_failed_do_not_retry')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    try:
        root=Path(__file__).resolve().parents[3];setup=json.loads((root/'local/spike02/setup.json').read_text(encoding='utf-8'))
        session=boto3.Session(region_name=REGION);config=Config(connect_timeout=3,read_timeout=5,retries={'total_max_attempts':1})
        control=session.client('bedrock',endpoint_url='https://bedrock.ap-southeast-1.amazonaws.com',config=config)
        result=configure(setup,control,session.client('sts',config=config),apply=args.apply)
        print(json.dumps(result))
    except Exception as error:
        result=dict(passed=False,code='sanitized_setup_failure',do_not_retry_automatically=True)
        reasons={'account_or_region_mismatch','existing_policy_requires_review','unexpected_result_do_not_retry','verification_failed_do_not_retry'}
        if isinstance(error,ValueError) and str(error) in reasons:result['code']=str(error)
        if isinstance(error,ClientError):
            safe={'AccessDeniedException','ThrottlingException','ValidationException','InternalServerException'}
            code=error.response.get('Error',{}).get('Code')
            result['provider_code']=code if code in safe else 'unknown'
            operation=getattr(error,'operation_name','unknown')
            result['operation']=operation if operation in {'GetCallerIdentity','GetAccountDataRetention','PutAccountDataRetention'} else 'unknown'
        print(json.dumps(result))
        raise SystemExit(1) from None


if __name__=='__main__':main()
