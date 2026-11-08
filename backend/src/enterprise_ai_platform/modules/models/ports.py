from typing import Protocol
from dataclasses import dataclass
from enterprise_ai_platform.modules.models.domain import ChatResult,EmbeddingResult
from enterprise_ai_platform.modules.models.domain import Message,ChatReply,ResponseContract
from enterprise_ai_platform.modules.models.domain import Usage,ModelFailure


class ModelPort(Protocol):
    def chat(self,messages:list[dict],*,tools:bool=False)->ChatResult: ...
    def embed(self,texts:list[str],*,input_type:str)->EmbeddingResult: ...


class ChatPort(Protocol):
    @property
    def last_usage(self)->Usage|None: ...
    def chat(self,messages:list[Message],*,tools:bool=False)->ChatReply: ...


class EmbeddingPort(Protocol):
    @property
    def last_usage(self)->Usage|None: ...
    def embed(self,texts:list[str],*,input_type:str)->EmbeddingResult: ...


@dataclass(frozen=True)
class ModelAdapters:
    chat: ChatPort
    embedding: EmbeddingPort
