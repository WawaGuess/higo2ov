# Higo-OpenViking 记忆桥梁实现方案

## 1. 背景与目标

### 1.1 当前状态

Higo2OV 是一个极简的 FastAPI 服务，实现了 Higo 插件协议的 `probe`（健康检查）和 `transform`（消息加工）两种模式。`transform` 目前由 `PlaceholderMemoryEngine` 返回固定格式的占位记忆文本，无实际记忆能力。

### 1.2 目标

将 Higo2OV 改造为 **Higo 与 OpenViking 之间的桥梁**：Higo 继续通过 `probe/transform` 协议与本服务交互，本服务内部对接 OpenViking HTTP API，实现长时记忆存储、自动归档、语义回忆、上下文组装等 OpenClaw-Plugin 的核心功能。

### 1.3 关键约束

1. Higo 仅提供 `probe` 和 `transform` 两种回调，没有 `afterTurn`/`compact`/`session_start` 等生命周期钩子。因此所有 OpenViking 操作必须在 `transform` 请求处理中完成。
2. `transform` 模式下原始消息**不做任何修改和重排序**，只能**新增一条** `{"role": "user", "content": "[memory] ..."}` 消息，插入位置固定在 **context environment 消息之后、current user 消息之前**。
3. OpenViking 配置使用独立的 `.env` 配置文件，不从系统环境变量读取。

---

## 2. 架构总览

```
Higo Platform                    Higo2OV (本服务)                    OpenViking
    │                                  │                                  │
    │  POST /  mode=probe            │                                  │
    │───────────────────────────────>│  GET /health                     │
    │  {ok, summary, engine}         │─────────────────────────────────>│
    │<───────────────────────────────│  {status: ok}                    │
    │                                  │                                  │
    │  POST /  mode=transform        │                                  │
    │  {sessionId, request.messages} │  1. 将新消息追加到 OV session    │
    │───────────────────────────────>│     POST /sessions/{id}/messages │
    │                                  │─────────────────────────────────>│
    │                                  │  2. 获取会话上下文               │
    │                                  │     GET /sessions/{id}/context   │
    │                                  │<─────────────────────────────────│
    │                                  │  3. 搜索相关记忆                 │
    │                                  │     POST /search/find            │
    │                                  │<─────────────────────────────────│
    │                                  │  4. Token 预算分配 + 消息组装    │
    │                                  │  5. 如 pending_tokens > 阈值     │
    │                                  │     触发 commit (异步)           │
    │  {ok, result.request.messages} │                                  │
    │<───────────────────────────────│                                  │
    │                                  │                                  │
```

---

## 3. 详细设计

### 3.1 配置系统

#### `.env` 配置文件

项目根目录下创建 `.env` 文件存放 OpenViking 连接配置（不提交到 git，由 `.gitignore` 忽略）：

```dotenv
OPENVIKING_BASE_URL=http://127.0.0.1:1933
OPENVIKING_API_KEY=your-api-key
OPENVIKING_AGENT_ID=default
OPENVIKING_ACCOUNT_ID=
OPENVIKING_USER_ID=
OPENVIKING_TIMEOUT_MS=30000
OPENVIKING_COMMIT_TOKEN_THRESHOLD=8000
OPENVIKING_RECALL_LIMIT=10
OPENVIKING_RECALL_SCORE_THRESHOLD=0.1
OPENVIKING_ISOLATE_USER_SCOPE_BY_AGENT=false
OPENVIKING_ISOLATE_AGENT_SCOPE_BY_USER=true
```

#### `engine/config.py`

Pydantic 配置模型，从 `.env` 文件加载（使用 `python-dotenv`）：

