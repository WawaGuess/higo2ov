from typing import Literal, Any
from pydantic import BaseModel, Field


class Message(BaseModel):
    role: str
    content: str


class Anchor(BaseModel):
    seq: int
    subSeq: int


class RequestPayload(BaseModel):
    messages: list[Message]


class Meta(BaseModel):
    modelContextWindowTokens: int
    historyDefaultCount: int
    historySource: str
    requestKind: str
    source: str


class BaseRequest(BaseModel):
    mode: Literal["probe", "transform"]
    sessionId: str
    source: str


class ProbeRequest(BaseRequest):
    mode: Literal["probe"] = "probe"
    timestamp: str


class TransformRequest(BaseRequest):
    mode: Literal["transform"] = "transform"
    contextPath: str
    anchor: Anchor
    request: RequestPayload
    meta: Meta


class EngineInfo(BaseModel):
    name: str
    version: str


class ProbeResponse(BaseModel):
    ok: bool = True
    summary: str
    engine: EngineInfo


class DebugInfo(BaseModel):
    source: str


class ResultRequest(BaseModel):
    messages: list[Message]


class TransformResult(BaseModel):
    request: ResultRequest
    debug: DebugInfo


class TransformResponse(BaseModel):
    ok: bool = True
    result: TransformResult
    summary: str
