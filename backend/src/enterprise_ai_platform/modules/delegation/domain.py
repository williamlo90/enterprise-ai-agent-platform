"""Versioned canonical requests. No floats, lossy reason normalization or free authority fields."""

from dataclasses import dataclass
from types import MappingProxyType
import hashlib
import json
import re
from uuid import UUID

HUMAN_ACTIONS = frozenset({"claim.submit", "claim.information", "claim.decide"})
OBJECTS = {"trip.read":"trip", "claim.read":"claim", "operation.read":"operation",
           "claim.submit":"draft", "claim.information":"claim", "claim.decide":"claim"}
CLAIM_FIELDS = frozenset({"iss","aud","caller","sub","tenant_id","membership_id","access_revision","action",
    "object_kind","object_id","object_version","request_hash","operation_id","jti","iat","nbf","exp","human_receipt_id","session_id"})


@dataclass(frozen=True,repr=False)
class VerificationReference:
    issuer:str
    jti:str
    binding_hash:str
    credential_hash:str
    key_kid:str
    key_hash:str

    def wire(self):return dict(issuer=self.issuer,jti=self.jti,binding_hash=self.binding_hash,credential_hash=self.credential_hash,key_kid=self.key_kid,key_hash=self.key_hash)


@dataclass(frozen=True,repr=False)
class VerifiedActor:
    actor:dict
    reference:VerificationReference

    def __post_init__(self):object.__setattr__(self,'actor',MappingProxyType(dict(self.actor)))


