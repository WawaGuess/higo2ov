# design/

本目录存放**已经确认并落地**的功能级设计文档。

## 适合存放的内容

- 功能交互流程
- 页面/组件状态机
- API 接口约定与字段说明
- 业务规则与边界条件
- 配置项与开关说明

## 不适合存放的内容

- 纯系统架构（应放入 `../architecture/`）
- 还在讨论的方案（应放入 `../proposals/`）
- 已废弃的功能设计（应移入 `../legacy/`）

## 文档命名规范

使用小写 kebab-case，以功能或页面命名：

```text
feature-name.md
page-name.md
```

示例：

```text
message-reconstruction.md
probe-api.md
```
