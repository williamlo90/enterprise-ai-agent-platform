"""Bounded extraction/chunking contract; content stays out of repr."""
from dataclasses import dataclass,field
from hashlib import sha256
from uuid import UUID

MODEL='cohere.embed-multilingual-v3'
DIMENSIONS=1024
CHUNK_CONFIG='utf8-384-nooverlap-v1'
MAX_TEXT_BYTES=12288
MAX_CHUNKS=32

class KnowledgeFailure(RuntimeError):
    def __init__(self,status=503):self.status=status;super().__init__('Knowledge operation rejected.')

def identifier(value):
    try:
        if not isinstance(value,str) or str(UUID(value))!=value:raise ValueError
    except (ValueError,TypeError,AttributeError):raise KnowledgeFailure(400) from None
    return value

@dataclass(frozen=True)
class Page:
    number:int
    text:str=field(repr=False)

@dataclass(frozen=True)
class Chunk:
    number:int
    page:int
    start:int
    end:int
    text:str=field(repr=False)
    digest:str

def chunks(pages):
    if not isinstance(pages,(list,tuple)) or not 1<=len(pages)<=100:raise KnowledgeFailure(422)
    result=[];total=0;previous=0
    for page in pages:
        if not isinstance(page,Page) or type(page.number) is not int or page.number<=previous or not isinstance(page.text,str):raise KnowledgeFailure(422)
        previous=page.number
        if any(ord(c)<32 and c not in '\r\n\t' for c in page.text) or '\x7f' in page.text:raise KnowledgeFailure(422)
        total+=len(page.text.encode('utf-8'))
        if total>MAX_TEXT_BYTES:raise KnowledgeFailure(413)
        start=0;size=0
        for index,char in enumerate(page.text):
            width=len(char.encode('utf-8'))
            if size+width>384:
                text=page.text[start:index]
                if text.strip():result.append(Chunk(len(result),page.number,start,index,text,sha256(text.encode()).hexdigest()))
                start=index;size=0
            size+=width
        text=page.text[start:]
        if text.strip():result.append(Chunk(len(result),page.number,start,len(page.text),text,sha256(text.encode()).hexdigest()))
        if len(result)>MAX_CHUNKS:raise KnowledgeFailure(413)
    if not result:raise KnowledgeFailure(422)
    return tuple(result)
