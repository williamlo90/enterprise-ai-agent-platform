"""Singapore direct-model adapter. No implicit retry, fallback or tool execution."""
from contextlib import closing
import json
import re
from botocore.config import Config
from botocore.exceptions import ClientError,ReadTimeoutError,ConnectTimeoutError
from enterprise_ai_platform.modules.models.domain import CHAT_MODEL,EMBED_MODEL,REGION,MAX_INPUT_TOKENS,MAX_OUTPUT_TOKENS,TOOL_NAME,TOOL_SCHEMA,ModelFailure,Usage,ChatResult,EmbeddingResult,count,tool_call,vectors

SDK_CONFIG=Config(connect_timeout=3,read_timeout=20,retries={'total_max_attempts':1})
ENDPOINT='https://bedrock-runtime.ap-southeast-1.amazonaws.com'
SYSTEM='Use only the fictional policy supplied by the caller or the read-only demo tool. Follow the requested language. Never invent a policy or execute another tool.'
MAX_RESPONSE_BYTES=262144
MAX_CHAT_REQUEST_BYTES=3072


def strict_json(raw):
    def pairs(rows):
        result={}
        for key,value in rows:
            if key in result:raise ModelFailure('invalid_output')
            result[key]=value
        return result
    def bad_constant(_):raise ModelFailure('invalid_output')
    try:return json.loads(raw,object_pairs_hook=pairs,parse_constant=bad_constant)
    except ModelFailure:raise
    except Exception:raise ModelFailure('invalid_output') from None


def sdk_call(function,**kwargs):
    operation={'converse':'Converse','invoke_model':'InvokeModel','count_tokens':'CountTokens'}.get(getattr(function,'__name__',''))
    try:
        response=function(**kwargs)
        metadata=response.get('ResponseMetadata',{})
        if metadata.get('RetryAttempts',0)!=0:raise ModelFailure('provider_unavailable')
        if metadata.get('HTTPStatusCode',200)!=200:raise ModelFailure('provider_unavailable')
        return response
    except ModelFailure:raise
    except (ReadTimeoutError,ConnectTimeoutError):raise ModelFailure('timeout',operation=operation) from None
    except ClientError as error:
        code=error.response.get('Error',{}).get('Code')
        mapped={'ThrottlingException':'rate_limited','AccessDeniedException':'access_denied','ValidationException':'invalid_input','ModelTimeoutException':'timeout'}
        raise ModelFailure(mapped.get(code,'provider_unavailable'),operation=operation,provider_code=code) from None
    except Exception:raise ModelFailure('provider_unavailable') from None


