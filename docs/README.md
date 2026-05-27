# 文档目录规范

本文档说明 `docs/` 目录的组织结构，以及后续输出文档应该放在哪个文件夹下。

---

## 目录结构

```text
docs/
├── README.md           ← 本文件（目录规范说明）
├── architecture/       ← 架构文档：已确认的系统结构、模块关系、数据流
├── design/             ← 设计文档：已确认的功能逻辑、交互流程、接口约定
├── legacy/             ← 历史文档：已废弃或已替换的方案、旧版逻辑存档
└── proposals/          ← 提案文档：尚未确认、正在讨论或验证中的新方案
```

---

## 各目录用途

### `docs/architecture/` — 架构文档

存放**已经确认并落地**的系统级架构说明。

**适合存放的内容：**
- 模块划分与依赖关系
- 核心数据流图
- 接口分层与调用链
- 部署与运行架构
- 技术选型及理由

**不适合存放的内容：**
- 尚未实现的设想
- 单一功能的业务逻辑细节（应放入 `design/`）
- 废弃的旧架构（应移入 `legacy/`）

---

### `docs/design/` — 设计文档

存放**已经确认并落地**的功能级设计说明。

**适合存放的内容：**
- 功能交互流程
- 页面/组件状态机
- API 接口约定与字段说明
- 业务规则与边界条件
- 配置项与开关说明

**不适合存放的内容：**
- 纯系统架构（应放入 `architecture/`）
- 还在讨论的方案（应放入 `proposals/`）
- 已废弃的功能设计（应移入 `legacy/`）

---

### `docs/legacy/` — 历史文档

存放**已废弃、已替换或不再适用**的文档，保留供回溯参考。

**适合存放的内容：**
- 已废弃的旧版架构或设计方案
- 从 `proposals/` 采纳后失去时效性的原始提案
- 被替换模块的历史逻辑说明

**规则：**
- 移入时建议在文档顶部标注废弃原因和替代方案链接
- 不要删除仍在参考价值的文档
- 完全没有保留价值的文档可以直接删除，无需移入

---

### `docs/proposals/` — 提案文档

存放**尚未确认、正在讨论或正在验证**中的新方案。

详细规则见 [`proposals/README.md`](proposals/README.md)。

**核心要点：**
- 新增较完整功能模块、调整交互流程、改变运行架构前，先写 proposal
- 小修小改、样式微调、bug 修复不需要 proposal
- 命名格式：`YYYY-MM-DD-topic.md`

---

## 文档生命周期

```
讨论/验证阶段     确认落地后         废弃后
     |                |               |
     v                v               v
+----------+    +----------+    +----------+
|proposals/|--> |architecture/|  |          |
|          |    |或 design/ |--> | legacy/  |
+----------+    +----------+    +----------+
```

1. **新建**：新想法先以 proposal 形式放入 `docs/proposals/`
2. **采纳**：方案确认后，将稳定内容沉淀到 `docs/architecture/` 或 `docs/design/`
3. **废弃**：旧文档根据保留价值移入 `docs/legacy/` 或直接删除
4. **同步**：每次新增、移动、删除文档后，更新本文件（`docs/README.md`）中的目录索引

---

## 新增文档清单（维护此处）

| 文档路径 | 状态 | 说明 |
|---------|------|------|
| `README.md` | Active | 文档目录总规范 |
| `architecture/README.md` | Active | 架构文档目录规范 |
| `design/README.md` | Active | 设计文档目录规范 |
| `design/end-to-end-deployment-guide.md` | Active | 端到端部署与对接指南 |
| `design/higo-openviking-bridge-implementation.md` | Active | 当前代码逻辑实现文档（已含 memory_query 占位） |
| `design/higo2ov-sequence-diagram.md` | Active | Higo2OV 时序图与协议交互 |
| `legacy/README.md` | Active | 历史文档目录规范 |
| `logging-reference.md` | Active | 日志前缀与排查参考 |
| `openclaw-plugin-feature-comparison.md` | Active | higo2ov 与 OpenClaw-Plugin 当前能力对比 |
| `proposals/README.md` | Active | 提案文档目录规范 |
| `proposals/higo-v2-protocol-adaptation-plan.md` | Proposal | Higo V2 协议适配方案记录 |
| `proposals/message-capture-conversion-plan.md` | Proposal | Higo 消息捕获转换方案记录 |
| `proposals/openviking-bridge-implementation-plan.md` | Proposal | OpenViking 桥接实现方案记录 |
