from typing import Literal, Any
from pydantic import BaseModel, Field


class Message(BaseModel):
    role: str
    content: str


class Session(BaseModel):
    sessionId: str
    contextPath: str | None = None
    userId: str | None = None


class Round(BaseModel):
    roundId: str
    seq: int
    startedAt: str


class RequestPayload(BaseModel):
    messages: list[Message]


class Meta(BaseModel):
    modelContextWindowTokens: int
    historyDefaultCount: int | None = None
    historySource: str | None = None
    requestKind: str | None = None
    source: str | None = None


class BaseRequest(BaseModel):
    mode: Literal["probe", "transform", "result"]


class ProbeRequest(BaseModel):
    mode: Literal["probe"] = "probe"
    protocolVersion: str | None = None
    source: str
    session: Session | None = None
    timestamp: str


class TransformRequest(BaseModel):
    mode: Literal["transform"] = "transform"
    protocolVersion: str | None = None
    source: str | None = None
    session: Session | None = None
    round: Round | None = None
    request: RequestPayload
    meta: Meta | None = None


class EngineInfo(BaseModel):
    name: str
    version: str


class ProbeResponse(BaseModel):
    ok: bool = True
    summary: str
    engine: EngineInfo


class TransformResultRequest(BaseModel):
    messages: list[Message]


class TransformResult(BaseModel):
    request: TransformResultRequest
    pluginContext: dict | None = None


class TransformResponse(BaseModel):
    ok: bool = True
    result: TransformResult
    summary: str


# ---------------------------------------------------------------------------
# Result callback models (V2 protocol)
# ---------------------------------------------------------------------------


class Section(BaseModel):
    subSeq: int
    type: str
    content: str | None = None
    reasoning: str | None = None
    toolname: str | None = None
    toolCallId: str | None = None
    toolargs: str | None = None
    toolrsp: str | None = None
    argsReady: bool | None = None
    toolExecutionState: str | None = None
    error: str | None = None


class ResultMessage(BaseModel):
    seq: int
    usermsg: str
    systemPrompt: str
    contextEnvironment: str
    sections: list[Section]


class ResultTransform(BaseModel):
    applied: bool
    summary: str | None = None
    pluginContext: dict | None = None
    systemPrompt: str | None = None
    contextEnvironment: str | None = None


class ResultError(BaseModel):
    code: str
    message: str
    at: str


class ResultRequest(BaseModel):
    mode: Literal["result"] = "result"
    protocolVersion: str
    source: str
    session: Session
    round: dict
    transform: ResultTransform | None = None
    message: ResultMessage
    errors: list[ResultError]


class ResultAck(BaseModel):
    roundId: str
    stored: bool
    memoryRevision: str | None = None


class ResultResponse(BaseModel):
    ok: bool = True
    summary: str
    ack: ResultAck
