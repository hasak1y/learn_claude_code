"""后台任务管理。

这一层把“长时间 shell 命令”从阻塞式工具调用里拆出来：

- 主线程仍然负责 AgentLoop 和 LLM 调用
- 后台线程负责执行长时间 shell 子进程
- 任务完成后把结果放进完成队列
- 主线程在下一次调用 LLM 前统一注入结果

当前版本刻意保持最小：
- 只支持后台 shell 命令
- 不做流式输出
- 不做会话恢复
- 不做持久化队列
"""

from __future__ import annotations

import locale
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from itertools import count
from typing import Literal


BackgroundJobStatus = Literal["running", "completed", "failed"]


@dataclass(slots=True)
class BackgroundJob:
    """单个后台任务。"""

    job_id: str
    command: str
    cwd: str
    status: BackgroundJobStatus
    created_at: float
    updated_at: float
    output: str | None = None
    error: str | None = None
    return_code: int | None = None
    thread: threading.Thread | None = field(default=None, repr=False)


@dataclass(slots=True)
class BackgroundJobEvent:
    """主线程消费的后台任务完成事件。"""

    job_id: str
    status: BackgroundJobStatus
    command: str
    output: str


class BackgroundJobManager:
    """管理后台 shell 任务。"""

    def __init__(self, cwd: str | None = None, timeout_seconds: int = 1800) -> None:
        self.cwd = cwd or os.getcwd()
        self.timeout_seconds = timeout_seconds
        self._jobs: dict[str, BackgroundJob] = {}
        self._lock = threading.Lock()
        self._completed_events: queue.Queue[BackgroundJobEvent] = queue.Queue()
        self._id_counter = count(1)

    @staticmethod
    def _decode_output(data: bytes) -> str:
        """稳妥解码后台子进程输出，避免系统默认编码不匹配时直接崩溃。"""

        if not data:
            return ""

        candidates = ["utf-8", locale.getpreferredencoding(False), "gbk"]
        for encoding in candidates:
            if not encoding:
                continue
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    def spawn_shell(self, command: str) -> str:
        """启动一个后台 shell 任务，并立即返回 job_id。"""

        clean_command = command.strip()
        if not clean_command:
            return "错误：command 不能为空"

        dangerous_fragments = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
        if any(fragment in clean_command for fragment in dangerous_fragments):
            return "错误：危险命令已被拦截"

        job_id = f"job-{next(self._id_counter):04d}"
        now = time.time()
        job = BackgroundJob(
            job_id=job_id,
            command=clean_command,
            cwd=self.cwd,
            status="running",
            created_at=now,
            updated_at=now,
        )

        thread = threading.Thread(
            target=self._run_job,
            args=(job_id,),
            daemon=True,
            name=job_id,
        )
        job.thread = thread

        with self._lock:
            self._jobs[job_id] = job

        thread.start()
        return (
            f"job_id: {job_id}\n"
            "status: running\n"
            "说明：后台任务已启动，后续完成结果会在下一次 LLM 调用前自动注入。"
        )

    def list_jobs(self) -> str:
        """列出当前后台任务。"""

        with self._lock:
            jobs = list(self._jobs.values())

        if not jobs:
            return "当前没有后台任务。"

        jobs.sort(key=lambda item: item.created_at)
        lines = ["当前后台任务："]
        for job in jobs:
            preview = job.command.replace("\n", " ").strip()
            if len(preview) > 60:
                preview = preview[:57] + "..."
            lines.append(f"- {job.job_id} | {job.status} | {preview}")
        return "\n".join(lines)

    def get_result(self, job_id: str) -> str:
        """查看单个后台任务结果。"""

        with self._lock:
            job = self._jobs.get(job_id)

        if job is None:
            return f"错误：不存在后台任务 {job_id}"

        if job.status == "running":
            return f"job_id: {job.job_id}\nstatus: running"

        if job.status == "completed":
            return (
                f"job_id: {job.job_id}\n"
                "status: completed\n"
                f"output:\n{job.output or '（无输出）'}"
            )

        return (
            f"job_id: {job.job_id}\n"
            "status: failed\n"
            f"output:\n{job.output or job.error or '（无输出）'}"
        )

    def drain_completed_events(self) -> list[BackgroundJobEvent]:
        """取出所有尚未注入主循环的完成事件。"""

        events: list[BackgroundJobEvent] = []
        while True:
            try:
                events.append(self._completed_events.get_nowait())
            except queue.Empty:
                break
        return events

    def _run_job(self, job_id: str) -> None:
        """在线程中执行后台任务。"""

        with self._lock:
            job = self._jobs[job_id]
            command = job.command
            cwd = job.cwd

        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=False,
                timeout=self.timeout_seconds,
            )
            stdout_text = self._decode_output(completed.stdout)
            stderr_text = self._decode_output(completed.stderr)
            raw_output = (stdout_text + stderr_text).strip() or "（无输出）"
            output = raw_output[:50000]
            status: BackgroundJobStatus = "completed" if completed.returncode == 0 else "failed"
            error = None if completed.returncode == 0 else f"退出码：{completed.returncode}"

        except subprocess.TimeoutExpired:
            status = "failed"
            output = f"错误：后台任务执行超时，已超过 {self.timeout_seconds} 秒"
            error = output
            completed = None
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            output = f"错误：后台任务执行失败：{exc}"
            error = output
            completed = None

        with self._lock:
            job = self._jobs[job_id]
            job.status = status
            job.output = output
            job.error = error
            job.updated_at = time.time()
            job.return_code = None if completed is None else completed.returncode

        self._completed_events.put(
            BackgroundJobEvent(
                job_id=job_id,
                status=status,
                command=command,
                output=output,
            )
        )
