# Proposals 使用规则

`docs/proposals/` 用于保存尚未确认采用、正在讨论或正在验证的新方案。这里的内容不代表当前运行逻辑。

## 什么时候写 proposal

- 准备新增一个较完整的功能模块。
- 准备调整用户可感知的交互流程。
- 准备改变平台检测、入口注入、面板系统、数据流等运行架构。
- 需要先比较多个实现方案，再决定写代码。

小修小改、样式微调、bug 修复通常不需要新增 proposal。

## 命名规则

使用日期加主题命名：

```text
YYYY-MM-DD-topic.md
```

示例：

```text
2026-05-02-resource-manager-v2.md
2026-05-02-input-association-data-flow.md
```

## 推荐结构

```markdown
# 方案标题

## 状态

Draft / Implementing / Accepted / Rejected / Archived

## 背景

为什么需要这个方案。

## 目标

这个方案要解决什么问题。

## 非目标

这次明确不解决什么问题。

## 方案

核心交互、技术路径或数据流。

## 影响范围

涉及哪些文件、模块、页面或文档。

## 验收标准

怎么判断这个方案已经完成。

## 后续归档

采用后需要沉淀到 `docs/design/` 还是 `docs/architecture/`，是否需要移入 `docs/legacy/`。
```

## 生命周期

1. 新方案先放在 `docs/proposals/`。
2. 实现和验证期间，只维护对应 proposal。
3. 方案确认采用后，把最终稳定行为写入 `docs/design/` 或 `docs/architecture/`。
4. 原 proposal 有决策参考价值时移入 `docs/legacy/`；没有保留价值时可以删除。
5. 新增、移动、删除文档后，同步更新 [docs/README.md](../README.md)。
