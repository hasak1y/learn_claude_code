"""轻量 Web UI：复用现有 runtime，为项目提供浏览器界面。"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from agent_runtime.agent import AgentLoop
from agent_runtime.background_jobs import BackgroundJobManager
from agent_runtime.compaction import ConversationCompactor
from agent_runtime.config import load_openai_compatible_configs
from agent_runtime.dialogue_history import RecentDialogueStore
from agent_runtime.hooks import HookManager
from agent_runtime.llm import FallbackLLMClient, OpenAICompatibleLLMClient
from agent_runtime.memory import AutoMemoryManager, LearnClaudeContextLoader
from agent_runtime.mcp_client import MCPClient, load_mcp_server_configs
from agent_runtime.permissions import PermissionPolicy
from agent_runtime.runtime_hooks import (
    BackgroundJobHook,
    MemoryRetrievalHook,
    PathScopedRuleHook,
    PermissionHook,
)
from agent_runtime.session_log import SessionLogger, load_latest_parent_history
from agent_runtime.skills import SkillRegistry
from agent_runtime.subagents import SubagentRunner
from agent_runtime.task_graph import TaskGraphManager
from agent_runtime.team import TeamAgentRunner, TeamManager
from agent_runtime.todo import TodoManager
from agent_runtime.tools.background_job import (
    BackgroundShellTool,
    GetBackgroundJobResultTool,
    ListBackgroundJobsTool,
)
from agent_runtime.tools.base import ToolRegistry
from agent_runtime.tools.bash import ShellTool
from agent_runtime.tools.compact import CompactTool
from agent_runtime.tools.edit_file import EditFileTool
from agent_runtime.tools.read_file import ReadFileTool
from agent_runtime.tools.skill import LoadSkillTool
from agent_runtime.tools.subagent import TaskTool
from agent_runtime.tools.task_graph import (
    CreateTaskTool,
    GetTaskTool,
    ListAbandonedTasksTool,
    ListAllTasksTool,
    ListBlockedTasksTool,
    ListCompletedTasksTool,
    ListReadyTasksTool,
    RestoreTasksTool,
    UpdateTaskTool,
)
from agent_runtime.tools.team import (
    GetTeamAgentTool,
    GetTeamRequestTool,
    ListTeamAgentsTool,
    ListTeamRequestsTool,
    PeekTeamInboxTool,
    RespondTeamProtocolTool,
    RunTeamAgentTool,
    SendTeamMessageTool,
    SendTeamProtocolTool,
    ShutdownTeamAgentTool,
    SpawnTeamAgentTool,
)
from agent_runtime.tools.todo import TodoTool
from agent_runtime.tools.worktree import (
    DecideWorktreeReviewTool,
    GetWorktreeDiffTool,
    GetWorktreeRecordTool,
    IntegrateWorktreeTool,
    ListWorktreesTool,
    SubmitWorktreeForReviewTool,
)
from agent_runtime.tools.write_file import WriteFileTool
from agent_runtime.types import ConversationMessage, ToolCall
from agent_runtime.worktree import WorktreeManager
from main import (
    MEMORY_DIALOGUE_LOOKBACK,
    RECENT_DIALOGUE_MAX_MESSAGES,
    SESSION_RESUME_MAX_MESSAGES,
    build_startup_context_messages,
    build_subagent_system_prompt,
    build_system_prompt,
    build_team_agent_base_prompt,
    ensure_startup_context_messages,
)


HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>learn-claude-code UI</title>
  <style>
    :root { --bg:#0a0c10; --panel:#11151c; --panel-soft:#151a22; --border:#232b36; --text:#eef2f7; --muted:#8d9aac; --accent:#56b6ff; --accent-soft:rgba(86,182,255,.12); --shadow:0 10px 30px rgba(0,0,0,.28); --radius:18px; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; background:radial-gradient(circle at top left, rgba(86,182,255,.1), transparent 28%), radial-gradient(circle at top right, rgba(126,231,135,.08), transparent 22%), var(--bg); color:var(--text); height:100vh; overflow:hidden; }
    .app { display:grid; grid-template-columns:330px minmax(0,1fr); height:100vh; gap:16px; padding:16px; }
    .sidebar,.main { min-height:0; background:rgba(17,21,28,.92); border:1px solid var(--border); border-radius:var(--radius); box-shadow:var(--shadow); backdrop-filter:blur(10px); }
    .sidebar { display:flex; flex-direction:column; overflow:hidden; }
    .sidebar-header,.main-header { padding:18px 18px 14px; border-bottom:1px solid var(--border); }
    .brand { font-size:18px; font-weight:700; }
    .sub { margin-top:6px; color:var(--muted); font-size:12px; line-height:1.5; }
    .sidebar-scroll { overflow:auto; padding:14px; display:grid; gap:12px; }
    .card { background:var(--panel-soft); border:1px solid var(--border); border-radius:14px; padding:14px; }
    .card-title { font-size:13px; color:var(--muted); margin-bottom:10px; text-transform:uppercase; letter-spacing:.08em; }
    .metrics { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; }
    .metric { background:rgba(255,255,255,.03); border:1px solid rgba(255,255,255,.05); border-radius:12px; padding:10px; }
    .metric-value { font-size:18px; font-weight:700; }
    .metric-label { margin-top:4px; color:var(--muted); font-size:11px; }
    .pill-list,.line-list { display:flex; flex-wrap:wrap; gap:8px; }
    .pill { padding:6px 10px; border-radius:999px; background:rgba(86,182,255,.08); border:1px solid rgba(86,182,255,.14); color:#cfe9ff; font-size:12px; line-height:1.3; }
    .item { display:grid; gap:4px; padding:10px 12px; border-radius:12px; border:1px solid rgba(255,255,255,.05); background:rgba(255,255,255,.02); }
    .item strong { font-size:13px; } .item small { color:var(--muted); line-height:1.5; }
    .main { display:grid; grid-template-rows:auto minmax(0,1fr) auto; overflow:hidden; }
    .main-header { display:flex; justify-content:space-between; align-items:center; gap:12px; }
    .header-actions { display:flex; gap:8px; flex-wrap:wrap; }
    button { border:1px solid var(--border); background:rgba(255,255,255,.03); color:var(--text); border-radius:12px; padding:10px 14px; cursor:pointer; transition:.15s ease; font-size:13px; }
    button:hover { border-color:rgba(86,182,255,.45); background:var(--accent-soft); }
    button.primary { background:linear-gradient(135deg,#2d7cff,#45c2ff); border-color:transparent; color:#fff; }
    .chat { overflow:auto; padding:22px 22px 8px; display:grid; gap:14px; }
    .message { max-width:900px; border-radius:16px; padding:14px 16px; border:1px solid var(--border); white-space:pre-wrap; line-height:1.65; box-shadow:0 6px 18px rgba(0,0,0,.14); }
    .message.user { margin-left:auto; background:rgba(86,182,255,.12); border-color:rgba(86,182,255,.2); }
    .message.assistant { background:rgba(255,255,255,.03); }
    .message.tool { background:rgba(242,204,96,.08); border-color:rgba(242,204,96,.18); }
    .message.system { background:rgba(126,231,135,.08); border-color:rgba(126,231,135,.18); }
    .role { font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); margin-bottom:8px; }
    .composer { border-top:1px solid var(--border); padding:16px; display:grid; gap:10px; background:rgba(17,21,28,.96); }
    .composer-row { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:10px; }
    textarea { width:100%; min-height:84px; resize:vertical; border-radius:14px; border:1px solid var(--border); background:#0d1117; color:var(--text); padding:14px; font:inherit; line-height:1.6; }
    .hint,.status-line { color:var(--muted); font-size:12px; line-height:1.5; }
    .hint { display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; }
    @media (max-width:1100px) { .app { grid-template-columns:1fr; } .sidebar { display:none; } }
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <div class="sidebar-header"><div class="brand">learn-claude-code UI</div><div class="sub">类似 OpenCode 的项目控制台：聊天、工具、任务、teammate、worktree 同屏查看。</div></div>
      <div class="sidebar-scroll">
        <section class="card"><div class="card-title">概览</div><div class="metrics" id="metrics"></div></section>
        <section class="card"><div class="card-title">Teammates</div><div id="team-list" class="line-list"></div></section>
        <section class="card"><div class="card-title">Tasks</div><div id="task-list" class="line-list"></div></section>
        <section class="card"><div class="card-title">Worktrees</div><div id="worktree-list" class="line-list"></div></section>
        <section class="card"><div class="card-title">Tools</div><div id="tool-list" class="pill-list"></div></section>
      </div>
    </aside>
    <main class="main">
      <div class="main-header">
        <div><div class="brand">对话控制台</div><div class="sub" id="runtime-meta"></div></div>
        <div class="header-actions"><button id="refresh-btn">刷新状态</button><button id="new-thread-btn">新线程</button></div>
      </div>
      <div class="chat" id="chat"></div>
      <div class="composer">
        <div class="status-line" id="status-line">就绪</div>
        <div class="composer-row"><textarea id="input" placeholder="输入你的需求，例如：创建一个持久 teammate，agent_id 叫 coder1，role 是 coder。"></textarea><button class="primary" id="send-btn">发送</button></div>
        <div class="hint"><span>Web UI 复用当前项目 runtime，不是单独的 demo agent。</span><span>纯工具结果、task/team/worktree 状态会自动同步到侧栏。</span></div>
      </div>
    </main>
  </div>
  <script>
    const chat=document.getElementById("chat"),input=document.getElementById("input"),sendBtn=document.getElementById("send-btn"),refreshBtn=document.getElementById("refresh-btn"),newThreadBtn=document.getElementById("new-thread-btn"),statusLine=document.getElementById("status-line");
    const escapeHtml=(t)=>t.replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
    function appendMessage(item){const block=document.createElement("div");block.className=`message ${item.role}`;block.innerHTML=`<div class="role">${escapeHtml(item.label||item.role)}</div><div>${escapeHtml(item.content||"")}</div>`;chat.appendChild(block);chat.scrollTop=chat.scrollHeight;}
    function renderMessages(items,replace=false){if(replace)chat.innerHTML="";items.forEach(appendMessage);}
    function renderList(containerId,items,mapFn){const container=document.getElementById(containerId);container.innerHTML="";if(!items.length){const empty=document.createElement("div");empty.className="item";empty.innerHTML="<small>暂无</small>";container.appendChild(empty);return;}items.forEach((item)=>{const el=document.createElement("div");el.className="item";el.innerHTML=mapFn(item);container.appendChild(el);});}
    function flattenTaskGroups(groups){const result=[];Object.entries(groups).forEach(([status,items])=>{(items||[]).forEach((task)=>result.push({...task,derivedStatus:status}));});return result;}
    function renderState(payload){document.getElementById("runtime-meta").textContent=`session=${payload.meta.sessionId} · permission=${payload.meta.permissionMode} · tools=${payload.tools.length}`;const taskItems=flattenTaskGroups(payload.tasks);const activeTasks=taskItems.filter((item)=>["ready","blocked","in_progress","integration_pending"].includes(item.derivedStatus)).length;document.getElementById("metrics").innerHTML=[{value:payload.team.length,label:"Teammates"},{value:activeTasks,label:"活跃任务"},{value:payload.worktrees.length,label:"Worktrees"}].map((item)=>`<div class="metric"><div class="metric-value">${item.value}</div><div class="metric-label">${item.label}</div></div>`).join("");
      renderList("team-list",payload.team,(item)=>`<strong>${escapeHtml(item.agentId)}</strong><small>role=${escapeHtml(item.role)} · status=${escapeHtml(item.status)}</small><small>auto_pull=${item.autoPullTasks} · pending=${item.pendingMessages}/${item.pendingRequests}</small>`);
      renderList("task-list",taskItems,(item)=>`<strong>task ${item.id} · ${escapeHtml(item.subject)}</strong><small>${escapeHtml(item.derivedStatus)} · version=${item.version} · owner=${escapeHtml(item.owner||"-")}</small>`);
      renderList("worktree-list",payload.worktrees,(item)=>`<strong>task ${item.taskId} → ${escapeHtml(item.agentId)}</strong><small>${escapeHtml(item.status)} · ${escapeHtml(item.branch)}</small>`);
      const toolContainer=document.getElementById("tool-list");toolContainer.innerHTML="";payload.tools.forEach((toolName)=>{const el=document.createElement("span");el.className="pill";el.textContent=toolName;toolContainer.appendChild(el);});}
    async function callApi(url,method="GET",body=null){const response=await fetch(url,{method,headers:{"Content-Type":"application/json"},body:body?JSON.stringify(body):null});const data=await response.json();if(!response.ok)throw new Error(data.error||"请求失败");return data;}
    async function bootstrap(){statusLine.textContent="加载中...";const data=await callApi("/api/bootstrap");renderMessages(data.messages,true);renderState(data.state);statusLine.textContent="就绪";}
    async function sendMessage(){const value=input.value.trim();if(!value)return;input.value="";statusLine.textContent="运行中...";sendBtn.disabled=true;appendMessage({role:"user",label:"user",content:value});try{const data=await callApi("/api/message","POST",{message:value});renderMessages(data.messages);renderState(data.state);if(data.autoMemory)appendMessage({role:"system",label:"auto_memory",content:data.autoMemory});statusLine.textContent="已完成";}catch(error){appendMessage({role:"system",label:"error",content:String(error.message||error)});statusLine.textContent="失败";}finally{sendBtn.disabled=false;}}
    sendBtn.addEventListener("click",sendMessage); input.addEventListener("keydown",(event)=>{if((event.ctrlKey||event.metaKey)&&event.key==="Enter")sendMessage();});
    refreshBtn.addEventListener("click",async()=>{statusLine.textContent="刷新中...";try{const data=await callApi("/api/state");renderState(data.state);statusLine.textContent="已刷新";}catch(error){appendMessage({role:"system",label:"error",content:String(error.message||error)});statusLine.textContent="刷新失败";}});
    newThreadBtn.addEventListener("click",async()=>{statusLine.textContent="重置中...";try{const data=await callApi("/api/reset","POST",{});renderMessages(data.messages,true);renderState(data.state);statusLine.textContent="已创建新线程";}catch(error){appendMessage({role:"system",label:"error",content:String(error.message||error)});statusLine.textContent="重置失败";}});
    bootstrap().catch((error)=>{appendMessage({role:"system",label:"error",content:String(error.message||error)});statusLine.textContent="初始化失败";});
  </script>
</body>
</html>"""


