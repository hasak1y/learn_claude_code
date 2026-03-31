# 最小 Agent 运行时

这个项目是一个面向 Claude Code 风格 Agent 的最小起点，重点不是一开始就做全功能，而是先把最核心的运行时骨架搭起来，并且保证后续容易扩展。

第一版只关注这条核心链路：

1. 接收用户输入
2. 把消息历史和工具定义发送给 LLM
3. 让 LLM 自己判断是否需要调用工具
4. 执行工具
5. 把工具结果回填给 LLM
6. 重复以上过程，直到 LLM 不再请求工具

## 当前范围

- 一个可复用的 Agent 循环
- 一个 OpenAI-compatible LLM 适配器
- 一个 `shell` 工具
- 一个 `todo` 工具
- 一组 `task_*` 任务图工具
- 一个 `compact` 工具
- 一个 `load_skill` 工具
- 一个 `read_file` 工具
- 一个 `write_file` 工具
- 一个 `edit_file` 工具
- 一个父 Agent 专属的 `task` 子代理工具
- 一个命令行入口
- 一个本地 `skills/` 目录
- 一个本地 `.transcripts/` 历史转储目录
- 一个本地 `.sessions/` 追加会话日志目录
- 一个本地 `.tasks/` 持久化任务图目录

当前有意不实现的部分：

- 持久工作目录
- 完整的沙箱 / 权限系统
- 更强的异常恢复能力
- 流式 UI
- OpenAI-compatible 之外的多厂商适配器

## 项目结构

```text
main.py
agent_runtime/
  agent.py
  compaction.py
  config.py
  session_log.py
  skills.py
  subagents.py
  task_graph.py
  todo.py
  types.py
  llm/
    base.py
    openai_compatible.py
  tools/
    base.py
    bash.py
    edit_file.py
    path_utils.py
    read_file.py
    skill.py
    subagent.py
    task_graph.py
    todo.py
    write_file.py
skills/
  git/
    SKILL.md
  test/
    SKILL.md
```

## 环境变量

必填：

- `LLM_MODEL`
- `LLM_API_KEY`

可选：

- `LLM_BASE_URL`，默认值为 `https://api.openai.com/v1`
- `LLM_TIMEOUT_SECONDS`，默认值为 `120`
- `LLM_MAX_TOKENS`，默认值为 `2000`
- `LLM_TEMPERATURE`，默认值为 `0`

你也可以在项目根目录放一个本地 `.env` 文件，写入同名配置项。

示例：

```env
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=your_api_key_here
LLM_BASE_URL=https://api.openai.com/v1
```

## 运行方式

```bash
python main.py
```

命令行交互约定：

- 直接回车：忽略本次输入，继续等待
- 输入 `q`、`quit` 或 `exit`：退出会话

## 会话日志

当前项目会在会话开始时创建一个新的：

```text
.sessions/<session_id>.jsonl
```

行为：

- 每当新的 `ConversationMessage` 进入会话历史，就会追加写入一行
- 一行一条消息
- 当前只做追加，不做恢复、不做索引、不做数据库

这和 `.transcripts/` 的区别是：

- `.sessions/`：全程追加日志，记录平时每条进入历史的消息
- `.transcripts/`：只在真正 compact 时保存压缩前的完整历史快照

## 任务图

当前项目已经支持持久化任务图，适合处理有依赖关系或可并行推进的复杂任务。

存储方式：

- 每个任务一个 JSON 文件
- 文件位置在 `.tasks/task_<id>.json`
- `blockedBy` 表示静态依赖边，不会因为前置任务完成而被删除

状态与派生语义：

- 持久化状态只存 `pending / in_progress / completed`
- `ready` 和 `blocked` 是运行时派生状态
- `pending` 且所有依赖都已完成时，派生状态为 `ready`
- `pending` 且仍有未完成依赖时，派生状态为 `blocked`

当前任务图工具包括：

- `task_create`
- `task_update`
- `task_get`
- `task_list_all`
- `task_list_ready`
- `task_list_blocked`
- `task_list_completed`

适用建议：

- 简单、线性的短任务：继续用 `todo`
- 有前置依赖、后续解锁或并行结构的复杂任务：优先用任务图

## 三层压缩

当前项目已经接入三层上下文压缩机制。

### 第一层：micro_compact

这一层在每次调用模型前执行，但不会修改原始历史。

行为：

- 只对“发给模型的请求视图”做轻量压缩
- 较旧且较长的 `tool` 消息会被替换成短占位摘要
- 最近若干条工具结果仍保留原文

目标：

- 减少旧工具输出持续污染上下文
- 又不破坏完整历史，便于后续真正做摘要压缩

