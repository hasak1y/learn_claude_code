---
version: 1
kind: auto-memory-index
---

# MEMORY

该文件是长期经验索引，不直接保存完整经验内容。
每条记录使用单行结构，供运行时按需检索。

- path: topics/epistemic-status-labeling.md | name: Epistemic Status Labeling | type: feedback | description: 用户纠正：凡属推断或未验证内容，必须明确标注，不得表述为既成事实。
- path: topics/hook-boundary.md | name: Hook Boundary | type: project | description: 项目中 hook 与主循环/manager 的职责边界，并附代码落点示例
- path: topics/memory-layer-design.md | name: Memory Layer Design | type: reference | description: 当前项目的四层记忆系统设计，包括规则层、经验层、按需加载层和执行控制层
- path: topics/test-output-convention.md | name: Test Output Directory Convention | type: project | description: 用户约定后续测试输出代码统一放在 test_output/ 目录，可在其下按需分层子目录。
- path: topics/user-response-style.md | name: User Response Style | type: user | description: 用户偏好先给结论、再展开解释，默认用中文且不要过短回复
