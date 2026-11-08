"""Conversation payloads and typed references. Stored content is not authority."""
from dataclasses import dataclass,field
import re
from uuid import UUID

class ConversationFailure(RuntimeError):
    def __init__(self,status=503):self.status=status;super().__init__('Conversation operation rejected.')

def identifier(value):
    try:
        if not isinstance(value,str) or str(UUID(value))!=value:raise ValueError
    except (ValueError,TypeError,AttributeError):raise ConversationFailure(400) from None
    return value

def bounded_text(value,maximum,byte_max,*,empty=False):
    try:
        if not isinstance(value,str) or len(value)>maximum or len(value.encode('utf-8'))>byte_max or (not empty and not value.strip()):raise ValueError
        if any(ord(c)<32 and c not in '\r\n\t' or ord(c)==127 for c in value):raise ValueError
    except (ValueError,UnicodeError):raise ConversationFailure(400) from None
    return value

@dataclass(frozen=True)
class SourceRef:
    system:str
    kind:str
    resource_id:str=field(repr=False)
    version:str=field(repr=False)
    relation:str
    observed_revision:str=field(repr=False)
    page:int|None=None
    chunk:int|None=None
    def __post_init__(self):
        identifier(self.resource_id)
        relations={('platform','assisted_request'):{'owner'},('platform','document_version'):{'read'},('platform','file'):{'read'},('service','trip'):{'owner'},('service','claim'):{'owner','active_reviewer'},('service','receipt'):{'owner','active_reviewer'}}
        if self.relation not in relations.get((self.system,self.kind),set()):raise ConversationFailure(503)
        for value in (self.version,self.observed_revision):
            if not isinstance(value,str) or not re.fullmatch('[A-Za-z0-9_.:-]{1,128}',value):raise ConversationFailure(503)
        if self.page is not None and (type(self.page) is not int or not 1<=self.page<=100):raise ConversationFailure(503)
        if self.chunk is not None and (type(self.chunk) is not int or not 0<=self.chunk<=31):raise ConversationFailure(503)

@dataclass(frozen=True)
class SourceSubject:
    tenant_id:str=field(repr=False)
    principal_id:str=field(repr=False)
    def __post_init__(self):identifier(self.tenant_id);identifier(self.principal_id)

def message_refs(message):
    values=message.get('_sources')
    if not isinstance(values,list) or len(values)>16:raise ConversationFailure(503)
    try:return tuple(SourceRef(**value) for value in values)
    except (TypeError,ValueError,ConversationFailure):raise ConversationFailure(503) from None

def public_messages(messages,allowed):
    result=[]
    for message in messages:
        refs=message_refs(message)
        visible=(message['author_kind']=='human' or (message['provenance_mode']!='unverified' and
            (message['provenance_mode']!='dependent' or bool(refs)) and all(allowed.get(ref,False) for ref in refs)))
        item={key:message[key] for key in ('id','sequence','author_kind','run_id','output_kind','created_at')}
        item['redacted']=not visible
        if visible:
            item['text']=message['body']
            item['sources']=[{key:value for key,value in source.items()} for source in message['_sources']]
            item['feedback']=message.get('feedback')
            item['citations']=message.get('citations',[])
        else:item['text']=None;item['sources']=[];item['feedback']=None;item['citations']=[]
        result.append(item)
    return result
