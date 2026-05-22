# architecture/

本目录存放**已经确认并落地**的系统级架构文档。

## 适合存放的内容

- 模块划分与依赖关系
- 核心数据流图
- 接口分层与调用链
- 部署与运行架构
- 技术选型及理由

## 不适合存放的内容

- 尚未实现的设想（应放入 `../proposals/`）
- 单一功能的业务逻辑细节（应放入 `../design/`）
- 废弃的旧架构（应移入 `../legacy/`）

## 文档命名规范

使用小写 kebab-case，以模块或主题命名：

```text
module-name.md
system-topic.md
```

示例：

```text
memory-engine.md
data-flow.md
```