```python
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import os

load_dotenv()

class OpenVikingConfig(BaseModel):
    base_url: str = Field(default="http://127.0.0.1:1933")
    api_key: str = Field(default="")
    agent_id: str = Field(default="default")
    account_id: str = Field(default="")
    user_id: str = Field(default="")
    timeout_ms: int = Field(default=30000)
    commit_token_threshold: int = Field(default=8000)
    recall_limit: int = Field(default=10)
    recall_score_threshold: float = Field(default=0.1)
    isolate_user_scope_by_agent: bool = Field(default=False)
    isolate_agent_scope_by_user: bool = Field(default=True)

    @classmethod
    def from_env(cls) -> "OpenVikingConfig":
        return cls(
            base_url=os.getenv("OPENVIKING_BASE_URL", "http://127.0.0.1:1933"),
            api_key=os.getenv("OPENVIKING_API_KEY", ""),
            agent_id=os.getenv("OPENVIKING_AGENT_ID", "default"),
            account_id=os.getenv("OPENVIKING_ACCOUNT_ID", ""),
            user_id=os.getenv("OPENVIKING_USER_ID", ""),
            timeout_ms=int(os.getenv("OPENVIKING_TIMEOUT_MS", "30000")),
            commit_token_threshold=int(os.getenv("OPENVIKING_COMMIT_TOKEN_THRESHOLD", "8000")),
            recall_limit=int(os.getenv("OPENVIKING_RECALL_LIMIT", "10")),
            recall_score_threshold=float(os.getenv("OPENVIKING_RECALL_SCORE_THRESHOLD", "0.1")),
            isolate_user_scope_by_agent=os.getenv("OPENVIKING_ISOLATE_USER_SCOPE_BY_AGENT", "false").lower() == "true",
            isolate_agent_scope_by_user=os.getenv("OPENVIKING_ISOLATE_AGENT_SCOPE_BY_USER", "true").lower() == "true",
        )
```

配置来源：仅读取 `.env` 文件（项目根目录），不从系统环境变量读取。

---

### 3.2 OpenViking HTTP 客户端

#### `engine/openviking_client.py`

封装所有 OpenViking API 调用（参考 `client.ts` 逻辑，用 Python/httpx 重写）：

```python
import httpx
from typing import Any, Optional

class OpenVikingClient:
    def __init__(self, config: OpenVikingConfig):
        self.config = config
        self._client = httpx.AsyncClient(timeout=config.timeout_ms / 1000)
        self._identity_cache: dict[str, dict] = {}

    async def health_check(self) -> dict:
        """GET /health"""
        resp = await self._client.get(f"{self.config.base_url}/health")
        resp.raise_for_status()
        return resp.json()

    async def add_session_message(
        self,
        session_id: str,
        role: str,
        parts: list[dict],
        created_at: Optional[str] = None,
        role_id: Optional[str] = None,
    ) -> dict:
        """POST /api/v1/sessions/{id}/messages"""
        body: dict[str, Any] = {"role": role, "parts": parts}
        if created_at:
            body["created_at"] = created_at
        if role_id:
            body["role_id"] = role_id
        resp = await self._client.post(
            f"{self.config.base_url}/api/v1/sessions/{session_id}/messages",
            json=body,
            headers=self._headers(),
        )
        return self._parse(resp)

    async def get_session(self, session_id: str) -> dict:
        """GET /api/v1/sessions/{id}"""
        resp = await self._client.get(
            f"{self.config.base_url}/api/v1/sessions/{session_id}",
            headers=self._headers(),
        )
        return self._parse(resp)

    async def get_session_context(self, session_id: str, token_budget: int = 128000) -> dict:
        """GET /api/v1/sessions/{id}/context"""
        resp = await self._client.get(
            f"{self.config.base_url}/api/v1/sessions/{session_id}/context?token_budget={token_budget}",
            headers=self._headers(),
        )
        return self._parse(resp)

    async def commit_session(self, session_id: str, wait: bool = False) -> dict:
        """POST /api/v1/sessions/{id}/commit"""
        resp = await self._client.post(
            f"{self.config.base_url}/api/v1/sessions/{session_id}/commit",
            json={},
            headers=self._headers(),
        )
        return self._parse(resp)

    async def find(
        self,
        query: str,
        target_uri: str,
        limit: int = 10,
        score_threshold: Optional[float] = None,
    ) -> dict:
        """POST /api/v1/search/find"""
        normalized_uri = await self._normalize_target_uri(target_uri)
        body = {
            "query": query,
            "target_uri": normalized_uri,
            "limit": limit,
            "score_threshold": score_threshold,
        }
        resp = await self._client.post(
            f"{self.config.base_url}/api/v1/search/find",
            json=body,
            headers=self._headers(),
        )
        return self._parse(resp)

    async def get_task(self, task_id: str) -> dict:
        """GET /api/v1/tasks/{task_id}"""
        resp = await self._client.get(
            f"{self.config.base_url}/api/v1/tasks/{task_id}",
            headers=self._headers(),
        )
        return self._parse(resp)

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.config.api_key:
            headers["X-API-Key"] = self.config.api_key
        if self.config.account_id:
            headers["X-OpenViking-Account"] = self.config.account_id
        if self.config.user_id:
            headers["X-OpenViking-User"] = self.config.user_id
        if self.config.agent_id:
            headers["X-OpenViking-Agent"] = self.config.agent_id
        return headers

    def _parse(self, resp: httpx.Response) -> dict:
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "error":
            err = data.get("error", {})
            raise RuntimeError(f"OpenViking error [{err.get('code')}]: {err.get('message')}")
        return data.get("result", data)

    async def _normalize_target_uri(self, target_uri: str) -> str:
        # 命名空间规范化：将 viking://user/memories 扩展为完整 URI
        # 参考 client.ts 中 buildCanonicalRoot + normalizeTargetUri 逻辑
        ...
```