def _build_llm_client(endpoint_configs: list[Any]) -> OpenAICompatibleLLMClient | FallbackLLMClient:
    clients = [OpenAICompatibleLLMClient(config) for config in endpoint_configs]
    if len(clients) == 1:
        return clients[0]
    return FallbackLLMClient(clients)


def _deny_ui_approval(_tool_call: object, _decision: object) -> bool:
    """Web UI 暂不支持交互式权限确认，保守处理为拒绝。"""

    return False


def _serialize_tool_calls(tool_calls: list[ToolCall]) -> str:
    names = [tool_call.name for tool_call in tool_calls]
    if not names:
        return ""
    return "调用工具：\n" + "\n".join(f"- {name}" for name in names)


def _serialize_message(message: ConversationMessage) -> dict[str, str] | None:
    if message.role == "assistant" and not message.content and message.tool_calls:
        return {
            "role": "tool",
            "label": "tool_call",
            "content": _serialize_tool_calls(message.tool_calls),
        }

    if message.role not in {"user", "assistant", "tool"}:
        return None

    label = message.role
    if message.role == "tool" and message.name:
        label = f"tool · {message.name}"

    return {
        "role": message.role,
        "label": label,
        "content": message.content,
    }


@dataclass(slots=True)
class RuntimeSession:
    agent: AgentLoop
    history: list[ConversationMessage]
    session_id: str
    session_logger: SessionLogger
    recent_dialogue_store: RecentDialogueStore
    memory_manager: AutoMemoryManager
    llm_client: OpenAICompatibleLLMClient | FallbackLLMClient
    tool_registry: ToolRegistry
    team_manager: TeamManager
    task_graph_manager: TaskGraphManager
    worktree_manager: WorktreeManager
    permission_mode: str
    startup_messages: list[dict[str, str]]


