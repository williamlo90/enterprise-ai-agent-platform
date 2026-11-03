"""Shared boundary models; endpoint-specific constraints can be stricter."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_max_length=4096)


class ErrorInfo(BaseModel):
    code: str
    message: str
    request_id: str
    retryable: bool


class ErrorResponse(BaseModel):
    error: ErrorInfo


class HealthResponse(BaseModel):
    status: Literal["ok", "ready"]
    scope: Literal["process", "bootstrap"]
