# Learn Claude Code

一个面向 Claude Code 风格的本地 Agent Runtime。

这个项目的目标不是复刻某个现成框架，而是把一个可持续扩展的 agent 运行时拆成几块清晰的能力：

- 主循环
- 工具调用
- 记忆系统
- 子代理与 teammate
- 权限与 hook
- 恢复与压缩

它目前已经能跑一个可交互的本地 coding agent，并且支持多轮会话、长期规则、长期经验、持久 teammate 和最小协议层。

## 整体结构

程序入口在 [main.py](E:\github\learn_claude_code\main.py)。

运行时核心都放在 [agent_runtime](E:\github\learn_claude_code\agent_runtime)：

- [agent.py](E:\github\learn_claude_code\agent_runtime\agent.py)
  主 AgentLoop，负责一轮轮推进、调用模型、执行工具、回填结果。
- [llm/openai_compatible.py](E:\github\learn_claude_code\agent_runtime\llm\openai_compatible.py)
  OpenAI-compatible 适配层。
- [tools](E:\github\learn_claude_code\agent_runtime\tools)
  所有工具定义。
- [memory.py](E:\github\learn_claude_code\agent_runtime\memory.py)
  长期规则、长期经验、自动记忆写入与按需检索。
- [team.py](E:\github\learn_claude_code\agent_runtime\team.py)
  持久 teammate、inbox、请求追踪和协议层。
- [subagents.py](E:\github\learn_claude_code\agent_runtime\subagents.py)
  一次性 fresh-context 子代理。
- [runtime_hooks.py](E:\github\learn_claude_code\agent_runtime\runtime_hooks.py)
  后台任务、memory retrieval、路径规则、权限等横切逻辑。
- [recovery.py](E:\github\learn_claude_code\agent_runtime\recovery.py)
  错误分类与恢复决策。
- [compaction.py](E:\github\learn_claude_code\agent_runtime\compaction.py)
  上下文压缩。

## 运行模型

主循环大致是：

1. 接收用户输入
2. 构建本轮上下文
3. 调用 LLM
4. 如果模型请求工具，就执行工具并回填结果
5. 重复，直到得到最终回答

这不是单纯的“prompt + tools”，而是一个有状态的 runtime。当前已经内置：

- 会话恢复
- 自动 compact
- 权限审查
- hook
- 错误恢复

## 记忆系统

项目现在采用分层记忆，不把所有内容混成一坨 prompt。

### 1. 长期规则层

规则文件统一使用：

- `learnclaude.md`
- `learnclaude.local.md`

它们属于外置上下文，不代表模型“学会了”这些内容。加载方式是按层拼接，然后作为启动上下文注入。

### 2. 长期经验层

长期经验放在：

```text
.memory/
  MEMORY.md
  topics/*.md
```

- `MEMORY.md` 是索引
- `topics/*.md` 是正文

系统会按需检索相关 topic，而不是每轮全量加载全部经验。

### 3. 会话记忆

主会话日志放在：

```text
.sessions/*.jsonl
```

启动时会从最近一次主会话恢复，默认只恢复最近窗口；如果有 compact 摘要，则优先使用“摘要 + 最近消息”的恢复模式。

### 4. 最近原始对话

最近未压缩的 `user / assistant` 对话会单独写到：

```text
.chat_history/recent_dialogue.jsonl
```

这层不参与主会话恢复，主要服务于自动记忆提取。

## 自动记忆

系统会在主回合结束后，基于最近对话和当前 memory 索引，尝试提取长期偏好或长期经验。

这一步不是盲目写入，而是会先做：

- normalize
- merge
- topic 复用

目标是避免同义内容被写成一堆碎片文件。

## Team 与 Subagent

项目里有两种“代理协作”方式。

### Subagent

`task` 对应的是一次性子代理：

- fresh context
- 同步运行
- 跑完就把结果返回给父 Agent

适合独立的小任务。

### Teammate

teammate 是持久 agent：

- 有固定 `agent_id`
- 有独立历史
- 有独立 inbox
- 可以跨多轮存活

team 相关状态都放在：

```text
.team/
  agents/
  inbox/
  requests/
  history/
  sessions/
  config.json
```

## 受限自治

当前没有做“完全自治”的 teammate。

现在实现的是更保守的版本：

- teammate 可以持久化
- teammate 可以基于自己的角色自动认领任务
- 但只能从现有 task graph 里拉取任务
- 不能自己发明新任务
- 也不会无视显式消息和协议请求