def exact(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError("Invalid contract object.")


def identifier(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError("Invalid contract identifier.")
    return value


def number(value, maximum=9223372036854775807):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("Invalid contract integer.")
    return value


def text(value, maximum=4096):
    if type(value) is not str or len(value.encode("utf-8")) > maximum or "\x00" in value:
        raise ValueError("Invalid contract string.")
    return value


def hash_value(value):
    if type(value) is not str or not re.fullmatch("[a-f0-9]{64}", value):
        raise ValueError("Invalid contract digest.")
    return value


def unique_ids(values, maximum):
    if type(values) is not list or len(values)>maximum:
        raise ValueError("Invalid contract list.")
    result=[identifier(v) for v in values]
    if len(result)!=len(set(result)):
        raise ValueError("Duplicate contract identifier.")
    return sorted(result)


def financial(data, *, information=False):
    fields={"trip_id","currency","items","total_minor","receipts","policy","approval_route","destination"}
    if information:fields |= {"revision","draft"}
    exact(data,fields)
    identifier(data["trip_id"])
    if data["currency"] not in {"IDR","SGD","USD"} or data["destination"]!="reference-reimbursement":
        raise ValueError("Invalid contract destination or currency.")
    if type(data["items"]) is not list or not 1<=len(data["items"])<=100:
        raise ValueError("Invalid item count.")
    items=[]
    for item in data["items"]:
        exact(item,{"id","trip_item_id","amount_minor","receipt_ids"})
        items.append({"id":identifier(item["id"]),"trip_item_id":identifier(item["trip_item_id"]),
            "amount_minor":number(item["amount_minor"]),"receipt_ids":unique_ids(item["receipt_ids"],10)})
    if len({v["id"] for v in items})!=len(items) or sum(v["amount_minor"] for v in items)!=number(data["total_minor"]):
        raise ValueError("Invalid item identity or total.")
    if type(data["receipts"]) is not list or len(data["receipts"])>10:
        raise ValueError("Invalid receipt count.")
    receipts=[]
    for receipt in data["receipts"]:
        exact(receipt,{"id","version","digest"})
        receipts.append({"id":identifier(receipt["id"]),"version":number(receipt["version"]),"digest":hash_value(receipt["digest"])})
    if (len({r["id"] for r in receipts})!=len(receipts)
            or {r["id"] for r in receipts}!={r for item in items for r in item["receipt_ids"]}):
        raise ValueError("Invalid receipt binding.")
    exact(data["policy"],{"id","version","digest"})
    identifier(data["policy"]["id"]);number(data["policy"]["version"]);hash_value(data["policy"]["digest"])
    route=data["approval_route"]
    if type(route) is not list or not 1<=len(route)<=10:
        raise ValueError("Invalid route.")
    for stage, entry in enumerate(route,1):
        exact(entry,{"stage","approver_id","scope_ref"})
        if type(entry["stage"]) is not int or entry["stage"]!=stage or entry["scope_ref"]!="reimbursement":
            raise ValueError("Invalid route stage.")
        identifier(entry["approver_id"])
    if information:
        number(data["revision"])
        exact(data["draft"],{"id","version"});identifier(data["draft"]["id"]);number(data["draft"]["version"])
    return {**data,"items":sorted(items,key=lambda i:i["id"]),"receipts":sorted(receipts,key=lambda r:r["id"])}


def canonical_payload(payload):
    exact(payload,{"schema_version","action","tenant_id","actor_id","operation_id","object","data"})
    if type(payload["schema_version"]) is not int or payload["schema_version"]!=1 or payload["action"] not in OBJECTS:
        raise ValueError("Unknown canonical contract.")
    for key in ("tenant_id","actor_id","operation_id"):identifier(payload[key])
    obj=payload["object"];exact(obj,{"kind","id","version"})
    if obj["kind"]!=OBJECTS[payload["action"]]:raise ValueError("Invalid object kind.")
    identifier(obj["id"]);number(obj["version"])
    data=payload["data"]
    if payload["action"] in {"claim.submit","claim.information"}:
        data=financial(data,information=payload["action"]=="claim.information")
    elif payload["action"]=="claim.decide":
        exact(data,{"revision","stage","decision","reason"})
        number(data["revision"]);number(data["stage"],10);text(data["reason"])
        if data["decision"] not in {"approve","reject","request_information"} or (data["decision"]!="approve" and not data["reason"].strip()):
            raise ValueError("Invalid decision.")
    else:exact(data,set())
    # Copy by serialization: no caller-owned mutable list/dict survives validation.
    encoded=json.dumps({**payload,"data":data},sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode("utf-8")
    if len(encoded)>65536:raise ValueError("Canonical request too large.")
    return encoded


def request_hash(payload):
    return hashlib.sha256(b"eai:human-action:v1:"+canonical_payload(payload)).hexdigest()


CATALOG_ACTIONS = frozenset({'trips.list','policy.publish','workflow.read'})


def canonical_catalog(payload):
    exact(payload,{'schema_version','action','tenant_id','actor_id','operation_id','object','data'})
    if type(payload['schema_version']) is not int or payload['schema_version']!=2 or payload['action'] not in CATALOG_ACTIONS:
        raise ValueError('Unknown catalog contract.')
    for key in ('tenant_id','actor_id','operation_id'):identifier(payload[key])
    obj=payload['object'];exact(obj,{'kind','id','version'});number(obj['version'])
    data=payload['data']
    if payload['action']=='workflow.read':
        if obj['kind'] not in {'employee_workspace','approval_workspace'} or obj['version']!=1:raise ValueError('Invalid workspace authority.')
        if obj['id'] is not None:identifier(obj['id'])
        exact(data,{'after','limit','request_digest'})
        if data['after'] is not None:identifier(data['after'])
        number(data['limit'],20)
        if obj['id'] is not None and (data['after'] is not None or data['limit']!=1):raise ValueError('Invalid detail query.')
        view='mine' if obj['kind']=='employee_workspace' else 'approvals'
        expected=hashlib.sha256(b'reimbursement:workspace:v1:'+json.dumps(dict(view=view,claim_id=obj['id'],after=data['after'],limit=data['limit']),sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if hash_value(data['request_digest'])!=expected:raise ValueError('Invalid workspace digest.')
    elif payload['action']=='trips.list':
        if obj!={'kind':'trip_collection','id':None,'version':1} or type(obj['version']) is not int:raise ValueError('Invalid collection binding.')
        exact(data,{'after','limit','request_digest'})
        if data['after'] is not None:identifier(data['after'])
        number(data['limit'],50)
        expected=hashlib.sha256(b'reimbursement:trip-list:v1:'+json.dumps(
            {'after':data['after'],'limit':data['limit']},sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if hash_value(data['request_digest'])!=expected:raise ValueError('Invalid list digest.')
    else:
        if obj['kind']!='policy_package':raise ValueError('Invalid publisher object.')
        identifier(obj['id']);exact(data,{'key','request_digest'});identifier(data['key']);hash_value(data['request_digest'])
    raw=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    if len(raw)>2048:raise ValueError('Catalog payload exceeds transport guard.')
    return raw


def catalog_hash(payload):
    return hashlib.sha256(b'eai:catalog-action:v2:'+canonical_catalog(payload)).hexdigest()


def validate_catalog_claims(claims,*,issuer,audience,caller,now):
    exact(claims,CLAIM_FIELDS|{'canonical_version'})
    if (type(claims['canonical_version']) is not int or claims['canonical_version']!=2
            or claims['iss']!=issuer or claims['aud']!=audience or claims['caller']!=caller
            or claims['action'] not in CATALOG_ACTIONS or claims['human_receipt_id'] is not None):raise ValueError('Invalid catalog delegation.')
    for key in ('sub','tenant_id','membership_id','session_id','operation_id','jti'):identifier(claims[key])
    number(claims['access_revision']);number(claims['object_version']);hash_value(claims['request_hash'])
    if claims['action']=='workflow.read':
        if claims['object_kind'] not in {'employee_workspace','approval_workspace'} or claims['object_version']!=1:raise ValueError('Invalid workspace authority.')
        if claims['object_id'] is not None:identifier(claims['object_id'])
    elif claims['action']=='trips.list':
        if claims['object_kind']!='trip_collection' or claims['object_id'] is not None or claims['object_version']!=1:raise ValueError('Invalid list authority.')
    elif claims['object_kind']!='policy_package':raise ValueError('Invalid publisher authority.')
    else:identifier(claims['object_id'])
    if (any(type(claims[k]) is not int for k in ('iat','nbf','exp')) or type(now) is not int
            or not 0<claims['iat']==claims['nbf']<claims['exp']<=claims['iat']+60
            or now>=claims['exp'] or claims['iat']>now+5):raise ValueError('Catalog authority expired.')


def catalog_binding(payload):
    canonical_catalog(payload)
    return dict(action=payload['action'],object_id=payload['object']['id'],object_version=payload['object']['version'],
                request_digest=payload['data']['request_digest'],operation_id=payload['operation_id'],key=payload['data'].get('key'))


def validate_claims(claims, *, issuer, audience, caller, now):
    exact(claims,CLAIM_FIELDS)
    if (claims["iss"]!=issuer or claims["aud"]!=audience or claims["caller"]!=caller
            or claims["action"] not in OBJECTS or claims["object_kind"]!=OBJECTS[claims["action"]]):
        raise ValueError("Delegation binding rejected.")
    for key in ("sub","tenant_id","membership_id","object_id","operation_id","jti","session_id"):identifier(claims[key])
    number(claims["access_revision"]);number(claims["object_version"]);hash_value(claims["request_hash"])
    if (any(type(claims[k]) is not int for k in ("iat","nbf","exp")) or type(now) is not int
            or not 0<claims["iat"]==claims["nbf"]<claims["exp"]<=claims["iat"]+60
            or now>=claims["exp"] or claims["iat"]>now+5):
        raise ValueError("Delegation lifetime rejected.")
    if claims["action"] in HUMAN_ACTIONS:identifier(claims["human_receipt_id"])
    elif claims["human_receipt_id"] is not None:raise ValueError("Unexpected human receipt.")


@dataclass(frozen=True, repr=False)
class HumanReceipt:
    id: str
    actor_id: str
    session_id: str
    tenant_id: str
    operation_id: str
    action: str
    object_kind: str
    object_id: str
    object_version: int
    request_hash: str
    human_action_time: int
    revoked: bool = False

    def __post_init__(self):
        for value in (self.id,self.actor_id,self.session_id,self.tenant_id,self.operation_id,self.object_id):
            identifier(value)
        if self.action not in HUMAN_ACTIONS or self.object_kind!=OBJECTS[self.action] or type(self.revoked) is not bool:
            raise ValueError("Invalid human receipt contract.")
        number(self.object_version);number(self.human_action_time);hash_value(self.request_hash)

    def matches(self, claims):
        return (type(self.revoked) is bool and not self.revoked and type(self.human_action_time) is int
            and 0<self.human_action_time<=claims["iat"] and all(getattr(self,a)==claims[b] for a,b in (
                ("id","human_receipt_id"),("session_id","session_id"),("actor_id","sub"),("tenant_id","tenant_id"),("operation_id","operation_id"),
                ("action","action"),("object_kind","object_kind"),("object_id","object_id"),
                ("object_version","object_version"),("request_hash","request_hash"))))
