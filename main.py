from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from models import (
    ProbeRequest,
    TransformRequest,
    ProbeResponse,
    TransformResponse,
    TransformResult,
    ResultRequest,
    EngineInfo,
    DebugInfo,
    Message,
)
from engine import PlaceholderMemoryEngine

app = FastAPI(title="Higo Session Memory Plugin")

memory_engine = PlaceholderMemoryEngine()

ENGINE_NAME = "higo-memory-plugin"
ENGINE_VERSION = "1.0.0"


@app.post("/")
async def handle(request: Request):
    body = await request.json()
    mode = body.get("mode")

    if mode == "probe":
        probe_req = ProbeRequest.model_validate(body)
        return _handle_probe(probe_req)
    elif mode == "transform":
        transform_req = TransformRequest.model_validate(body)
        return await _handle_transform(transform_req)
    else:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "summary": f"unknown mode: {mode}"},
        )


def _handle_probe(request: ProbeRequest) -> ProbeResponse:
    return ProbeResponse(
        ok=True,
        summary="probe ok",
        engine=EngineInfo(name=ENGINE_NAME, version=ENGINE_VERSION),
    )


async def _handle_transform(request: TransformRequest) -> TransformResponse:
    original_messages = request.request.messages

    memory_text = await memory_engine.generate_memory(
        request.sessionId,
        [m.model_dump() for m in original_messages],
    )
    memory_message = Message(role="user", content=memory_text)

    new_messages = _build_messages(original_messages, memory_message)

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
    """
    构造新消息列表，保持锚点语义：
    1. system（保留）
    2. user（注入的记忆消息）
    3. assistant（上一轮回复，若存在）
    4. user（上下文环境信息）
    5. user（本轮用户消息，必须最后）
    """
    if not original:
        return [memory_message]

    # 取 system 消息
    system_msg = original[0]

    # 取最后两条 user 消息：倒数第二条是环境信息，最后一条是当前用户消息
    context_env_msg = original[-2] if len(original) >= 2 else None
    current_user_msg = original[-1]

    # 检查 messages[1] 是否为 assistant（上一轮回复）
    assistant_msg = None
    if len(original) >= 2 and original[1].role == "assistant":
        assistant_msg = original[1]

    result: list[Message] = [system_msg, memory_message]

    if assistant_msg is not None:
        result.append(assistant_msg)

    # 如果 context_env_msg 存在且不是 assistant_msg 也不是最后一条，保留它
    if (
        context_env_msg is not None
        and context_env_msg is not assistant_msg
        and context_env_msg is not current_user_msg
    ):
        result.append(context_env_msg)

    # 最后一条必须是当前用户消息
    result.append(current_user_msg)

    return result


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