class WebAgentApp:
    """持有一份长期 runtime，会话通过浏览器 API 复用。"""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self._lock = threading.Lock()
        self._session = self._build_session()

    def _build_session(self) -> RuntimeSession:
        cwd = self.repo_root
        endpoint_configs = load_openai_compatible_configs()
        config = endpoint_configs[0]
        llm_client = _build_llm_client(endpoint_configs)
        permission_mode = os.getenv("PERMISSION_MODE", "dangerous")
        permission_policy = PermissionPolicy(mode=permission_mode)  # type: ignore[arg-type]
        skill_registry = SkillRegistry(root=str(cwd / "skills"))
        todo_manager = TodoManager(reminder_threshold=3)
        task_graph_manager = TaskGraphManager(tasks_dir=cwd / ".tasks")
        worktree_manager = WorktreeManager(
            repo_root=cwd,
            registry_root=cwd / ".worktrees",
            worktree_base_dir=cwd.parent / f"{cwd.name}_worktrees",
        )
        background_job_manager = BackgroundJobManager(cwd=str(cwd))
        team_manager = TeamManager(root=cwd / ".team")
        mcp_server_configs = load_mcp_server_configs(repo_root=cwd)
        mcp_client = MCPClient(mcp_server_configs) if mcp_server_configs else None
        sessions_dir = cwd / ".sessions"
        resume_result = load_latest_parent_history(
            sessions_dir,
            max_messages=SESSION_RESUME_MAX_MESSAGES,
            prefer_summary=True,
        )

        managed_rules_path_raw = os.getenv("LEARNCLAUDE_MANAGED_PATH", "").strip()
        managed_rules_path = Path(managed_rules_path_raw) if managed_rules_path_raw else None
        learnclaude_loader = LearnClaudeContextLoader(cwd=cwd, managed_path=managed_rules_path)
        learnclaude_text = learnclaude_loader.render_for_system_prompt()
        memory_manager = AutoMemoryManager(root=cwd / ".memory")
        memory_index_slice = memory_manager.render_startup_index_slice()
        recent_dialogue_store = RecentDialogueStore(
            path=cwd / ".chat_history" / "recent_dialogue.jsonl",
            max_messages=RECENT_DIALOGUE_MAX_MESSAGES,
        )

        session_id = f"ui_session_{int(time.time() * 1000)}"
        task_graph_manager.set_runtime_context(session_id=session_id)
        session_logger = SessionLogger(
            session_id=session_id,
            path=sessions_dir / f"{session_id}.jsonl",
        )
        task_graph_manager.abandon_stale_tasks(current_session_id=session_id)
        auto_compact_threshold = max(1, int(config.context_window * 0.85))
        compactor = ConversationCompactor(
            transcript_dir=cwd / ".transcripts",
            auto_compact_token_threshold=auto_compact_threshold,
        )

        startup_context_messages = build_startup_context_messages(
            learnclaude_text=learnclaude_text,
            memory_index_slice=memory_index_slice,
        )

        subagent_runner = SubagentRunner(
            llm_client_factory=lambda: _build_llm_client(endpoint_configs),
            child_tool_registry_factory=lambda: ToolRegistry(
                [
                    CompactTool(),
                    LoadSkillTool(skill_registry=skill_registry),
                    ReadFileTool(cwd=str(cwd)),
                    WriteFileTool(cwd=str(cwd)),
                    EditFileTool(cwd=str(cwd)),
                    ShellTool(cwd=str(cwd)),
                ],
                mcp_client=mcp_client,
            ),
            child_system_prompt=build_subagent_system_prompt(skill_registry),
            child_startup_messages=startup_context_messages,
            child_max_steps=12,
            compactor=compactor,
            session_logger=session_logger,
        )

        team_runner = TeamAgentRunner(
            team_manager=team_manager,
            llm_client_factory=lambda: _build_llm_client(endpoint_configs),
            tool_registry_factory=lambda agent_id, workspace_cwd=None: ToolRegistry(
                [
                    CompactTool(),
                    LoadSkillTool(skill_registry=skill_registry),
                    SendTeamMessageTool(manager=team_manager, sender_id=agent_id),
                    SendTeamProtocolTool(manager=team_manager, sender_id=agent_id),
                    RespondTeamProtocolTool(manager=team_manager, sender_id=agent_id),
                    ListTeamAgentsTool(manager=team_manager),
                    GetTeamAgentTool(manager=team_manager),
                    ListTeamRequestsTool(manager=team_manager),
                    GetTeamRequestTool(manager=team_manager),
                    GetTaskTool(manager=task_graph_manager),
                    UpdateTaskTool(manager=task_graph_manager),
                    ListReadyTasksTool(manager=task_graph_manager),
                    ListAllTasksTool(manager=task_graph_manager),
                    GetWorktreeRecordTool(manager=worktree_manager),
                    GetWorktreeDiffTool(manager=worktree_manager),
                    SubmitWorktreeForReviewTool(
                        worktree_manager=worktree_manager,
                        team_manager=team_manager,
                        task_graph_manager=task_graph_manager,
                        sender_id=agent_id,
                    ),
                    ReadFileTool(cwd=workspace_cwd or str(cwd)),
                    WriteFileTool(cwd=workspace_cwd or str(cwd)),
                    EditFileTool(cwd=workspace_cwd or str(cwd)),
                    ShellTool(cwd=workspace_cwd or str(cwd)),
                ],
                mcp_client=mcp_client,
            ),
            base_system_prompt_builder=lambda _agent_id, _workspace_cwd=None: build_team_agent_base_prompt(
                skill_registry,
                workspace_cwd=_workspace_cwd,
            ),
            task_graph_manager=task_graph_manager,
            worktree_manager=worktree_manager,
            startup_messages=startup_context_messages,
            max_steps=12,
            compactor=compactor,
        )

        tool_registry = ToolRegistry(
            [
                TodoTool(todo_manager=todo_manager),
                CreateTaskTool(manager=task_graph_manager),
                UpdateTaskTool(manager=task_graph_manager),
                GetTaskTool(manager=task_graph_manager),
                ListAllTasksTool(manager=task_graph_manager),
                ListReadyTasksTool(manager=task_graph_manager),
                ListBlockedTasksTool(manager=task_graph_manager),
                ListCompletedTasksTool(manager=task_graph_manager),
                ListAbandonedTasksTool(manager=task_graph_manager),
                RestoreTasksTool(manager=task_graph_manager),
                ListWorktreesTool(manager=worktree_manager),
                GetWorktreeRecordTool(manager=worktree_manager),
                GetWorktreeDiffTool(manager=worktree_manager),
                SubmitWorktreeForReviewTool(
                    worktree_manager=worktree_manager,
                    team_manager=team_manager,
                    task_graph_manager=task_graph_manager,
                    sender_id="lead",
                ),
                DecideWorktreeReviewTool(
                    worktree_manager=worktree_manager,
                    team_manager=team_manager,
                    task_graph_manager=task_graph_manager,
                    sender_id="lead",
                ),
                IntegrateWorktreeTool(manager=worktree_manager, task_graph_manager=task_graph_manager),
                BackgroundShellTool(manager=background_job_manager),
                ListBackgroundJobsTool(manager=background_job_manager),
                GetBackgroundJobResultTool(manager=background_job_manager),
                SpawnTeamAgentTool(manager=team_manager),
                ListTeamAgentsTool(manager=team_manager),
                GetTeamAgentTool(manager=team_manager),
                SendTeamMessageTool(manager=team_manager, sender_id="lead"),
                SendTeamProtocolTool(manager=team_manager, sender_id="lead"),
                RespondTeamProtocolTool(manager=team_manager, sender_id="lead"),
                PeekTeamInboxTool(manager=team_manager),
                ListTeamRequestsTool(manager=team_manager),
                GetTeamRequestTool(manager=team_manager),
                RunTeamAgentTool(runner=team_runner),
                ShutdownTeamAgentTool(manager=team_manager),
                TaskTool(runner=subagent_runner),
                CompactTool(),
                LoadSkillTool(skill_registry=skill_registry),
                ReadFileTool(cwd=str(cwd)),
                WriteFileTool(cwd=str(cwd)),
                EditFileTool(cwd=str(cwd)),
                ShellTool(cwd=str(cwd)),
            ],
            mcp_client=mcp_client,
        )

        hook_manager = HookManager(
            {
                "before_llm_request": [
                    BackgroundJobHook(background_job_manager),
                    MemoryRetrievalHook(memory_manager, llm_client),
                ],
                "before_tool_execute": [
                    PermissionHook(permission_policy, _deny_ui_approval)
                ],
                "after_tool_execute": [
                    PathScopedRuleHook(learnclaude_loader, cwd)
                ],
            }
        )

        agent = AgentLoop(
            llm_client=llm_client,
            tool_registry=tool_registry,
            system_prompt=build_system_prompt(skill_registry),
            max_steps=12,
            echo_tool_calls=False,
            todo_manager=todo_manager,
            task_graph_manager=task_graph_manager,
            background_job_manager=background_job_manager,
            compactor=compactor,
            session_logger=session_logger,
            log_scope="parent",
            hook_manager=hook_manager,
        )

        history: list[ConversationMessage] = list(resume_result.history)
        ensure_startup_context_messages(
            history=history,
            startup_messages=startup_context_messages,
            session_logger=session_logger,
            scope="parent:startup",
        )

        startup_messages_payload: list[dict[str, str]] = []
        if resume_result.path is not None and history:
            startup_messages_payload.append(
                {
                    "role": "system",
                    "label": "session_resume",
                    "content": (
                        f"已从 {resume_result.path.name} 恢复主历史，模式={resume_result.mode}，"
                        f"共 {len(history)} 条消息。"
                    ),
                }
            )
        if learnclaude_text:
            startup_messages_payload.append(
                {"role": "system", "label": "learnclaude", "content": "已加载长期规则层。"}
            )
        if len(endpoint_configs) > 1:
            startup_messages_payload.append(
                {
                    "role": "system",
                    "label": "llm",
                    "content": f"已加载 {len(endpoint_configs)} 个 LLM endpoint，当前启用自动 failover。",
                }
            )
        if mcp_client is not None:
            startup_messages_payload.append(
                {
                    "role": "system",
                    "label": "mcp",
                    "content": f"已加载 {len(mcp_server_configs)} 个 MCP server。",
                }
            )

        return RuntimeSession(
            agent=agent,
            history=history,
            session_id=session_id,
            session_logger=session_logger,
            recent_dialogue_store=recent_dialogue_store,
            memory_manager=memory_manager,
            llm_client=llm_client,
            tool_registry=tool_registry,
            team_manager=team_manager,
            task_graph_manager=task_graph_manager,
            worktree_manager=worktree_manager,
            permission_mode=permission_mode,
            startup_messages=startup_messages_payload,
        )

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self._session = self._build_session()
            return self._bootstrap_payload_locked()

    def bootstrap_payload(self) -> dict[str, Any]:
        with self._lock:
            return self._bootstrap_payload_locked()

    def state_payload(self) -> dict[str, Any]:
        with self._lock:
            return {"state": self._state_payload()}

    def _state_payload(self) -> dict[str, Any]:
        return {
            "meta": {
                "sessionId": self._session.session_id,
                "permissionMode": self._session.permission_mode,
            },
            "tools": self._session.tool_registry.get_tool_display_names(),
            "tasks": self._session.task_graph_manager.snapshot(),
            "team": self._session.team_manager.snapshot_agents(),
            "requests": self._session.team_manager.snapshot_requests(),
            "worktrees": self._session.worktree_manager.snapshot_records(),
        }

    def _bootstrap_payload_locked(self) -> dict[str, Any]:
        visible_messages = [
            item
            for item in (_serialize_message(message) for message in self._session.history)
            if item is not None
        ]
        return {
            "messages": self._session.startup_messages + visible_messages,
            "state": self._state_payload(),
        }

    def send_message(self, text: str) -> dict[str, Any]:
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("消息不能为空。")

        with self._lock:
            start_index = len(self._session.history)
            user_message = ConversationMessage(role="user", content=clean_text)
            self._session.history.append(user_message)
            self._session.session_logger.append_message(user_message, scope="parent")

            try:
                run_result = self._session.agent.run(self._session.history)
            except RuntimeError as exc:
                raise RuntimeError(str(exc)) from exc

            self._session.recent_dialogue_store.append_message(
                role="user",
                content=clean_text,
                session_id=self._session.session_id,
            )
            if run_result.final_text:
                self._session.recent_dialogue_store.append_message(
                    role="assistant",
                    content=run_result.final_text,
                    session_id=self._session.session_id,
                )

            auto_memory_message = self._session.memory_manager.maybe_update_from_dialogue(
                llm_client=self._session.llm_client,
                recent_dialogue=self._session.recent_dialogue_store.load_recent(MEMORY_DIALOGUE_LOOKBACK),
            )

            new_messages = [
                item
                for item in (
                    _serialize_message(message)
                    for message in self._session.history[start_index:]
                )
                if item is not None
            ]
            return {
                "messages": new_messages,
                "autoMemory": auto_memory_message,
                "state": self._state_payload(),
            }


class _AppHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], app: WebAgentApp) -> None:
        self.app = app
        super().__init__(server_address, _RequestHandler)


class _RequestHandler(BaseHTTPRequestHandler):
    server: _AppHTTPServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/bootstrap":
            self._write_json(self.server.app.bootstrap_payload())
            return

        if self.path == "/api/state":
            self._write_json(self.server.app.state_payload())
            return

        self._write_json({"error": "Not Found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self._write_json({"error": "请求体不是合法 JSON。"}, status=HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/message":
            try:
                result = self.server.app.send_message(str(payload.get("message", "")))
            except Exception as exc:
                self._write_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._write_json(result)
            return

        if self.path == "/api/reset":
            try:
                result = self.server.app.reset()
            except Exception as exc:
                self._write_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._write_json(result)
            return

        self._write_json({"error": "Not Found"}, status=HTTPStatus.NOT_FOUND)

    def _write_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_ui_server(*, repo_root: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    """启动 Web UI。"""

    app = WebAgentApp(repo_root=repo_root)
    server = _AppHTTPServer((host, port), app=app)
    print(f"[ui] Web UI 已启动：http://{host}:{port}")
    server.serve_forever()
