import asyncio
import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from models import (
    EngineInfo,
    MemoryQueryRequest,
    MemoryQueryResponse,
    Message,
    ProbeRequest,
    ProbeResponse,
    ResultAck,
    ResultRequest,
    ResultResponse,
    TransformRequest,
    TransformResponse,
    TransformResult,
)
from engine import OpenVikingConfig, OpenVikingClient, OpenVikingMemoryEngine
from monitor.server import mount_monitor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Higo Session Memory Plugin")
mount_monitor(app)

ENGINE_NAME = "higo-openviking-bridge"
ENGINE_VERSION = "1.1.0"

# Initialize OpenViking engine
_ov_config = OpenVikingConfig.from_env()
_ov_client = OpenVikingClient(_ov_config)
memory_engine = OpenVikingMemoryEngine(_ov_config, _ov_client)


@app.post("/")
async def handle(request: Request):
    body = await request.json()
    mode = body.get("mode")
    session_id = body.get("sessionId", "unknown")
    logger.info("[handle] mode=%s sessionId=%s", mode, session_id)
    logger.info(
        "[higo_request] %s", json.dumps(body, ensure_ascii=False)
    )

    if mode == "probe":
        probe_req = ProbeRequest.model_validate(body)
        resp = await _handle_probe(probe_req)
    elif mode == "transform":
        transform_req = TransformRequest.model_validate(body)
        resp = await _handle_transform(transform_req)
    elif mode == "result":
        result_req = ResultRequest.model_validate(body)
        resp = await _handle_result(result_req)
    elif mode == "memory_query":
        memory_query_req = MemoryQueryRequest.model_validate(body)
        resp = await _handle_memory_query(memory_query_req)
    else:
        logger.error("[handle] unknown mode: %s", mode)
        resp = JSONResponse(
            status_code=400,
            content={"ok": False, "summary": f"unknown mode: {mode}"},
        )

    # Log full response body before returning to Higo
    if isinstance(resp, JSONResponse):
        resp_body = resp.body.decode("utf-8")
    else:
        resp_body = json.dumps(resp.model_dump(), ensure_ascii=False)
    logger.info("[higo_response] %s", resp_body)

    return resp


@app.post("/compact")
async def compact(request: Request):
    """Force-commit an OpenViking session and return the post-compact summary."""
    body = await request.json()
    session_id = body.get("sessionId", "")
    if not session_id:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "sessionId is required"},
        )
    result = await memory_engine.compact(session_id)
    return JSONResponse(content=result)


async def _handle_probe(request: ProbeRequest) -> ProbeResponse:
    """Probe: check OpenViking connectivity in addition to local health."""
    sid = request.session.sessionId if request.session else "unknown"
    logger.info(
        "[probe] sessionId=%s timestamp=%s",
        sid,
        request.timestamp,
    )
    try:
        health = await _ov_client.health_check()
        logger.info("[probe] OpenViking health ok: %s", health)
        return ProbeResponse(
            ok=True,
            summary="probe ok",
            engine=EngineInfo(name=ENGINE_NAME, version=ENGINE_VERSION),
        )
    except Exception as e:
        logger.error("[probe] OpenViking health check failed: %s", e)
        return ProbeResponse(
            ok=False,
            summary=f"OpenViking unreachable: {e}",
            engine=EngineInfo(name=ENGINE_NAME, version=ENGINE_VERSION),
        )