---

### 3.3 记忆引擎实现

#### `engine/openviking_engine.py`

实现 `MemoryEngine` 的子类 `OpenVikingMemoryEngine`：

```python
from engine.memory import MemoryEngine
from engine.config import OpenVikingConfig
from engine.openviking_client import OpenVikingClient
from engine.memory_ranking import (
    deduplicate_by_uri,
    filter_leaf_memories,
    apply_score_threshold,
    rerank_memories,
    build_memory_lines_with_budget,
)
from engine.text_utils import extract_latest_user_text, messages_to_ov_parts
import asyncio

class OpenVikingMemoryEngine(MemoryEngine):
    def __init__(self, config: OpenVikingConfig, client: OpenVikingClient):
        self.config = config
        self.client = client

    async def generate_memory(self, session_id: str, messages: list[dict]) -> str:
        """
        核心入口，被 main.py 的 transform 处理器调用。
        在 Higo 约束下模拟 OpenClaw-Plugin 的 assemble + afterTurn + auto-recall 生命周期。
        """
        # 1. 消息追加（Capture / afterTurn 等价物）
        await self._capture_messages(session_id, messages)

        # 2. 获取会话上下文（Assemble）
        context = await self.client.get_session_context(session_id)

        # 3. 搜索相关记忆（Auto-recall）
        query_text = extract_latest_user_text(messages)
        memories = await self._recall_memories(query_text)

        # 4. 组装记忆文本
        memory_text = self._assemble_memory_text(context, memories)

        # 5. 异步触发 commit（如 pending_tokens 超过阈值）
        asyncio.create_task(self._maybe_commit(session_id))

        return memory_text

    async def _capture_messages(self, session_id: str, messages: list[dict]) -> None:
        """将新消息转换为 OV parts 格式并追加到 session。"""
        parts = messages_to_ov_parts(messages)
        if parts:
            # 区分 user 和 assistant 消息
            for msg in messages:
                role = msg.get("role", "")
                if role in ("user", "assistant"):
                    msg_parts = messages_to_ov_parts([msg])
                    if msg_parts:
                        await self.client.add_session_message(
                            session_id,
                            role=role,
                            parts=msg_parts,
                        )

    async def _recall_memories(self, query_text: str) -> list[dict]:
        """并行查询 user memories 和 agent memories。"""
        if not query_text.strip():
            return []

        # 构建目标 URI（考虑作用域隔离）
        user_uri = "viking://user/memories"
        agent_uri = "viking://agent/memories"

        results = []
        # 并行查询
        tasks = [
            self.client.find(
                query_text,
                user_uri,
                self.config.recall_limit,
                self.config.recall_score_threshold,
            ),
            self.client.find(
                query_text,
                agent_uri,
                self.config.recall_limit,
                self.config.recall_score_threshold,
            ),
        ]
        try:
            find_results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in find_results:
                if isinstance(res, Exception):
                    continue
                memories = res.get("memories", [])
                results.extend(memories)
        except Exception:
            pass

        # 后处理：去重、过滤叶子、阈值过滤、重排序
        results = deduplicate_by_uri(results)
        results = filter_leaf_memories(results)
        results = apply_score_threshold(results, self.config.recall_score_threshold)
        results = rerank_memories(results, query_text)

        return results

    def _assemble_memory_text(self, context: dict, memories: list[dict]) -> str:
        """组装返回给 Higo 的记忆文本。"""
        lines: list[str] = []

        # 历史摘要
        overview = context.get("latest_archive_overview", "")
        if overview:
            lines.append(f"[Session History Summary]")
            lines.append(overview)
            lines.append("")

        # 归档索引
        abstracts = context.get("pre_archive_abstracts", [])
        if abstracts:
            lines.append(f"[Archive Index]")
            for ab in abstracts:
                lines.append(f"- {ab.get('archive_id', 'unknown')}: {ab.get('abstract', '')}")
            lines.append("")

        # 相关记忆
        if memories:
            lines.append("<relevant-memories>")
            for mem in memories:
                category = mem.get("category", "memory")
                content = mem.get("abstract", mem.get("overview", ""))
                score = mem.get("score", 0)
                lines.append(f"- [{category}] {content} ({score:.0%})")
            lines.append("</relevant-memories>")
            lines.append("")

        return "\n".join(lines).strip()

    async def _maybe_commit(self, session_id: str) -> None:
        """如 pending_tokens 超过阈值，异步触发 commit。"""
        try:
            session_info = await self.client.get_session(session_id)
            pending_tokens = session_info.get("pending_tokens", 0)
            if pending_tokens > self.config.commit_token_threshold:
                await self.client.commit_session(session_id, wait=False)
        except Exception:
            pass
```