class BedrockModels:
    """The paid-attempt observer reserves durably before every inference call."""
    def __init__(self,client,before_call):
        self.client,self.before_call=client,before_call
        self.last_usage=None

    def chat_request(self,messages,tools):
        request=dict(modelId=CHAT_MODEL,messages=messages,system=[{'text':SYSTEM}],inferenceConfig={'maxTokens':MAX_OUTPUT_TOKENS},additionalModelRequestFields={'thinking':{'type':'disabled'}})
        if tools:request['toolConfig']={'tools':[{'toolSpec':{'name':TOOL_NAME,'description':'Read a fictional travel reimbursement policy. No business transactions.','inputSchema':{'json':TOOL_SCHEMA}}}],'toolChoice':{'auto':{}}}
        return request

    def chat(self,messages,*,tools=False):
        try:return self._chat(messages,tools=tools)
        except ModelFailure:raise
        except Exception:raise ModelFailure('invalid_output') from None

    def embed(self,texts,*,input_type):
        try:return self._embed(texts,input_type=input_type)
        except ModelFailure:raise
        except (ReadTimeoutError,ConnectTimeoutError):raise ModelFailure('timeout') from None
        except Exception:raise ModelFailure('invalid_output') from None

    def _chat(self,messages,*,tools=False):
        self.last_usage=None
        if not isinstance(messages,list) or not 1<=len(messages)<=3 or type(tools) is not bool:raise ModelFailure('invalid_input')
        try:encoded=json.dumps(messages,ensure_ascii=False,allow_nan=False).encode('utf-8')
        except Exception:raise ModelFailure('invalid_input') from None
        if len(encoded)>8192:raise ModelFailure('invalid_input')
        request=self.chat_request(messages,tools)
        # Sonnet 5 does not support CountTokens on bedrock-runtime. This is
        # a strict byte guard plus a conservative cost reservation, NOT an
        # exact tokenizer or a guaranteed pre-call token ceiling. Actual usage
        # is checked after the response and kept even on later schema failure.
        serialized=json.dumps(request,ensure_ascii=False,allow_nan=False).encode('utf-8')
        if len(serialized)>MAX_CHAT_REQUEST_BYTES:raise ModelFailure('invalid_input')
        self.before_call('chat',MAX_INPUT_TOKENS)
        response=sdk_call(self.client.converse,**request)
        usage=response.get('usage',{})
        inp=count(usage.get('inputTokens'),1048576);out=count(usage.get('outputTokens'),131072)
        if count(usage.get('totalTokens'),1179648)!=inp+out:raise ModelFailure('invalid_output')
        if any(count(usage[k],0)!=0 for k in ('cacheReadInputTokens','cacheWriteInputTokens') if k in usage):raise ModelFailure('invalid_output')
        self.last_usage=Usage(inp,out)
        if inp>MAX_INPUT_TOKENS or out>MAX_OUTPUT_TOKENS:raise ModelFailure('invalid_output')
        message=response.get('output',{}).get('message',{})
        if not isinstance(message,dict) or set(message)!={'role','content'} or message.get('role')!='assistant':raise ModelFailure('invalid_output')
        content=message.get('content')
        if not isinstance(content,list) or not 1<=len(content)<=3:raise ModelFailure('invalid_output')
        text=[];calls=[]
        for block in content:
            if not isinstance(block,dict) or len(block)!=1:raise ModelFailure('invalid_output')
            if 'text' in block and isinstance(block['text'],str):text.append(block['text'])
            elif 'toolUse' in block and tools:calls.append(tool_call(block['toolUse']))
            else:raise ModelFailure('invalid_output')
        output=''.join(text)
        if len(output.encode('utf-8'))>4096 or len(calls)>1:raise ModelFailure('invalid_output')
        expected='tool_use' if calls else 'end_turn'
        if response.get('stopReason')!=expected or (not calls and not output.strip()):raise ModelFailure('invalid_output')
        return ChatResult(output,calls[0] if calls else None,self.last_usage,message)

    def _embed(self,texts,*,input_type):
        self.last_usage=None
        if not isinstance(texts,list) or not 1<=len(texts)<=4 or input_type not in ('search_document','search_query'):raise ModelFailure('invalid_input')
        if any(not isinstance(t,str) or not t.strip() or len(t)>2048 or len(t.encode('utf-8'))>8192 for t in texts):raise ModelFailure('invalid_input')
        body=json.dumps(dict(texts=texts,input_type=input_type,truncate='NONE',embedding_types=['float']),ensure_ascii=False,allow_nan=False).encode('utf-8')
        self.before_call('embedding',len(texts)*512)
        response=sdk_call(self.client.invoke_model,modelId=EMBED_MODEL,body=body,contentType='application/json',accept='application/json')
        with closing(response['body']) as stream:
            # Preserve trusted provider usage even when body reading, JSON or
            # vector validation subsequently fails. Closing covers every branch.
            token_header=response.get('ResponseMetadata',{}).get('HTTPHeaders',{}).get('x-amzn-bedrock-input-token-count')
            if token_header is not None:
                if not isinstance(token_header,str) or not re.fullmatch('[0-9]{1,7}',token_header):raise ModelFailure('invalid_output')
                self.last_usage=Usage(count(int(token_header),1048576),0)
                if self.last_usage.input_tokens>len(texts)*512:raise ModelFailure('invalid_output')
            raw=stream.read(MAX_RESPONSE_BYTES+1)
            if len(raw)>MAX_RESPONSE_BYTES:raise ModelFailure('invalid_output')
            value=strict_json(raw)
            if not isinstance(value,dict):raise ModelFailure('invalid_output')
            if 'texts' in value and value['texts']!=texts:raise ModelFailure('invalid_output')
            data=value.get('embeddings')
            if isinstance(data,dict):
                if set(data)!={'float'}:raise ModelFailure('invalid_output')
                data=data['float']
            return EmbeddingResult(vectors(data,len(texts)),self.last_usage)