### 触发条件

只有同时满足这些条件时，teammate 才会自动认领任务：

- `auto_pull_tasks = true`
- 当前 inbox 为空
- 当前没有待处理 protocol request
- task graph 中存在匹配自己角色的 ready task
- 该任务尚未被其他 teammate 认领

### 角色匹配

当前任务是否允许某个 teammate 认领，使用现有任务字段 `owner` 作为最小路由提示：

- `owner` 为空：任何角色都可以认领
- `owner == agent_id`：只有指定 teammate 可以认领
- `owner == role`：只有对应角色可以认领

### 任务认领

当前任务图新增了最小 claim 信息：

- `claimed_by`
- `claimed_at`

当 teammate 自动认领任务时：

- 任务会被标记为 `in_progress`
- `claimed_by` 会写成当前 `agent_id`
- 其他 teammate 不会再认领同一项任务

这意味着现在做的不是“自由自治规划”，而是“带角色边界和 claim 保护的持久 worker 模式”。

## Team 协议层

现在的 team 通信分成两层。

### 普通消息

适合：

- 讨论
- 提醒
- 补充说明

继续使用：

- `team_send_message`

### 协议消息

适合：

- 审批
- 关机请求
- 交接
- 签收

使用：

- `team_send_protocol`
- `team_respond_protocol`
- `team_get_request`
- `team_list_requests`

每个协议请求都会在 `.team/requests/` 下持久化一份 `RequestRecord`，inbox 只负责投递，状态追踪走请求表。

## Hook 与权限

hook 只承接横切逻辑，不接管主循环。

当前主要 hook 点包括：

- `before_llm_request`
- `before_tool_execute`
- `after_tool_execute`
- `before_compact`
- `after_compact`

当前已经接入的横切能力主要有：

- 后台任务结果注入
- memory retrieval
- 路径规则激活
- 权限审查

权限系统是最小可用版本，重点是：

- 低风险直接放行
- 敏感操作要求确认
- 明显危险操作直接拒绝

## 错误处理与恢复

这套 runtime 现在不是只“捕获异常”，而是已经开始做恢复导向设计。

核心思路是：

- 先分类错误
- 再决定恢复动作
- 用运行状态承载恢复过程

当前主循环已经有这些运行状态：

- `RUNNING`
- `RETRYING`
- `RESUMING`
- `COMPACTING`
- `FAILED`
- `COMPLETED`

当前恢复动作主要覆盖：

- 临时网络错误重试
- 上下文溢出后 compact 再继续
- 非致命工具错误转成步骤级错误继续

## 主要工具

当前父 Agent 侧主要能力包括：

- 文件读写与编辑
- shell
- 后台任务
- todo
- task graph
- compact
- load_skill
- task
- team 系列工具

teammate 的工具集会更收敛，只保留适合长期协作的那部分。

## 项目目录

```text
main.py
agent_runtime/
  agent.py
  background_jobs.py
  compaction.py
  config.py
  dialogue_history.py
  hooks.py
  memory.py
  permissions.py
  recovery.py
  runtime_hooks.py
  session_log.py
  skills.py
  subagents.py
  task_graph.py
  team.py
  todo.py
  types.py
  llm/
  tools/
skills/
learnclaude.md
```

## 环境变量

必填：

- `LLM_MODEL`
- `LLM_API_KEY`

常用可选项：

- `LLM_BASE_URL`
- `LLM_TIMEOUT_SECONDS`
- `LLM_MAX_TOKENS`
- `LLM_TEMPERATURE`
- `LLM_CONTEXT_WINDOW`
- `SESSION_RESUME_MAX_MESSAGES`
- `RECENT_DIALOGUE_MAX_MESSAGES`
- `MEMORY_DIALOGUE_LOOKBACK`
- `LEARNCLAUDE_MANAGED_PATH`
- `PERMISSION_MODE`

示例：

```env
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=your_api_key_here
LLM_BASE_URL=https://api.openai.com/v1
LLM_CONTEXT_WINDOW=16000
PERMISSION_MODE=dev_safe
```

## 运行

```powershell
python main.py
```

## 当前阶段

这个项目已经不是最早的“单 agent + shell”原型了，当前更接近一个轻量 agent runtime，重点已经转向：

- 结构清晰
- 状态可恢复
- 能力分层
- 协作可扩展

后面继续迭代时，优先级大概会落在：

- 更完整的恢复路径
- 更稳的 team 协议
- 更好的记忆提取与检索
- 更细的权限与执行控制