---

### 3.4 消息转换与组装（严格保持原始消息不变）

#### `main.py` 修改

- `probe` 处理器扩展：除返回固定成功外，先调用 `client.health_check()` 验证 OpenViking 连通性。如 OpenViking 不可用，返回 `ok=False` 和错误摘要。
- `transform` 处理器保持原有流程，但将 `memory_engine` 替换为 `OpenVikingMemoryEngine` 实例。
- **重写 `_build_messages`**：原始消息不做任何修改和重排序，仅在最末一条 `user`（current user）之前插入新增的 memory 消息。

原始消息格式（根据参考文档，固定 3~4 条）：
```
[0] system
[1] assistant（上一轮回复，可选）
[2] user（context environment）
[3] user（current user）
```

插入后格式：
```
[0] system                    ← 保留，位置不变
[1] assistant（若存在）       ← 保留，位置不变
[2] user（context environment）← 保留，位置不变
[3] user（[memory] summary）  ← 新增，唯一插入项
[4] user（current user）      ← 保留，位置不变，必须是最后一条 user
```

实现方式：遍历原始消息列表，找到最后一条 `role == "user"` 的索引，在该位置之前插入 memory 消息。不使用当前代码中的重排序逻辑（system → memory → assistant → context env → current user）。

```python
def _build_messages(
    original: list[Message], memory_message: Message
) -> list[Message]:
    """
    构造新消息列表：原始消息顺序不变，仅在最后一条 user 消息之前插入 memory 消息。
    保证 current user 消息仍是最后一条 user 消息。
    """
    if not original:
        return [memory_message]

    result: list[Message] = []
    inserted = False

    for i, msg in enumerate(original):
        # 如果当前是 user 且不是最后一条消息，检查下一条是否也是 user
        # 实际上：找到最后一条 user 的索引，在其前插入
        is_last_user = (
            msg.role == "user"
            and all(m.role != "user" for m in original[i + 1 :])
        )
        if msg.role == "user" and not is_last_user:
            result.append(msg)
        elif msg.role == "user" and is_last_user and not inserted:
            # 在最后一条 user 之前插入 memory
            result.append(memory_message)
            result.append(msg)
            inserted = True
        else:
            result.append(msg)

    # 兜底：如果没有找到合适的插入点，追加到最后（但不应发生）
    if not inserted:
        # 找到最后一个 user 的索引
        last_user_idx = -1
        for i, msg in enumerate(result):
            if msg.role == "user":
                last_user_idx = i
        if last_user_idx >= 0:
            result.insert(last_user_idx, memory_message)
        else:
            result.append(memory_message)

    return result
```

