# Learn Claude Code

一个面向 Claude Code 风格的本地 Agent Runtime。

这个项目的目标不是复制某个现成框架，而是把一个可持续扩展的 agent 运行时拆成几层清晰能力：

- 主循环与工具调用
- 分层记忆
- subagent 与 teammate 协作
- hook 与权限控制
- 恢复与压缩
- 持久化任务图

当前项目已经不是简单的 “prompt + tools” 脚本，而是一个有状态、可恢复、可扩展的本地 coding agent runtime。

## 核心结构

入口在 [E:\github\learn_claude_code\main.py](E:\github\learn_claude_code\main.py)。

核心模块都在 [E:\github\learn_claude_code\agent_runtime](E:\github\learn_claude_code\agent_runtime)：

- [E:\github\learn_claude_code\agent_runtime\agent.py](E:\github\learn_claude_code\agent_runtime\agent.py)
  主循环 `AgentLoop`
- [E:\github\learn_claude_code\agent_runtime\llm\openai_compatible.py](E:\github\learn_claude_code\agent_runtime\llm\openai_compatible.py)
  OpenAI-compatible 模型适配层
- [E:\github\learn_claude_code\agent_runtime\memory.py](E:\github\learn_claude_code\agent_runtime\memory.py)
  规则层、长期经验层、自动记忆与检索
- [E:\github\learn_claude_code\agent_runtime\team.py](E:\github\learn_claude_code\agent_runtime\team.py)
  持久 teammate、inbox、请求协议
- [E:\github\learn_claude_code\agent_runtime\subagents.py](E:\github\learn_claude_code\agent_runtime\subagents.py)
  一次性 fresh-context 子代理
- [E:\github\learn_claude_code\agent_runtime\task_graph.py](E:\github\learn_claude_code\agent_runtime\task_graph.py)
  持久化任务图、依赖、claim、并发保护
- [E:\github\learn_claude_code\agent_runtime\runtime_hooks.py](E:\github\learn_claude_code\agent_runtime\runtime_hooks.py)
  运行时横切逻辑
- [E:\github\learn_claude_code\agent_runtime\recovery.py](E:\github\learn_claude_code\agent_runtime\recovery.py)
  错误分类与恢复决策
- [E:\github\learn_claude_code\agent_runtime\compaction.py](E:\github\learn_claude_code\agent_runtime\compaction.py)
  上下文压缩
- [E:\github\learn_claude_code\agent_runtime\mcp_client.py](E:\github\learn_claude_code\agent_runtime\mcp_client.py)
  MCP client，支持 stdio 与 HTTP 两类 transport
- [E:\github\learn_claude_code\agent_runtime\tool_router.py](E:\github\learn_claude_code\agent_runtime\tool_router.py)
  统一工具路由层，把本地工具和 MCP 工具接到同一个执行入口

## 工具系统

当前工具系统已经从“全部本地硬编码”升级成“本地工具 + MCP 工具”两种来源。

### 本地工具

本地工具仍然保留原来的命名方式，例如：

- `read_file`
- `task_create`
- `team_send_message`

它们直接对应本地 `BaseTool` 实现。

### MCP 工具

MCP 工具在逻辑上统一表示为：

```text
mcp.<server>.<tool>
```

例如：

- `mcp.github.search_prs`
- `mcp.mock.echo`

但真正注册给模型的 schema name 会转成安全格式：

```text
mcp__<server>__<tool>
```

例如：

- `mcp__github__search_prs`
- `mcp__smart_search__ai_search_github`

这样做的目的很明确：

- 一眼看出工具来源
- 避免和本地工具撞名
- 兼容 OpenAI-compatible 工具名只允许字母、数字、下划线和短横线的限制
- 方便后续做权限控制、日志和错误分类

### 统一工具描述

现在工具注册时不会只保留“一个本地工具实例”，而是统一转成 `ToolSpec`：

- `name`
- `description`
- `input_schema`
- `source`

其中 `source` 目前只分两类：

- `local`
- `mcp`

也就是说，工具描述和工具执行已经分离。前者告诉模型“有哪些工具”，后者决定“这个工具到底怎么执行”。

### 统一路由器

`ToolRouter` 只做一件事：

- 如果是本地工具，就交给本地 handler
- 如果是 MCP 工具，就交给 `MCPClient`

Router 不关心 `shell / task / github` 这些业务语义，只关心工具来源。

### MCP client

`MCPClient` 当前支持两类 transport，负责：

1. 连接 MCP server
2. 做 `initialize`
3. 拉 `tools/list`
4. 执行 `tools/call`

