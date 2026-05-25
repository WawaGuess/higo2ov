import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from models import (
    DebugInfo,
    EngineInfo,
    Message,
    ProbeRequest,
    ProbeResponse,
    ResultRequest,
    TransformRequest,
    TransformResponse,
    TransformResult,
)
from engine import OpenVikingConfig, OpenVikingClient, OpenVikingMemoryEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Higo Session Memory Plugin")

ENGINE_NAME = "higo-openviking-bridge"
ENGINE_VERSION = "1.0.0"

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
    logger.info(
        "[probe] sessionId=%s timestamp=%s",
        request.sessionId,
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
    original_messages = request.request.messages
    logger.info(
        "[transform] sessionId=%s anchor=%s/%s msg_count=%s modelTokens=%s",
        request.sessionId,
        request.anchor.seq,
        request.anchor.subSeq,
        len(original_messages),
        request.meta.modelContextWindowTokens,
    )
    for i, msg in enumerate(original_messages):
        logger.info(
            "[transform] original_msg[%s] role=%s content_len=%s",
            i,
            msg.role,
            len(msg.content),
        )

    memory_text = await memory_engine.generate_memory(
        request.sessionId,
        [m.model_dump() for m in original_messages],
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

    return TransformResponse(
        ok=True,
        result=TransformResult(
            request=ResultRequest(messages=new_messages),
            debug=DebugInfo(source=ENGINE_NAME),
        ),
        summary="transform ok",
    )


def _build_messages(
    original: list[Message], memory_message: Message
) -> list[Message]:
    """Construct new message list without modifying or reordering original messages.

    The memory message is inserted immediately before the last user message
    (the current user message), preserving all original message order.

    Original format (per Higo protocol):
        [0] system
        [1] assistant (previous turn reply, optional)
        [2] user (context environment)
        [3] user (current user message)

    After insertion:
        [0] system                    <- preserved
        [1] assistant (if present)    <- preserved
        [2] user (context env)        <- preserved
        [3] user (memory)             <- inserted
        [4] user (current user)       <- preserved, must be last user
    """
    if not original:
        return [memory_message]

    result: list[Message] = []
    inserted = False

    # Find the index of the last user message
    last_user_idx = -1
    for i, msg in enumerate(original):
        if msg.role == "user":
            last_user_idx = i

    # If no user message found (should not happen), append memory at end
    if last_user_idx < 0:
        return [*original, memory_message]

    # Insert memory before the last user message
    for i, msg in enumerate(original):
        if i == last_user_idx and not inserted:
            result.append(memory_message)
            inserted = True
        result.append(msg)

    return result


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
