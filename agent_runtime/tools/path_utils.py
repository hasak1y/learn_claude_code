"""文件类工具共享的路径辅助函数。

这层的目标很明确：
- 所有文件类工具都通过同一套逻辑解析路径
- 相对路径统一相对于当前工作区
- 最终路径必须落在工作区内部，防止路径逃逸

这套约束目前主要服务于：
- `read_file`
- `write_file`
- `edit_file`
"""

from __future__ import annotations

from pathlib import Path


def resolve_workspace_path(workspace: str | Path, raw_path: str) -> Path:
    """把输入路径解析为工作区内的绝对路径。

    规则：
    - 相对路径会相对于工作区解析
    - 绝对路径也允许传入，但最终仍必须位于工作区内部
    - 如果最终路径逃逸出工作区，直接抛出异常
    """

    workspace_path = Path(workspace).resolve()
    candidate = Path(raw_path)

    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (workspace_path / candidate).resolve()

    if not resolved.is_relative_to(workspace_path):
        raise ValueError(f"路径越界，不能访问工作区之外的文件：{raw_path}")

    return resolved


def display_workspace_path(workspace: str | Path, target_path: Path) -> str:
    """优先用相对工作区的路径做展示。"""

    workspace_path = Path(workspace).resolve()
    resolved_target = target_path.resolve()

    try:
        return str(resolved_target.relative_to(workspace_path))
    except ValueError:
        return str(resolved_target)