当前支持通过两种方式加载 MCP server 配置：

- 环境变量 `MCP_SERVERS_JSON`
- 仓库内 `.mcp/servers.json`

一个 stdio 示例：

```json
{
  "servers": {
    "mock": {
      "command": "python",
      "args": ["-m", "agent_runtime.mock_mcp_server"],
      "cwd": "E:/github/learn_claude_code"
    }
  }
}
```

一个 HTTP 示例：

```json
{
  "servers": {
    "smart_search": {
      "transport": "http",
      "url": "https://mcpmarket.cn/mcp/ec9e8779bf4ced5c048d22f6",
      "tools": ["ai_search_web", "ai_search_github"]
    }
  }
}
```

其中：

- `stdio` transport 通过本地 `command + args` 启动进程
- `http` transport 直接对远端 URL 发 JSON-RPC 请求
- `http` transport 兼容普通 JSON 与 `text/event-stream` 响应
- 如果服务端返回 `mcp-session-id`，client 会在后续请求里自动带上
- `tools` 可选，用来限制只注册该 server 下的一部分工具，减小每轮发送给 LLM 的 tools 负载

配置完成后，重新启动 `main.py`，对应 server 暴露的工具会自动注册成安全 schema 名：

```text
mcp__<server>__<tool>
```

例如：

```text
mcp__mock__echo
```

### 当前边界

这一版先把“工具来源可插拔”打通，暂时还没有做：

- MCP server 生命周期管理优化
- 更细的 MCP 错误恢复
- MCP 工具级别的权限细分

但路由和命名规则已经固定，后续可以继续扩。

## 权限系统

权限系统现在不仅能判断本地工具，也能判断 MCP 工具。

### 本地工具

本地工具继续按原名匹配，例如：

- `read_file`
- `write_file`
- `team_run_agent`

### MCP 工具

MCP 工具在逻辑上仍按完整名匹配，例如：

- `mcp.github.search_prs`
- `mcp.figma.get_design_context`

虽然真正发给模型的是安全工具名，例如：

- `mcp__github__search_prs`
- `mcp__figma__get_design_context`

但权限系统会自动把安全名恢复成逻辑名，所以权限规则仍然推荐按点号写。

为了避免把某个 server 的规则写死在代码里，权限系统支持通过环境变量配置通配符：

- `PERMISSION_ALLOW_TOOL_PATTERNS`
- `PERMISSION_DENY_TOOL_PATTERNS`

规则格式是：

- 逗号分隔
- 支持 shell 风格通配符

例如：

```text
PERMISSION_ALLOW_TOOL_PATTERNS=mcp.github.*
```

或者更细一点：

```text
PERMISSION_ALLOW_TOOL_PATTERNS=mcp.github.get_*,mcp.github.list_*,mcp.github.search_*
```

这意味着后续如果你接入真实 GitHub MCP server，就可以直接按工具名前缀决定放行、拒绝或进入确认流程，而不需要再为每个 server 写一套单独逻辑。

## 运行模型

主循环大致是：

1. 接收用户输入
2. 构建本轮上下文
3. 调用 LLM
4. 如果模型请求工具，就执行工具并回填结果
5. 重复，直到得到最终回答

当前 runtime 内置了：

- 会话恢复
- 自动 compact
- 权限审查
- hook
- 错误恢复

## 分层记忆

项目采用分层记忆，而不是把所有内容塞进同一个 prompt。

### 1. 长期规则层

规则文件统一使用：

- `learnclaude.md`
- `learnclaude.local.md`

它们属于外置上下文，不代表模型已经“学会了”这些内容。

### 2. 长期经验层

长期经验放在：

```text
.memory/
  MEMORY.md
  topics/*.md
```

- `MEMORY.md` 是索引
- `topics/*.md` 是具体经验正文

系统会按需检索相关 topic，而不是每轮全量加载全部经验。

### 3. 会话记忆

主会话日志放在：

```text
.sessions/*.jsonl
```

启动时会从最近主会话恢复。默认恢复最近窗口；如果已有 compact 摘要，则优先使用“摘要 + 最近消息”的恢复方式。

### 4. 最近原始对话

最近未压缩的 `user / assistant` 对话额外保存在：

```text
.chat_history/recent_dialogue.jsonl
```

它主要服务于自动记忆提取，不直接作为主会话恢复源。

## 自动记忆

系统会在主回合结束后，基于最近对话和当前 memory 索引，尝试提取：

- 稳定用户偏好
- 项目约定
- 可复用经验
- 用户纠正过的反馈

