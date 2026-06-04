# Bug: OpenViking 搜索返回的 memory category 为空

> **归档说明**：此问题已通过 higo2ov 侧的 workaround 修复（方案 A）。OpenViking 服务端根因未修复。本文档保留供回溯参考。

## 问题描述

在 higo2ov 的 transform 流程中，`_recall_memories` 调用 OpenViking 的 `POST /api/v1/search/find` 接口搜索相关记忆。搜索结果中的 `category` 字段全部为 `""`（空字符串），导致拼接出的记忆文本显示为 `- [] content` 而非 `- [memory] content`。

**实际表现：**

```
<relevant-memories>
- [] 小明会写Java，掌握Java编程技能。 (40%)
- [] 用户姓名：小明
会写Java编程 (38%)
- [] # 小明
- 姓名：小明
- 技能：Java编程
(as of 2026-05-25) (38%)
</relevant-memories>
```

**预期表现：**

```
<relevant-memories>
- [profile] 小明会写Java，掌握Java编程技能。 (40%)
- [preferences] 用户姓名：小明
会写Java编程 (38%)
- [entities] # 小明
- 姓名：小明
- 技能：Java编程
(as of 2026-05-25) (38%)
</relevant-memories>
```

---

## 问题根因

### OpenViking 的存储结构

OpenViking 服务端**确实按 category 分目录存储**记忆：

```
viking://user/{user_id}/memories/
  ├── preferences/          # 偏好记忆
  ├── events/               # 事件记忆
  ├── entities/             # 实体记忆
  └── profile.md            # 用户档案
```

例如：
- `viking://user/default/memories/entities/user/小明.md`
- `viking://user/default/memories/preferences/小明/编程语言偏好.md`
- `viking://user/default/memories/profile.md`

### OpenViking 的搜索返回

`POST /api/v1/search/find` 返回的记忆数据结构中，`category` 字段从存储的文档元数据中读取：

```python
# openviking/retrieve/hierarchical_retriever.py:565
results.append(
    MatchedContext(
        uri=display_uri,
        category=c.get("category", ""),  # 从底层存储读取
        ...
    )
)
```

但底层存储（向量索引/数据库）在写入文档时**没有将 category 字段存入元数据**，导致搜索返回时 `category` 永远为空字符串。

**注意：** OpenViking 的 `Context._derive_category()` 方法（`openviking/core/context.py`）实现了从 URI 路径推导 category 的逻辑，但该逻辑仅在存储层创建 `Context` 对象时使用，并未被搜索返回路径复用。

---

## 影响范围

- **higo2ov 侧：** `build_memory_lines_with_budget` 中 `r.get("category", "memory")` 因 key 存在但值为 `""`，导致显示为空 `[]`
- **功能影响：** 无功能损坏，仅显示异常
- **用户体验：** 拼接的记忆文本中 category 标签缺失，可读性下降

---

## 解决方案（已实施）

### 方案 A：在 higo2ov 侧修复（workaround）

在 `engine/memory_ranking.py` 的 `build_memory_lines_with_budget` 中，从 URI 路径推导 category：

```python
def _derive_category_from_uri(uri: str) -> str:
    if "/preferences/" in uri:
        return "preferences"
    if "/entities/" in uri:
        return "entities"
    if "/events/" in uri:
        return "events"
    if "/profile" in uri:
        return "profile"
    if "/patterns/" in uri:
        return "patterns"
    if "/cases/" in uri:
        return "cases"
    return "memory"

# 在 build_memory_lines_with_budget 中
category = r.get("category") or _derive_category_from_uri(r.get("uri", ""))
```

**2026-05-28：** higo2ov 侧已实施方案 A，在 `engine/memory_ranking.py` 中新增 `_derive_category_from_uri()` 函数，从 URI 路径推导 category。当前注入记忆文本中 category 标签显示正常。

> 注：此为 higo2ov 侧的 workaround，OpenViking 服务端返回的 `category` 字段仍为空字符串，未从根因修复。

---

## 相关代码

| 逻辑 | 文件 | 函数/类 |
|------|------|---------|
| Category 推导 workaround | `engine/memory_ranking.py` | `_derive_category_from_uri()` |
| 记忆行组装 | `engine/memory_ranking.py` | `build_memory_lines_with_budget()` |