**注意**：Higo 会验证 system、context env、current user 三个锚点必须存在且 current user 必须是最后一条 user 消息。组装逻辑必须严格遵守此约束。

---

### 3.5 辅助模块

#### `engine/memory_ranking.py`

实现记忆排名和过滤：

```python
def deduplicate_by_uri(results: list[dict]) -> list[dict]:
    """按 URI 去重，保留分数最高者。"""
    seen: dict[str, dict] = {}
    for r in results:
        uri = r.get("uri", "")
        if not uri:
            continue
        existing = seen.get(uri)
        if existing is None or r.get("score", 0) > existing.get("score", 0):
            seen[uri] = r
    return list(seen.values())


def filter_leaf_memories(results: list[dict]) -> list[dict]:
    """仅保留 level == 2（叶子记忆，即完整内容）。"""
    return [r for r in results if r.get("level") == 2]


def apply_score_threshold(results: list[dict], threshold: float) -> list[dict]:
    """分数阈值过滤。"""
    return [r for r in results if r.get("score", 0) >= threshold]


def rerank_memories(results: list[dict], query: str) -> list[dict]:
    """查询感知重排序：检测查询类型（时间/偏好/实体），应用 boost。"""
    query_lower = query.lower()
    # 检测查询类型
    is_temporal = any(w in query_lower for w in ["when", "time", "date", "昨天", "今天", "明天", "上周", "之前", "之后"])
    is_preference = any(w in query_lower for w in ["prefer", "like", "want", "喜欢", "偏好", "习惯"])

    def score_boost(r: dict) -> float:
        base = r.get("score", 0)
        # leaf boost
        if r.get("level") == 2:
            base += 0.12
        # event temporal boost
        if is_temporal and r.get("category") == "events":
            base += 0.1
        # preference boost
        if is_preference and r.get("category") == "preferences":
            base += 0.08
        # lexical overlap boost
        content = r.get("abstract", "") + " " + r.get("overview", "")
        overlap = sum(1 for word in query_lower.split() if word in content.lower())
        base += min(overlap * 0.05, 0.2)
        return base

    results.sort(key=lambda r: score_boost(r), reverse=True)
    return results


def build_memory_lines_with_budget(
    results: list[dict], token_budget: int
) -> list[str]:
    """Token 预算内构建记忆行。"""
    lines: list[str] = []
    # TODO: 使用 tiktoken 精确计数
    # 第一条记忆即使超预算也包含（有界溢出）
    for i, r in enumerate(results):
        category = r.get("category", "memory")
        content = r.get("abstract", r.get("overview", ""))
        score = r.get("score", 0)
        line = f"- [{category}] {content} ({score:.0%})"
        if i == 0:
            lines.append(line)
            continue
        # 简单估算：如已用预算超过 80%，停止添加
        # 实际实现应使用 tiktoken 精确计算
        estimated = len("\n".join(lines)) // 4  # 粗略 4 字符/token
        if estimated < token_budget:
            lines.append(line)
    return lines
```

#### `engine/text_utils.py`

文本处理工具：