### 第二层：auto_compact

当会话估算 token 超过阈值时，运行时会自动触发真正压缩。

行为：

- 先把压缩前的完整历史保存到 `.transcripts/`
- 再让模型生成结构化续航摘要
- 最后用“摘要 + 最近若干条真实消息”替换活跃历史

当前默认参数偏向“方便本地测试”：

- 父 Agent 默认 `max_steps = 12`
- `auto_compact_token_threshold = 12000`

结构化摘要至少包含：

- 当前目标
- 已完成事项
- 未完成事项
- 当前 Todo 状态
- 当前任务图状态
- 已加载 Skills
- 关键文件与修改
- 关键工具结果
- 重要约束与风险
- 最近一次用户要求

### 第三层：manual compact

模型也可以显式调用 `compact` 工具。

行为：

- 与 `auto_compact` 复用同一套压缩逻辑
- 适合在任务阶段切换、上下文已经明显变长、或模型自己感觉该收束时使用

当前原则：

- `micro_compact` 只压缩请求视图
- `auto_compact` / `manual compact` 才会真正替换活跃历史
- 完整历史通过 `.transcripts/` 保存在磁盘上

## 这次踩过的坑

这一节记录的是当前项目在接第三方 OpenAI-compatible 服务时，已经实际踩过并确认过的问题。

### 1. `LLM_BASE_URL` 不一定能随便写根域名

有些服务虽然首页是根域名，例如：

```env
LLM_BASE_URL=https://mzlone.top
```

但真正可用的聊天接口仍然是：

```text
/v1/chat/completions
```

如果运行时直接拼成 `/chat/completions`，就会和实际服务路径不一致。

当前项目已经兼容这两种写法：

- `LLM_BASE_URL=https://api.openai.com/v1`
- `LLM_BASE_URL=https://mzlone.top`

运行时最终都会正确访问 `.../v1/chat/completions`。

### 2. 某些中转服务会拦截默认 Python 请求头

这次接入的服务对“默认 Python 请求”有明显的网关拦截行为。
同样的接口，如果请求头特征不对，可能直接返回：

```text
HTTP 403
error code: 1010
```

当前项目已经在 [openai_compatible.py](E:\github\learn_claude_code\agent_runtime\llm\openai_compatible.py) 里补了默认请求头，尤其是：

- `User-Agent: api-client/3.0`

这个值是参考可正常访问的测试项目对齐出来的。

### 3. 模型名大小写可能严格区分

这次服务里：

- `gpt-5.4` 可用
- `GPT-5.4` 会失败

也就是说，模型名不能想当然地大小写随便写。
如果模型名不对，服务端可能返回类似：

```text
No available channel for model GPT-5.4 ...
```

当前 `.env` 示例已经改成：

```env
LLM_MODEL=gpt-5.4
```

如果你怀疑模型名写错了，优先去服务端的模型列表接口核对，不要凭感觉填写。

### 4. 普通聊天时不要发送空工具定义

有些 OpenAI-compatible 服务对下面这种请求兼容性不好：

```json
{
  "tools": [],
  "tool_choice": "auto"
}
```

即使你只是想发一个普通聊天请求，也可能因此失败。

当前运行时已经改成：

- 只有在确实存在工具时，才发送 `tools`
- 没有工具时，不发送 `tools` 和 `tool_choice`

### 5. 某些服务返回 `tool_calls: null`

标准兼容接口里，“没有工具调用”可能返回：

```json
"tool_calls": null
```

而不是空数组 `[]`。

如果代码只按列表处理，就会在解析响应时崩掉。
当前运行时已经兼容：

- `tool_calls: null`
- `tool_calls: []`

### 6. 包内模块不能直接当脚本运行

像下面这个文件：

- [openai_compatible.py](E:\github\learn_claude_code\agent_runtime\llm\openai_compatible.py)

它是包内模块，不是程序入口。
如果直接运行这个文件，会因为相对导入而报错。

正确入口是：

```bash
python main.py
```

如果只是想检查配置和接口，不要直接跑底层模块，建议单独写最小请求脚本或直接用你已有的 API 测试项目验证。

## 为什么这样拆

这个项目最关键的设计选择，是把下面三件事拆开：

- 循环层负责控制流程
- LLM 适配层负责和模型提供方通信
- 工具注册表负责执行工具

这样拆开之后，后面继续加功能会顺很多，比如：

- 增加 Anthropic 适配器
- 增加更多工具
- 增加持久化状态
- 增加文件编辑类工具
- 增加审批和安全层
- 增加真正的终端或 Web UI

## 后续规划

### Todo 与任务图提醒机制

