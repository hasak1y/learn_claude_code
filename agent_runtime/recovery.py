"""运行时错误分类与恢复决策。

这一层不直接执行恢复动作，只负责回答两个问题：
1. 当前错误属于哪一类、影响范围有多大
2. 下一步应该重试、压缩恢复、记录后继续，还是直接失败
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal


RuntimeErrorCategory = Literal[
    "transient",
    "context_overflow",
    "permission",
    "protocol",
    "non_transient",
    "unknown",
]

RuntimeErrorScope = Literal["fatal", "step", "auxiliary"]
RuntimeStage = Literal["llm", "tool", "hook", "compact", "unknown"]
RecoveryAction = Literal[
    "retry",
    "compact_and_resume",
    "record_and_continue",
    "fail",
]
AgentRuntimeState = Literal[
    "RUNNING",
    "RETRYING",
    "RESUMING",
    "COMPACTING",
    "FAILED",
    "COMPLETED",
]


@dataclass(slots=True)
class RuntimeErrorInfo:
    """运行时错误的结构化描述。"""

    stage: RuntimeStage
    category: RuntimeErrorCategory
    scope: RuntimeErrorScope
    message: str
    original_exception: Exception
    retryable: bool


@dataclass(slots=True)
class RecoveryDecision:
    """恢复策略决策结果。"""

    action: RecoveryAction
    reason: str


def classify_error(
    exc: Exception,
    *,
    stage: RuntimeStage,
    hook_event: str | None = None,
    is_manual_compact: bool = False,
) -> RuntimeErrorInfo:
    """把原始异常收敛成运行时可消费的错误信息。"""

    message = str(exc).strip() or exc.__class__.__name__
    lowered = message.lower()

    category: RuntimeErrorCategory = "unknown"
    retryable = False

    if any(
        keyword in lowered
        for keyword in (
            "timeout",
            "timed out",
            "connection reset",
            "remote end closed",
            "temporarily unavailable",
            "temporarily",
            "disconnected",
            "incompleteread",
            "connection aborted",
            "connection refused",
            "bad gateway",
            "gateway timeout",
            "service unavailable",
            "http 502",
            "http 503",
            "http 504",
        )
    ):
        category = "transient"
        retryable = True
    elif any(
        keyword in lowered
        for keyword in (
            "context length",
            "context window",
            "maximum context",
            "token limit",
            "too many tokens",
            "request too large",
        )
    ):
        category = "context_overflow"
    elif any(
        keyword in lowered
        for keyword in (
            "permission",
            "forbidden",
            "unauthorized",
            "access denied",
            "http 401",
            "http 403",
        )
    ):
        category = "permission"
    elif isinstance(exc, (ValueError, KeyError, TypeError, json.JSONDecodeError)):
        category = "protocol"
    else:
        category = "non_transient"

    if stage == "tool":
        scope: RuntimeErrorScope = "step"
    elif stage == "compact":
        scope = "step" if is_manual_compact else "auxiliary"
    elif stage == "hook":
        scope = "step" if hook_event == "before_tool_execute" else "auxiliary"
    else:
        scope = "fatal"

    return RuntimeErrorInfo(
        stage=stage,
        category=category,
        scope=scope,
        message=message,
        original_exception=exc,
        retryable=retryable,
    )


def decide_recovery(
    error: RuntimeErrorInfo,
    *,
    attempt: int,
    max_attempts: int,
    has_compactor: bool,
    compact_already_attempted: bool = False,
) -> RecoveryDecision:
    """根据错误类型和当前尝试次数决定下一步恢复动作。"""

    if error.retryable and attempt < max_attempts:
        return RecoveryDecision(
            action="retry",
            reason="检测到临时性错误，优先进行有限次数的重试。",
        )

    if (
        error.stage == "llm"
        and error.category == "context_overflow"
        and has_compactor
        and not compact_already_attempted
    ):
        return RecoveryDecision(
            action="compact_and_resume",
            reason="检测到上下文溢出，先压缩历史再继续当前任务。",
        )

    if error.scope == "auxiliary":
        return RecoveryDecision(
            action="record_and_continue",
            reason="当前错误只影响附属能力，记录后继续主流程。",
        )

    if error.scope == "step" and error.category in {"transient", "non_transient", "protocol"}:
        return RecoveryDecision(
            action="record_and_continue",
            reason="当前错误只影响当前步骤，转成步骤级错误信息并继续。",
        )

    return RecoveryDecision(
        action="fail",
        reason="当前错误无法安全恢复，应直接终止本轮运行。",
    )