写入前会做：

- normalize
- merge
- topic 复用

目的是避免把同义内容写成很多碎片文件。

## Subagent 与 Teammate

### Subagent

`task` 对应的是一次性子代理：

- fresh context
- 同步运行
- 跑完后只把结果返回给父 Agent

适合独立的小任务。

### Teammate

teammate 是持久 agent：

- 固定 `agent_id`
- 独立历史
- 独立 inbox
- 可跨多轮继续工作

状态落在：

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

现在实现的是更保守的版本：持久 teammate 可以在明确边界内自动领任务，但不会自己发明新任务，也不会无视显式消息和协议请求。

### 触发条件

只有同时满足这些条件，teammate 才会自动认领任务：

- `auto_pull_tasks = true`
- 当前 inbox 为空
- 当前没有待处理的 protocol request
- task graph 中存在符合自己角色的 ready task
- 该任务尚未被其他 teammate 认领

### 角色匹配

当前使用任务字段 `owner` 作为最小路由提示：

- `owner` 为空：任何角色都可认领
- `owner == agent_id`：只允许指定 teammate 认领
- `owner == role`：只允许对应角色认领

### 任务认领

任务图会记录：

- `claimed_by`
- `claimed_at`

也就是说，当前做的是带角色边界和 claim 保护的 worker 模式，而不是自由自治规划。

## Team 协议层

team 通信分成两层。

### 普通消息

适合：

- 讨论
- 提醒
- 补充说明

使用：

- `team_send_message`

### 协议消息

适合：

- 审批
- 关机请求
- 交接
- 签收
- 集成候选审查

使用：

- `team_send_protocol`
- `team_respond_protocol`
- `team_get_request`
- `team_list_requests`

每条协议请求都会持久化为 `.team/requests/<request_id>.json`。inbox 只负责投递，状态追踪走请求表。

当前已经实际使用到的 review 相关动作包括：

- `integration_request`
- `approved`
- `changes_requested`
- `rejected`

## Hook 与权限

hook 只承接横切逻辑，不接管主循环。

当前主要 hook 点：

- `before_llm_request`
- `before_tool_execute`
- `after_tool_execute`
- `before_compact`
- `after_compact`

当前接入的横切能力：

- 后台任务结果注入
- memory retrieval
- 路径规则激活
- 权限审查

## 错误处理与恢复

runtime 现在是恢复导向的，不只是“捕获异常”。

主循环显式维护这些运行状态：

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

## 任务图并发控制

任务图除了表达“做什么、谁在做、状态如何”，现在还承担最小一致性保护。

### 1. 乐观并发控制

每条任务新增了 `version` 字段。

含义是：

- 每次成功更新任务，`version` 都会递增
- 调用方可以先 `task_get`
- 再把读取到的 `version` 作为 `base_version` 带给 `task_update`

如果更新时发现当前任务版本已经不是 `base_version`，更新会被拒绝，并提示：

- 这条任务已经被其他 Agent 改过
- 需要先重读，再基于最新版本重试

这解决的是“我拿着旧快照把别人新改的内容覆盖掉”的问题。

### 2. claim / update 原子化

任务图不是简单的“读出来再写回”。

当前实现做了两层保护：

- `create` 使用全局锁，避免多个 Agent 同时分配出同一个 task id
- `claim` 和 `update` 使用单任务锁，在锁内重读最新文件再写回

另外，任务写回不是直接覆盖，而是：

1. 先写临时文件
2. 再 `os.replace(...)` 原子替换

这能降低：

- 两个 teammate 同时 claim 同一任务
- 两个更新互相覆盖
- 写入过程中留下半截 JSON

当前任务状态已经区分：

- `in_progress`：正在实现
- `integration_pending`：候选改动已在独立 worktree 中完成并提交审查，但还没进入主线
- `completed`：已经真正集成回主线
- `cancelled`：用户明确不要了
- `abandoned`：旧会话遗留的未完成任务，当前默认不继续

### 跨会话默认失活

任务图现在会记录：

- `createdInSession`
- `taskBatchId`

程序启动新会话时，会自动扫描旧任务：

- 旧会话里遗留的 `pending / in_progress / integration_pending`
- 默认都会被标记为 `abandoned`

这样做的目的不是删除任务，而是避免新会话在没有用户明确要求的情况下，自动继续上次做到一半的工作。

如果用户明确说：

- “继续上次任务”
- “恢复之前的任务”
- “接着做 task 2”

这时再使用 `task_restore` 把 `abandoned / cancelled` 任务恢复回 `pending` 或 `in_progress`。

