# 多租户数据隔离设计：OpenViking 按用户独立 Account

> **状态：已实施**（2026-06-02）

## 背景

此前方案（`docs/proposals/userid-per-request-isolation.md`）将 Higo 的 `userId` 透传到 OpenViking 的 `X-OpenViking-User`，但仍共用同一个 `default` account 和 ROOT key。这导致：

1. **Session 数据在 account 内全局可见**（OpenViking 的 `viking://session/{id}` 是 account-global 的）
2. **权限边界模糊**：所有请求都用 ROOT key，一旦 higo2ov 被攻破，全量数据暴露
3. **无法级联清理**：OpenViking 的 `delete_account` 可以清理 account 下全部数据，但幽灵 account（未在 manager 注册）无法被清理

因此，将方案升级为**每个 Higo 用户在 OpenViking 中拥有独立的 account + USER key**。

## 目标

1. 每个 Higo 用户对应一个 OpenViking account，session、user memories、resources 完全物理隔离
2. higo2ov 自动懒创建 account 和 USER key，无需人工维护账户映射表
3. 业务请求使用 USER key（非 ROOT key），权限最小化
4. 映射关系持久化到本地 registry，重启后可恢复

## OpenViking 关键机制（代码依据）

### 1. Account 生命周期 API

```python
# openviking/server/routers/admin.py
POST /api/v1/admin/accounts                    # 创建 account
POST /api/v1/admin/accounts/{id}/users         # 注册 user，返回 USER key
GET  /api/v1/admin/accounts/{id}/users         # 列出 users
POST /api/v1/admin/accounts/{id}/users/{uid}/key  # 重新生成 key
DELETE /api/v1/admin/accounts/{id}             # 级联删除 account 及全部数据
```

`delete_account` 会级联清理：
- `viking://user/`, `viking://agent/`, `viking://session/`, `viking://resources/`
- VectorDB 中该 account 的全部索引

### 2. USER key 格式

```python
# openviking/server/api_keys/new.py
def parse_api_key(api_key: str) -> Tuple[str, str, str]:
    parts = api_key.split(".")
    account_id = _decode_segment(parts[0])   # base64url
    user_id    = _decode_segment(parts[1])   # base64url
    secret     = _decode_segment(parts[2])   # base64url
```

USER key 自带 `account_id` 和 `user_id`，请求时不需要再传 `X-OpenViking-Account`。

### 3. 物理路径隔离

```python
# openviking/storage/viking_fs.py
def _uri_to_path(self, uri: str, ctx=None) -> str:
    account_id = real_ctx.account_id
    return f"/local/{account_id}/{'/'.join(safe_parts)}"
```

`account_id` 来自 key 解析或 header，不同 account 的数据落在不同的 `/local/{account_id}/` 下。

### 4. ROOT key 权限

ROOT key 可以覆盖 `account_id` 和 `user_id`（`auth.py` 第206行），但必须显式提供 `X-OpenViking-Account` 和 `X-OpenViking-User` 才能访问 tenant-scoped API。

## 架构设计

```
Higo 请求
    │
    ▼
┌─────────────────────────────────────┐
│  main.py                              │
│  - 提取 session.userId                │
│  - 格式化为 higo_{userId}             │
└─────────────┬───────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  AccountRegistry                      │
│  - 内存缓存（dict）                    │
│  - 本地持久化（JSON + atomic write）   │
│  - 懒创建逻辑（asyncio.Lock）          │
└─────────────┬───────────────────────┘
              │
    ┌─────────┴──────────┐
    │                      │
    ▼                      ▼
┌──────────┐      ┌──────────────┐
│ AdminClient │      │ UserClient     │
│ (ROOT key)  │      │ (USER key)     │
│ 只用于 admin │      │ 用于业务 API   │
└─────┬─────┘      └──────┬───────┘
      │                    │
      ▼                    ▼
   OV admin API         OV data API
   /api/v1/admin/...   /api/v1/sessions/...
```

## 核心模块设计

### 1. AccountRegistry

