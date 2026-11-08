import asyncio
import copy
import time
from uuid import uuid4
import pytest
from enterprise_ai_platform.modules.administration.domain import mutation,config,page
from enterprise_ai_platform.modules.administration.service import AdministrationService
from enterprise_ai_platform.modules.authorization.ports import AccessFailure
from enterprise_ai_platform.config import Settings

def values():return dict(assistant_name='Assistant',locales=['id','en'],help_contact='Hubungi HR',instruction_text='Gunakan sumber yang diizinkan.',connector_ref='reference-reimbursement',model_policy_ref='nova-pro-apac-fictional')

@pytest.mark.parametrize('key',['credential','api_key','provider_url','retention_seconds','region','tools','tenant_id','role'])
def test_config_allowlist_excludes_security_identity_and_credentials(key):
    value=values();value[key]='untrusted'
    with pytest.raises(ValueError):config(value)

@pytest.mark.parametrize('ref',['https://attacker.test','../service','s3://bucket','NOVA','a'*129,''])
def test_connector_ref_cannot_be_arbitrary_address(ref):
    value=values();value['connector_ref']=ref
    with pytest.raises(ValueError):config(value)

@pytest.mark.parametrize('n',[True,0,-1,1.2,'1',10**18])
def test_mutation_versions_are_exact_integers(n):
    with pytest.raises(ValueError):mutation('config_write',dict(expected_version=n,key=str(uuid4()),config=values()))

def test_existing_config_character_limits_and_multiline_remain_valid():
    value=values();value.update(assistant_name='界'*200,help_contact='界'*500,instruction_text='😀'*15997+'\n\t ')
    config(value)
    value['instruction_text']+='x'
    with pytest.raises(ValueError):config(value)

@pytest.mark.parametrize('role',['operator_scoped','superadmin','owner','*'])
def test_browser_cannot_grant_operator_or_unknown_role(role):
    with pytest.raises(ValueError):mutation('role',dict(id=str(uuid4()),expected_version=1,key=str(uuid4()),operation='grant',role=role,scope_kind='tenant',scope_ref=str(uuid4())))

@pytest.mark.parametrize('active',['false',0,1,None])
def test_membership_active_requires_boolean(active):
    with pytest.raises(ValueError):mutation('member_active',dict(id=str(uuid4()),expected_version=1,key=str(uuid4()),active=active))

@pytest.mark.parametrize('extra',['issuer','subject','email','password','roles'])
def test_member_admission_does_not_create_provider_identity(extra):
    value=dict(principal_id=str(uuid4()),key=str(uuid4()));value[extra]='untrusted'
    with pytest.raises(ValueError):mutation('member_create',value)

def test_policy_cursor_preserves_exact_package_version():
    page(str(uuid4())+':2',20,policy=True)
    with pytest.raises(ValueError):page(str(uuid4())+':02',20,policy=True)
    with pytest.raises(ValueError):page(str(uuid4()),21)

class Repo:
    calls=0
    async def call(self,*a):self.calls+=1;return dict(authority_expires_at=time.time()+60,config_version=2)

def test_write_requires_csrf_and_exact_origin_before_repository():
    async def exercise():
        repo=Repo();service=AdministrationService(repo,'https://app.test')
        with pytest.raises(AccessFailure):await service.mutate('x'*43,{},'config_write',dict(expected_version=1,key=str(uuid4()),config=values()))
        assert repo.calls==0
    asyncio.run(exercise())

def test_administration_is_disabled_by_default_and_requires_identity_and_G13():
    assert Settings(env='test',host='127.0.0.1',port=8000).administration_enabled is False
    with pytest.raises(ValueError):Settings(env='test',host='127.0.0.1',port=8000,administration_enabled=True)
