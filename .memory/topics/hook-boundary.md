---
name: Hook Boundary
description: 项目中 hook 与主循环/manager 的职责边界，并附代码落点示例
type: project
---

在该项目中，hook 仅用于横切、事件驱动、低侵入的扩展；主循环与核心状态机仍由 AgentLoop/manager 显式编排与控制。可复用判定：

适合 hook：
- 工具执行前统一治理：`agent_runtime/runtime_hooks.py` 的 `PermissionHook`（`before_tool_execute`）
- 每轮请求前上下文增强：`BackgroundJobHook`、`MemoryRetrievalHook`（`before_llm_request`）
- 工具后附加规则激活：`PathScopedRuleHook`（`after_tool_execute`，典型 `read_file/write_file/edit_file`）

不适合 hook：
- 主循环编排与回合推进：`agent_runtime/agent.py` 中 `AgentLoop.run`
- 工具执行本体：`tool_registry.execute(...)`
- compact/todo/task graph 的核心状态管理（应在 AgentLoop/manager）

边界证据：`HookResult` 以 `decision/request_messages/append_messages/updates` 返回建议，主循环统一应用，说明 hook 不接管核心控制流。