async def _handle_transform(
    request: TransformRequest,
) -> TransformResponse:
    sid = request.session.sessionId if request.session else "unknown"
    original_messages = request.request.messages
    anchor_seq = request.round.seq if request.round else 0
    anchor_sub = 0
    model_tokens = (
        request.meta.modelContextWindowTokens
        if request.meta
        else 0
    )

    logger.info(
        "[transform] sessionId=%s anchor=%s/%s msg_count=%s modelTokens=%s",
        sid,
        anchor_seq,
        anchor_sub,
        len(original_messages),
        model_tokens,
    )
    for i, msg in enumerate(original_messages):
        logger.info(
            "[transform] original_msg[%s] role=%s content_len=%s",
            i,
            msg.role,
            len(msg.content),
        )

    memory_text = await memory_engine.generate_memory(
        sid,
        [m.model_dump() for m in original_messages],
        model_context_tokens=model_tokens,
    )
    logger.info(
        "[transform] memory_text generated, length=%s",
        len(memory_text) if memory_text else 0,
    )
    if memory_text:
        logger.debug("[transform] memory_text content:\n%s", memory_text)

    if memory_text and memory_text.strip():
        memory_message = Message(role="user", content=memory_text)
        new_messages = _build_messages(original_messages, memory_message)
    else:
        # Skip injection when memory is empty
        logger.info("[transform] memory is empty, skipping injection")
        new_messages = list(original_messages)

    logger.info(
        "[transform] returning msg_count=%s (added=%s)",
        len(new_messages),
        len(new_messages) - len(original_messages),
    )
    for i, msg in enumerate(new_messages):
        logger.info(
            "[transform] result_msg[%s] role=%s content_len=%s",
            i,
            msg.role,
            len(msg.content),
        )

    # Record turn data for monitoring (after message reconstruction)
    from monitor.collector import TurnCollector
    TurnCollector.get_instance().start_turn(
        session_id=sid,
        round_id=request.round.roundId if request.round else f"unknown_{time.time()}",
        seq=request.round.seq if request.round else 0,
        messages=[m.model_dump() for m in new_messages],
        model_tokens=model_tokens,
        memory_text=memory_text,
    )

    return TransformResponse(
        ok=True,
        result=TransformResult(
            request={"messages": new_messages},
            pluginContext={"memoryRevision": "higo-ov-r1"},
        ),
        summary="transform ok",
    )


async def _handle_result(request: ResultRequest) -> ResultResponse:
    """Handle round result callback from Higo.

    Captures assistant reply and tool results from message.sections
    into OpenViking session storage.
    """
    session_id = request.session.sessionId
    round_id = request.round.get("roundId", "unknown")
    status = request.round.get("status", "unknown")

    logger.info(
        "[result] sessionId=%s roundId=%s status=%s sections=%s errors=%s",
        session_id,
        round_id,
        status,
        len(request.message.sections),
        len(request.errors),
    )

    # Capture assistant reply and tool results from sections
    captured = 0
    if request.message.sections:
        captured = await memory_engine.capture_round_result(
            session_id,
            [s.model_dump() for s in request.message.sections],
            round_id=round_id,
        )

    # Async commit if threshold exceeded
    from engine.session_utils import session_to_ov_id
    ov_session_id = session_to_ov_id(session_id)
    asyncio.get_event_loop().create_task(
        memory_engine._maybe_commit(ov_session_id)
    )

    # Record turn output for monitoring
    from monitor.collector import TurnCollector
    TurnCollector.get_instance().end_turn(
        round_id=round_id,
        sections=[s.model_dump() for s in request.message.sections],
        errors=[e.model_dump() for e in request.errors],
    )

    logger.info(
        "[result] complete sessionId=%s roundId=%s captured=%s",
        session_id,
        round_id,
        captured,
    )

    return ResultResponse(
        ok=True,
        summary="result accepted",
        ack=ResultAck(
            roundId=round_id,
            stored=True,
            memoryRevision="higo-ov-r1",
        ),
    )


async def _handle_memory_query(
    request: MemoryQueryRequest,
) -> MemoryQueryResponse:
    sid = request.session.sessionId if request.session else "unknown"
    logger.info(
        "[memory_query] sessionId=%s action=%s", sid, request.action
    )
    return MemoryQueryResponse(
        ok=False,
        summary="当前功能不可用，暂不支持",
        result={},
        engine=EngineInfo(name=ENGINE_NAME, version=ENGINE_VERSION),
    )


def _build_messages(
    original: list[Message], memory_message: Message
) -> list[Message]:
    """Construct new message list with memory injected before the first user message.

    New protocol format:
        [0] system
        [1] user (memory)             <- injected
        [2] user (context environment)
        [3] user (current user message) <- must be last
    """
    if not original:
        return [memory_message]

    # Find the index of the first user message
    first_user_idx = -1
    for i, msg in enumerate(original):
        if msg.role == "user":
            first_user_idx = i
            break

    # If no user message found, append memory at end
    if first_user_idx < 0:
        return [*original, memory_message]

    result: list[Message] = []
    for i, msg in enumerate(original):
        if i == first_user_idx:
            result.append(memory_message)
        result.append(msg)

    return result


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