当前项目已经支持 `todo` 和任务图两种任务跟踪方式。

设计约束如下：

- `todo` 工具每次提交的是完整清单，而不是局部 patch
- 同一时刻最多只能有一个 `in_progress`
- Agent 会在内存中维护当前 todo 状态
- 复杂任务如果存在依赖关系，应优先使用任务图
- 如果模型连续多轮没有更新任何任务跟踪信息，运行时会临时注入 reminder

当前 reminder 机制是轻量的：

- 只在调用模型前临时注入
- 不会把 reminder 永久写入正式对话历史
- reminder 文本里会附带当前 todo 状态和任务图摘要

当前会被视为“已更新任务跟踪”的操作包括：

- `todo`
- `task_create`
- `task_update`

这样做的目标是让复杂任务更稳定，不容易在多轮工具调用中丢步骤，也能同时覆盖简单清单和依赖任务图两种工作流。

### 子代理接口

当前项目已经改成阻塞式子代理分发，更接近 Claude Code 风格。

父 Agent 比子 Agent 多一个专属工具：

- `task(prompt)`
  把一个边界清晰、相对独立的子任务同步委派给 fresh context 的子代理

当前实现特点：

- 子代理使用 fresh context 启动，不继承父对话历史
- 子代理只拥有基础文件 / shell 工具，不允许再创建新的子代理
- 父 Agent 继续保留 `todo`、`read_file`、`write_file`、`edit_file` 和 `shell`
- 父 Agent 只是比子 Agent 多一个 `task` 工具，而不是只保留 `task`
- `task` 会阻塞等待子代理跑完，再把最终文本结果同步返回给父 Agent
- 子代理内部的消息历史和工具轨迹会被丢弃，不会污染父上下文
- `AgentLoop.run()` 现在返回 `AgentRunResult`，而不是裸消息

这套设计适合：

- 把一个相对独立的阅读、搜索、总结或验证任务单独分发出去
- 让父 Agent 拿到子任务结论后继续当前主线推理

这套设计暂时不追求：

- 后台并发运行多个子代理
- `task_id` 管理
- 终端侧的后台任务状态面板

### Skill 机制

当前项目已经支持本地 skill 的“两层加载”机制。

第一层是常驻索引：

- 启动时扫描项目根目录下的 `skills/` 目录
- 每个 skill 目录下放一个 `SKILL.md`
- 运行时只把 skill 的简短索引放进 system prompt

第二层是按需正文：

- 父 Agent 和子 Agent 都拥有 `load_skill(name)` 工具
- 模型只有在确实需要某个方法论时，才调用 `load_skill`
- `load_skill` 会读取对应 `SKILL.md` 全文，并作为 tool result 回填给模型

当前设计特点：

- skill 更像“懒加载的提示词包”，不是普通业务工具
- 现有 AgentLoop 不需要为 skill 单独改结构
- 父 Agent 和子 Agent 共享同一份 skill 目录，但各自独立决定是否加载
- 当前示例内置了 `git` 和 `test` 两个 skill

最小 skill 文件格式支持：

- YAML frontmatter 中的 `name` 和 `description`
- 正文部分写具体工作步骤、约束和建议

示例：

```md
---
name: git
description: Git 工作流与提交前检查方法
---

# Git Skill
...
```

### Shell 模式化权限

当前项目已经有 `shell` 工具，但后续不应该一直维持“一个全能开关”式的执行模型。
更实用的方向是给 `shell` 做模式化权限分级，让 Agent 在不同风险等级下运行。

计划中的三个权限档位：

- `read_only`
  只允许查看类命令。
  典型命令包括：`dir`、`type`、`rg`、`git status`、`git diff`

- `dev_safe`
  允许正常开发所需的测试、构建和脚本执行，但仍然限制高风险系统操作。
  典型命令包括：`python`、`pytest`、`npm test`、`npm run build`

- `dangerous`
  面向高风险操作，默认不开放，后续最好配合确认机制使用。
  这类操作通常包括删除、覆盖、系统级安装、进程操作、网络下载执行等

这三个档位的划分原则不是按命令名字硬分，而是按风险和副作用范围划分：

- `read_only`：只读信息
- `dev_safe`：允许项目内可控副作用
- `dangerous`：可能带来系统级或不可逆副作用

这部分当前还没有接入代码，暂时只是设计约束。
后续如果实现，会把它下沉到 `shell` 工具的策略层，而不是散落在 prompt 或临时黑名单里。

## 项目约定

- 这个项目中的注释、文档字符串和 `README` 统一使用中文
- 标识符和接口字段是否保持英文，以代码可读性和协议兼容性为准
