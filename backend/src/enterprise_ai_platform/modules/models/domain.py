"""Bounded model results. Prompt, completion and vectors stay out of repr/logs."""
from dataclasses import dataclass,field
from enum import Enum
import math
import re

CHAT_MODEL='anthropic.claude-sonnet-5'
EMBED_MODEL='cohere.embed-multilingual-v3'
REGION='ap-southeast-1'
DIMENSIONS=1024
MAX_INPUT_TOKENS=4096
MAX_OUTPUT_TOKENS=256
TOOL_NAME='lookup_demo_policy'
TOOL_SCHEMA={'type':'object','properties':{'topic':{'type':'string','enum':['travel']},'language':{'type':'string','enum':['id','en']}},'required':['topic','language'],'additionalProperties':False}


class ModelFailure(Exception):
    def __init__(self,code,*,operation=None,provider_code=None):
        safe={'invalid_input','invalid_output','rate_limited','access_denied','provider_unavailable','timeout','budget_exhausted','deadline','preflight_failed','usage_unavailable'}
        self.code=code if code in safe else 'provider_unavailable'
        self.operation=operation if operation in {'CountTokens','Converse','InvokeModel'} else None
        allowed={'ValidationException','AccessDeniedException','ThrottlingException','ModelTimeoutException','ModelNotReadyException','ServiceUnavailableException','InternalServerException','ResourceNotFoundException'}
        self.provider_code=provider_code if provider_code in allowed else None
        super().__init__(self.code)


def count(value,maximum):
    if type(value) is not int or not 0<=value<=maximum:raise ModelFailure('invalid_output')
    return value


@dataclass(frozen=True)
class Usage:
    input_tokens:int
    output_tokens:int


@dataclass(frozen=True)
class ToolCall:
    id:str=field(repr=False)
    language:str


@dataclass(frozen=True)
class ChatResult:
    text:str=field(repr=False)
    tool:ToolCall|None
    usage:Usage
    message:dict=field(repr=False)


@dataclass(frozen=True)
class EmbeddingResult:
    vectors:tuple[tuple[float,...],...]=field(repr=False)
    usage:Usage|None


def tool_call(value):
    if not isinstance(value,dict) or set(value)!={'toolUseId','name','input'}:raise ModelFailure('invalid_output')
    if value['name']!=TOOL_NAME or not isinstance(value['toolUseId'],str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',value['toolUseId']):raise ModelFailure('invalid_output')
    args=value['input']
    if not isinstance(args,dict) or set(args)!={'topic','language'} or args['topic']!='travel' or args['language'] not in ('id','en'):raise ModelFailure('invalid_output')
    return ToolCall(value['toolUseId'],args['language'])


def vectors(value,expected_count):
    if not isinstance(value,list) or len(value)!=expected_count:raise ModelFailure('invalid_output')
    result=[]
    for row in value:
        if not isinstance(row,list) or len(row)!=DIMENSIONS:raise ModelFailure('invalid_output')
        if any(type(x) not in (int,float) or abs(x)>1e6 or not math.isfinite(x) for x in row):raise ModelFailure('invalid_output')
        if sum(x*x for x in row)<=1e-20:raise ModelFailure('invalid_output')
        result.append(tuple(float(x) for x in row))
    return tuple(result)


@dataclass(frozen=True)
class PolicyResult:
    call_id: str = field(repr=False)
    deadline_days: int
    source: str = field(repr=False)
    policy: str = field(repr=False)


@dataclass(frozen=True)
class Message:
    role: str
    text: str = field(default='', repr=False)
    tool: ToolCall | None = field(default=None, repr=False)
    result: PolicyResult | None = field(default=None, repr=False)
    response_contract: 'ResponseContract | None' = field(default=None, repr=False)


class ResponseContract(Enum):
    EVIDENCE_SELECTION = 'rag-extractive-v3'


@dataclass(frozen=True)
class ChatReply:
    text: str = field(repr=False)
    tool: ToolCall | None
    usage: Usage
    message: Message = field(repr=False)
