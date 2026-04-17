---
name: Memory Layer Design
description: 当前项目的四层记忆系统设计，包括规则层、经验层、按需加载层和执行控制层
type: reference
---

当前项目的记忆系统分为四层：

1. 长期规则层
- 来源是 learnclaude.md / learnclaude.local.md
- 通过原始文本直接注入系统提示词
- 属于外置上下文，不代表模型权重已经学会

2. 长期经验层
- 来源是 .memory/MEMORY.md 和 topics/*.md
- MEMORY.md 只保存索引
- topic 文件保存详细经验正文

3. 按需加载层
- 每轮用户请求前，先基于 MEMORY.md 索引做少量 topic 检索
- 再把选中的 topic 正文注入本轮 request context
- 子目录 learnclaude 规则在访问对应路径后才激活

4. 执行控制层
- settings / hooks / permissions / model config
- 和记忆有关，但不等于记忆本身

实现原则：

- 会话恢复、原始聊天记录、长期经验索引分开存放
- 规则层不做结构化强约束，只做上下文注入
- 自动记忆写入只看索引摘要和最近对话，不全量扫描所有 memory 正文
