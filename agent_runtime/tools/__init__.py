"""工具包。

这里故意不再聚合导入所有具体工具类。
原因是子代理管理会依赖 `AgentLoop`，而 `AgentLoop` 又依赖 `tools.base`，
如果在包初始化时把所有工具都提前导入，很容易形成循环导入。

实际使用时请直接从具体子模块导入工具类，例如：
- `agent_runtime.tools.base`
- `agent_runtime.tools.bash`
- `agent_runtime.tools.read_file`
"""