```python
def extract_latest_user_text(messages: list[dict]) -> str:
    """从消息列表中提取最新用户消息文本。"""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return msg.get("content", "")
    return ""


def sanitize_user_text_for_capture(text: str) -> str:
    """清洗用于捕获的文本（剥离 <relevant-memories>、元数据等）。"""
    import re
    # 剥离 <relevant-memories> 块
    text = re.sub(r"<relevant-memories>.*?</relevant-memories>", "", text, flags=re.DOTALL)
    # 剥离 fenced JSON 元数据块
    text = re.sub(r"```json\n.*?\n```", "", text, flags=re.DOTALL)
    # 剥离前导时间戳
    text = re.sub(r"^\[\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\]\s*", "", text)
    return text.strip()


def messages_to_ov_parts(messages: list[dict]) -> list[dict]:
    """将 Higo 消息转换为 OpenViking parts 格式。"""
    parts: list[dict] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            parts.append({"type": "text", "text": content})
        elif role == "assistant":
            parts.append({"type": "text", "text": content})
        elif role == "tool":
            # TODO: tool result 转换
            parts.append({"type": "tool", "tool_output": content})
    return parts
```

---

### 3.6 模型扩展

#### `models.py`

Higo 协议模型已完整，无需修改。OV 相关数据模型定义在 engine 内部使用，不暴露在 `models.py` 中。

---

## 4. 文件变更清单

| 文件 | 动作 | 说明 |
|------|------|------|
| `.env` | 新增 | OpenViking 连接配置 |
| `.gitignore` | 修改 | 忽略 `.env` 文件 |
| `main.py` | 修改 | probe 增加 OV 健康检查；切换 engine 实例；重写 `_build_messages` |
| `models.py` | 不变 | Higo 协议模型已完整 |
| `engine/memory.py` | 不变 | 抽象基类保留，新 engine 继承它 |
| `engine/openviking_client.py` | 新增 | OpenViking HTTP API 客户端 |
| `engine/openviking_engine.py` | 新增 | OpenViking 记忆引擎实现 |
| `engine/config.py` | 新增 | 配置模型和 `.env` 解析 |
| `engine/memory_ranking.py` | 新增 | 记忆排名、过滤、预算 |
| `engine/text_utils.py` | 新增 | 文本提取和清洗工具 |
| `engine/__init__.py` | 修改 | 导出新类 |
| `requirements.txt` | 修改 | 增加 httpx、tiktoken、python-dotenv |

---

## 5. 依赖变更

`requirements.txt` 新增：

```
httpx>=0.27.0          # 异步 HTTP 客户端
python-dotenv>=1.0.0   # .env 文件加载
tiktoken>=0.7.0        # OpenAI Token 计数（用于预算分配）
```

---

## 6. 验证方式

### 6.1 单元验证

```bash
python -c "from engine import OpenVikingClient, OpenVikingConfig; print('import ok')"
```

检查 Pydantic 模型无验证错误。

### 6.2 Probe 验证

启动 Higo2OV 服务，发送 probe 请求：

```bash
curl -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "probe",
    "sessionId": "test-session",
    "timestamp": "2026-05-22T00:00:00.000Z",
    "source": "higo"
  }'
```

确认：
- `ok=true` 且 engine 信息正确
- 停止 OpenViking 后，probe 返回 `ok=false`

### 6.3 Transform 验证

发送 transform 请求（使用参考文档中的请求示例）：

```bash
curl -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "transform",
    "sessionId": "test-session",
    "contextPath": "/",
    "anchor": {"seq": 2, "subSeq": 0},
    "request": {
      "messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "assistant", "content": "Hello!"},
        {"role": "user", "content": "Context info here."},
        {"role": "user", "content": "What is my name?"}
      ]
    },
    "meta": {
      "modelContextWindowTokens": 128000,
      "historyDefaultCount": 0,
      "historySource": "none",
      "requestKind": "user_turn",
      "source": "higo"
    }
  }'
```

检查响应结构：
- `result.request.messages` 为数组
- 原始消息顺序不变
- 新增 memory 消息在 context env 之后、current user 之前
- system/context env/current user 三个锚点保留
- current user 是最后一条 user 消息

### 6.4 记忆闭环验证

连续发送多轮 transform 请求（模拟多轮对话）：

1. 验证 OpenViking 中 session 消息逐轮累积
2. 验证 commit 触发后归档生成
3. 验证新一轮 transform 能召回之前轮次的记忆

### 6.5 端到端验证

在 Higo 前端配置 memory engine endpoint 指向本服务，进行真实对话，观察模型是否能利用历史记忆。