职责：管理 `higo_user_id -> {account_id, user_id, api_key}` 的映射，支持懒创建。

```python
class AccountEntry(TypedDict):
    account_id: str
    user_id: str
    api_key: str
    created_at: str

class AccountRegistry:
    def __init__(self, registry_path: str, admin_client: OpenVikingClient):
        self._admin = admin_client          # ROOT key client
        self._registry_path = registry_path
        self._registry = self._load()
        self._cache: dict[str, OpenVikingClient] = {}
        self._lock = asyncio.Lock()

    async def get_client(self, higo_user_id: str) -> OpenVikingClient:
        """获取该 higo 用户对应的业务 Client（带缓存）。"""
        if higo_user_id in self._cache:
            return self._cache[higo_user_id]

        entry = self._registry.get(higo_user_id)
        if not entry:
            entry = await self._lazy_create(higo_user_id)

        client = OpenVikingClient(
            OpenVikingConfig(
                base_url=self._admin.config.base_url,
                api_key=entry["api_key"],
                agent_id=self._admin.config.agent_id,
            )
        )
        self._cache[higo_user_id] = client
        return client

    async def _lazy_create(self, higo_user_id: str) -> AccountEntry:
        """懒创建 account + user，带 asyncio.Lock 防并发。"""
        async with self._lock:
            # 双重检查
            entry = self._registry.get(higo_user_id)
            if entry:
                return entry

            account_id = higo_user_id

            # 1. 创建 account（如不存在）
            account_exists = await self._account_exists(account_id)
            if not account_exists:
                await self._admin.request(
                    "/api/v1/admin/accounts",
                    {"method": "POST", "body": {
                        "account_id": account_id,
                        "admin_user_id": "default",
                    }},
                )

            # 2. 获取 USER key（尝试注册，409 则降级 regenerate key）
            try:
                reg_resp = await self._admin.request(
                    f"/api/v1/admin/accounts/{account_id}/users",
                    {"method": "POST", "body": {"user_id": "default", "role": "user"}},
                )
                api_key = reg_resp["user_key"]
            except Exception as e:
                if "409" in str(e) or "Conflict" in str(e):
                    key_resp = await self._admin.request(
                        f"/api/v1/admin/accounts/{account_id}/users/default/key",
                        {"method": "POST"},
                    )
                    api_key = key_resp["user_key"]
                else:
                    raise

            entry = AccountEntry(
                account_id=account_id,
                user_id="default",
                api_key=api_key,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self._registry[higo_user_id] = entry
            self._persist()
            return entry
```

### 2. OpenVikingClient 改造

现状：`api_key` 在 `__init__` 时固定。需要支持**请求级覆盖 api_key**（但方案 A 中更简单的做法是每个 USER key 对应一个独立 Client 实例，因为不同 key 的 account 不同，header 也不同）。

因此 `OpenVikingClient` 本身不需要大改，只需要在 `AccountRegistry` 里按需 `new OpenVikingClient(...)` 即可。

`request` 方法保持 public（由单下划线 `_request` 重命名为 `request`），供 `AccountRegistry` 调用。

### 3. OpenVikingEngine 改造

不再持有单一的 `self.client`，而是持有 `self.account_registry`，每次操作前 `resolve` 用户 client：

```python
async def generate_memory(
    self, session_id: str, messages: list[dict],
    model_context_tokens: int = 0, user_id: str | None = None,
) -> str:
    ov_session_id = session_to_ov_id(session_id)
    client = await self.account_registry.get_client(user_id) if user_id else self.admin_client
    # ... 用 client 替代 self.client
```

> 注：`admin_client` 作为 fallback，用于不带 user_id 的请求（向后兼容）。

### 4. 配置变更

`.env` 变更：

```bash
# 修改前
OPENVIKING_API_KEY=ov-root-...
OPENVIKING_ACCOUNT_ID=default
OPENVIKING_USER_ID=default

# 修改后
OPENVIKING_ROOT_API_KEY=ov-root-...           # ROOT key，仅用于 admin
OPENVIKING_AGENT_ID=higo-extension
OPENVIKING_ACCOUNT_REGISTRY_PATH=.account_registry.json

# 删除以下两项（不再使用固定 account）
# OPENVIKING_ACCOUNT_ID=default
# OPENVIKING_USER_ID=default
```

