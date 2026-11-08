"""Resolve a credential snapshot once for a bounded execution scope."""
import os
import boto3
from enterprise_ai_platform.modules.models.domain import ModelFailure,REGION

def bound_session():
    if os.environ.get('AWS_BEARER_TOKEN_BEDROCK'):raise ModelFailure('preflight_failed')
    credential=boto3.Session(region_name=REGION).get_credentials()
    if credential is None:raise ModelFailure('preflight_failed')
    frozen=credential.get_frozen_credentials()
    return boto3.Session(region_name=REGION,aws_access_key_id=frozen.access_key,aws_secret_access_key=frozen.secret_key,aws_session_token=frozen.token)
