"""Authenticate the recipient service separately from the delegated actor."""

import asyncio
import hashlib
import hmac
import json
import re
import time
from enterprise_ai_platform.modules.delegation.domain import request_hash, validate_claims, HUMAN_ACTIONS
from enterprise_ai_platform.modules.delegation.domain import VerificationReference,VerifiedActor
from enterprise_ai_platform.modules.delegation.domain import catalog_hash,validate_catalog_claims,catalog_binding,CATALOG_ACTIONS
from enterprise_ai_platform.modules.delegation.ports import DelegationFailure


class DelegationService:
    def __init__(self,repository,signatures,issuer,*,receipts=None,clock=time.time):
        self.repository,self.signatures,self.issuer,self.receipts,self.clock=repository,signatures,issuer,receipts,clock

    async def _call(self,operation,payload):
        result=await self.repository.call(operation,payload)
        if result.get("status")!="ok":
            raise DelegationFailure({"unauthenticated":401,"forbidden":403,"conflict":409,"invalid":400}.get(result.get("status"),503))
        return result

    async def verify(self,credential,delegation,payload):
        try:
            async with asyncio.timeout(8):
                if (type(payload) is dict and type(payload.get('schema_version')) is int and payload.get('schema_version')==2
                        and payload.get('action') in CATALOG_ACTIONS):
                    return await self._verify_catalog(credential,delegation,payload)
                return await self._verify(credential,delegation,payload)
        except TimeoutError:raise DelegationFailure(503) from None

    async def verify_private(self,credential,delegation,payload):
        if type(payload) is not dict or payload.get('action') not in HUMAN_ACTIONS or type(payload.get('schema_version')) is not int or payload['schema_version']!=1:raise DelegationFailure(400)
        try:
            async with asyncio.timeout(8):return await self._verify(credential,delegation,payload,private=True)
        except TimeoutError:raise DelegationFailure(503) from None

    async def _verify(self,credential,delegation,payload,*,private=False):
        if not isinstance(credential,str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}",credential):raise DelegationFailure(401)
        credential_hash=hashlib.sha256(("eai:internal-caller:v1:"+credential).encode()).hexdigest()
        caller=await self._call("caller",{"credential_hash":credential_hash})
        try:
            header=self.signatures.header(delegation)
            key=await self._call("key",{"credential_hash":credential_hash,"issuer":self.issuer,"kid":header["kid"]})
            claims=self.signatures.verify(delegation,key["jwk"])
            validate_claims(claims,issuer=self.issuer,audience=caller["audience"],caller=caller["delegation_caller"],now=int(self.clock()))
        except DelegationFailure:raise
        except Exception:raise DelegationFailure(401) from None
        try:
            digest=request_hash(payload)
            binding={"sub":payload["actor_id"],"tenant_id":payload["tenant_id"],"action":payload["action"],
                "operation_id":payload["operation_id"],"object_kind":payload["object"]["kind"],
                "object_id":payload["object"]["id"],"object_version":payload["object"]["version"]}
        except Exception:raise DelegationFailure(400) from None
        if not hmac.compare_digest(digest,claims["request_hash"]) or any(claims[k]!=v for k,v in binding.items()):
            raise DelegationFailure(403)
        if claims["action"] in HUMAN_ACTIONS:
            if self.receipts is None:raise DelegationFailure(503)
            try:
                receipt=await self.receipts.get(claims["human_receipt_id"])
                valid=receipt is not None and receipt.matches(claims)
            except Exception:raise DelegationFailure(503) from None
            if not valid:raise DelegationFailure(403)
        binding_hash=hashlib.sha256(json.dumps({"claims":claims,"verifying_caller":caller["caller_ref"]},sort_keys=True,separators=(",",":")).encode()).hexdigest()
        if claims["action"] in HUMAN_ACTIONS:
            try:fresh=await self.receipts.get(claims["human_receipt_id"])
            except Exception:raise DelegationFailure(503) from None
            if fresh != receipt:raise DelegationFailure(403)
        # The authoritative DB check follows the last receipt dependency await.
        result=await self._call("verify",{"credential_hash":credential_hash,"claims":claims,
            "key_hash":key["key_hash"],"key_kid":header["kid"],"binding_hash":binding_hash})
        try:
            validate_claims(claims,issuer=self.issuer,audience=caller["audience"],caller=caller["delegation_caller"],now=int(self.clock()))
        except (ValueError,TypeError):raise DelegationFailure(401) from None
        if self.clock()>=result["authority_expires_at"]:raise DelegationFailure(401)
        actor={"allow":True,"scope":"actor_only","actor_id":claims["sub"],"tenant_id":claims["tenant_id"],"membership_id":claims["membership_id"],
            "access_revision":result["access_revision"],"action":claims["action"],"operation_id":claims["operation_id"],
            "request_hash":digest,"verified_at":result["verified_at"],"expires_at":min(claims["exp"],int(result["authority_expires_at"])),"replayed":result["replayed"]}
        if private:return VerifiedActor(actor,VerificationReference(self.issuer,claims['jti'],binding_hash,credential_hash,header['kid'],key['key_hash']))
        return actor

    async def _verify_catalog(self,credential,delegation,payload):
        if not isinstance(credential,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',credential):raise DelegationFailure(401)
        credential_hash=hashlib.sha256(('eai:internal-caller:v1:'+credential).encode()).hexdigest()
        caller=await self._call('caller',{'credential_hash':credential_hash})
        try:
            header=self.signatures.header(delegation)
            key=await self._call('key',{'credential_hash':credential_hash,'issuer':self.issuer,'kid':header['kid']})
            claims=self.signatures.verify(delegation,key['jwk'])
            validate_catalog_claims(claims,issuer=self.issuer,audience=caller['audience'],caller=caller['delegation_caller'],now=int(self.clock()))
        except DelegationFailure:raise
        except Exception:raise DelegationFailure(401) from None
        try:
            digest=catalog_hash(payload);binding=catalog_binding(payload)
            expected={'sub':payload['actor_id'],'tenant_id':payload['tenant_id'],'action':payload['action'],
                'operation_id':payload['operation_id'],'object_kind':payload['object']['kind'],
                'object_id':payload['object']['id'],'object_version':payload['object']['version']}
        except Exception:raise DelegationFailure(400) from None
        if not hmac.compare_digest(digest,claims['request_hash']) or any(claims[k]!=v for k,v in expected.items()):raise DelegationFailure(403)
        binding_hash=hashlib.sha256(json.dumps({'claims':claims,'verifying_caller':caller['caller_ref']},sort_keys=True,separators=(',',':')).encode()).hexdigest()
        result=await self._call('verify',{'credential_hash':credential_hash,'claims':claims,'key_hash':key['key_hash'],
                                        'key_kid':header['kid'],'binding_hash':binding_hash})
        try:validate_catalog_claims(claims,issuer=self.issuer,audience=caller['audience'],caller=caller['delegation_caller'],now=int(self.clock()))
        except (ValueError,TypeError):raise DelegationFailure(401) from None
        if self.clock()>=result['authority_expires_at']:raise DelegationFailure(401)
        return dict(allow=True,scope='catalog',canonical_version=2,actor_id=claims['sub'],tenant_id=claims['tenant_id'],
            membership_id=claims['membership_id'],access_revision=result['access_revision'],action=claims['action'],
            operation_id=claims['operation_id'],request_hash=digest,verified_at=result['verified_at'],
            expires_at=min(claims['exp'],int(result['authority_expires_at'])),replayed=result['replayed'],binding=binding)
