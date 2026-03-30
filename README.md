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
- 一个 `bash` 工具
- 一个命令行入口

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
  config.py
  types.py
  llm/
    base.py
    openai_compatible.py
  tools/
    base.py
    bash.py
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

## 项目约定

- 这个项目中的注释、文档字符串和 `README` 统一使用中文
- 标识符和接口字段是否保持英文，以代码可读性和协议兼容性为准