### 3. 依赖关系

任务依赖仍然是静态边：

- `blockedBy` 只表示依赖关系
- 前置任务完成后不会删除这条边
- `ready / blocked` 是运行时派生状态

例如：

- `task 3 blockedBy=[2]`
- 只要 `task 2` 不是 `completed`
- `task 3` 就仍然是 `blocked`

并且任务图会校验：

- 依赖任务必须存在
- 任务不能依赖自己
- 依赖图不能形成环
- 被阻塞的任务不能直接切到 `in_progress`

### 4. 推荐更新方式

为了减少覆盖，推荐流程是：

1. `task_get`
2. 读取当前 `version`
3. 基于这个版本调用 `task_update(base_version=...)`

teammate 的系统提示词也已经按这个流程约束。

## Worktree 注册表

task graph 解决的是“任务控制面”，并不负责工作目录隔离。

所以项目又单独加了一层 worktree 注册表，用来回答：

- 这个任务在哪个独立工作区里执行
- 这个工作区属于哪个 agent
- 对应哪个 task
- 目录路径和分支名是什么

当前实现放在：

- [E:\github\learn_claude_code\agent_runtime\worktree.py](E:\github\learn_claude_code\agent_runtime\worktree.py)

状态目录默认是：

```text
.worktrees/
  registry/
  locks/
```

真正的 git worktree 默认创建在仓库同级目录下：

```text
../learn_claude_code_worktrees/
```

### 为什么单独做这一层

因为：

- task graph 负责“做什么、谁在做、状态如何”
- worktree registry 负责“在哪做、目录在哪、对应哪个任务”

这两层通过 `task_id` / `agent_id` 关联，但不混在一个结构里。

### 现在的运行方式

当持久 teammate 自动认领任务时：

1. 先 claim task
2. 再为该 task / agent 分配或复用一个 worktree
3. 把 worktree 路径写进注册表
4. teammate 本轮使用的文件工具和 shell 工具都会切到这个目录

也就是说，多个 teammate 即使同时工作，也不会默认都在主仓库目录里互相踩文件。

### PR 式集成流程

当前 worktree 还承担了最小的“候选变更 -> review -> integrate”流程，思路接近 PR / code review：

1. teammate 在独立 worktree 中完成改动
2. teammate 使用 `worktree_submit_for_review`
3. 系统把这项改动标成 `review_pending`，并把任务状态切到 `integration_pending`
4. 同时给 `lead` 发起一条 `integration_request`
5. lead 使用 `worktree_get_record / worktree_get_diff` 审查候选变更
6. lead 使用 `worktree_review_decision`
   - `approved`
   - `changes_requested`
   - `rejected`
7. `changes_requested / rejected` 会把任务退回 `pending`
8. 只有 `approved` 的候选变更，lead 才能使用 `worktree_integrate` 合回主仓库当前分支
9. 集成成功后，任务才会被标记为 `completed`

也就是说：

- teammate 负责产出候选改动
- lead 负责 review 和最后集成
- “完成任务”不等于“自动并入主线”

### 和乐观并发的关系

这两者不冲突，解决的是两类不同问题：

- worktree：解决代码工作区并发
- version + 锁：解决共享状态写入并发

所以当前设计是：

- 代码执行面隔离到不同 worktree
- 控制面状态仍然集中在 task graph / request record
- 集中状态继续靠版本和锁保护

## 主要工具

父 Agent 主要能力：

- 文件读写与编辑
- shell
- 后台任务
- todo
- task graph
- task restore / abandoned task 查询
- worktree review / integrate
- compact
- load_skill
- task
- team 系列工具

teammate 的工具集会更收敛，只保留适合长期协作的部分。

## 目录概览

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
  worktree.py
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
- `LLM_CONTEXT_WINDOW`
- `SESSION_RESUME_MAX_MESSAGES`
- `RECENT_DIALOGUE_MAX_MESSAGES`
- `MEMORY_DIALOGUE_LOOKBACK`
- `APPROVAL_TIMEOUT_SECONDS`

## 运行

```powershell
python main.py
```

## 当前阶段

这个项目目前已经具备：

- 本地交互式 coding agent
- 分层记忆
- session 恢复与 compact
- hook + permission
- subagent
- 持久 teammate
- team 协议层
- 恢复导向 runtime
- 带最小并发控制的持久化任务图
- 任务级 git worktree 注册表

还没有覆盖的方向包括：

- 更完整的多 Agent 工作区隔离
- 更强的任务调度与回收策略
- 更细的持久化一致性模型