`config.py` 变更：
- 环境变量 `OPENVIKING_API_KEY` 重命名为 `OPENVIKING_ROOT_API_KEY`，模型字段仍保持 `api_key`（兼容 `OpenVikingClient`）
- 删除 `account_id`、`user_id` 硬编码配置
- 新增 `account_registry_path`

## 请求流程（完整链路）

### Transform 请求

```
Higo POST /
    mode=transform
    session.sessionId=xxx
    session.userId=abc
        ↓
main.py: raw_user_id = "abc", user_id = "higo_abc"
        ↓
account_registry.get_client("higo_abc")
    - 查缓存：无
    - 查 registry：无
    - 懒创建：
        admin_client POST /admin/accounts  (account_id=higo_abc)
        admin_client POST /admin/accounts/higo_abc/users  (user_id=default)
        返回 USER key: "xxx.xxx.xxx"
        保存到 .account_registry.json
    - new OpenVikingClient(api_key=USER key)
        ↓
user_client POST /api/v1/sessions/{id}/messages      (X-API-Key=USER key)
user_client GET  /api/v1/sessions/{id}/context         (account=higo_abc)
user_client POST /api/v1/search/find                 (account=higo_abc)
user_client POST /api/v1/sessions/{id}/commit        (account=higo_abc)
```

### Result 请求

同上，`account_registry.get_client("higo_abc")` 会命中内存缓存，直接返回已有的 USER client。

## 风险与对策

| 风险 | 对策 |
|------|------|
| **并发创建**（两个请求同时遇到新用户） | `asyncio.Lock` 保护懒创建逻辑，内部双重检查 |
| **Registry 丢失**（文件被删，但 OV account 存在） | 懒创建时捕获 `AlreadyExistsError`，然后 `list users` + `regenerate_key`，重新持久化 |
| **Account ID 格式不合法** | Higo userId 是 UUID，加 `higo_` 前缀后符合 OpenViking 的 `validate_account_id`（需要实际验证） |
| **性能**（每次请求查 registry） | 内存缓存（`self._cache`），只有首次创建有开销 |
| **OV 不可达** | 抛异常，Higo 侧按无记忆处理（不阻断主流程） |
| **ROOT key 泄露** | 这是架构级风险，但 USER key 方案已把业务权限最小化；ROOT key 仅保存在 higo2ov 服务器 |
| **向后兼容**（不带 userId 的请求） | fallback 到 `admin_client`（ROOT key），行为与旧版一致 |

## 实施顺序

1. **配置层**：`.env` + `config.py`（改 `OPENVIKING_ROOT_API_KEY`，删 `ACCOUNT_ID`/`USER_ID`，加 `ACCOUNT_REGISTRY_PATH`）
2. **AccountRegistry 模块**：新建 `engine/account_registry.py`
3. **Client 层**：`engine/openviking_client.py`（`_request` 改为 public，或新增 `request` 方法）
4. **Engine 层**：`engine/openviking_engine.py`（引入 AccountRegistry，替换 `self.client` 为按需 resolve）
5. **入口层**：`main.py`（已完成的 user_id 提取逻辑保留，将 `memory_engine` 的调用方式改为通过 registry）
6. **清理**：删除 `.env` 中废弃的配置项，更新文档

## 验证方案

1. 首次请求（新用户）：验证 OV 侧创建了 `data/viking/higo_xxx/session/...` 和 `data/viking/higo_xxx/user/...`
2. 重复请求（同一用户）：验证命中缓存，未重复调 admin API
3. Registry 丢失后重启：验证能自动恢复（通过 `regenerate_key`）
4. 不带 userId 的请求：验证 fallback 到 ROOT client，数据落到 `default` account
5. Commit 后：验证 memories 提取到 `data/viking/higo_xxx/user/default/memories/`
