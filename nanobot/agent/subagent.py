"""Subagent manager for background task execution."""

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import urlparse

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.file_state import FileStates
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import AgentDefaults, ToolsConfig
from nanobot.providers.base import LLMProvider
from nanobot.security.project_context import current_project_context
from nanobot.security.workspace_access import (
    WorkspaceScope,
    bind_workspace_scope,
    reset_workspace_scope,
    workspace_sandbox_status,
)
from nanobot.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from nanobot.observability.trace_collector import TraceCollector

_EXPERT_TEAM_MAX_ITERATIONS = 100
_EXPERT_TEAM_MEMBER_TIMEOUT_S = 540
_EXPERT_TEAM_MEMBER_ARTIFACT_DIR = Path("reports") / ".team-runs"


@dataclass(slots=True)
class SubagentStatus:
    """Real-time status of a running subagent."""

    task_id: str
    label: str
    task_description: str
    started_at: float          # time.monotonic()
    phase: str = "initializing"  # initializing | awaiting_tools | tools_completed | final_response | done | error
    iteration: int = 0
    tool_events: list = field(default_factory=list)   # [{name, status, detail}, ...]
    completed_tool_events: list = field(default_factory=list)  # survives cancellation/timeout
    usage: dict = field(default_factory=dict)          # token usage
    stop_reason: str | None = None
    error: str | None = None


@dataclass(slots=True)
class WorkflowSubagentResult:
    """Terminal result returned directly to a runtime-owned graph."""

    task_id: str
    label: str
    status: str
    content: str


def _expert_team_member_runtime(
    expert_team: dict[str, Any] | None,
    label: str,
) -> dict[str, int]:
    """Return trusted, normalized limits for one expert-team member.

    Team bindings are normalized by the WebUI resource loader. Keeping the
    lookup here intentionally opt-in preserves the existing runtime behavior
    for every team that does not declare ``member_runtime``.
    """

    raw = expert_team.get("member_runtime") if isinstance(expert_team, dict) else None
    if not isinstance(raw, dict):
        return {}
    bounds = {
        "max_iterations": (4, _EXPERT_TEAM_MAX_ITERATIONS),
        "timeout_seconds": (30, _EXPERT_TEAM_MEMBER_TIMEOUT_S),
        "max_retries": (0, 1),
    }

    def _limits(source: dict[str, Any]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for key, (minimum, maximum) in bounds.items():
            value = source.get(key)
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            normalized[key] = max(minimum, min(maximum, value))
        return normalized

    limits = _limits(raw)
    raw_members = raw.get("members")
    member = raw_members.get(label) if isinstance(raw_members, dict) else None
    if isinstance(member, dict):
        limits.update(_limits(member))
    return limits


class _SubagentHook(AgentHook):
    """Hook for subagent execution — logs tool calls and updates status."""

    def __init__(
        self,
        task_id: str,
        status: SubagentStatus | None = None,
        on_activity: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self._task_id = task_id
        self._status = status
        self._on_activity = on_activity

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        for tool_call in context.tool_calls:
            if tool_call.id in context.hidden_tool_call_ids:
                continue
            args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
            logger.debug(
                "Subagent [{}] executing: {} with arguments: {}",
                self._task_id, tool_call.name, args_str,
            )
            if self._on_activity is not None:
                await self._on_activity(_subagent_activity(tool_call.name, tool_call.arguments))

    async def after_iteration(self, context: AgentHookContext) -> None:
        if self._status is None:
            return
        self._status.iteration = context.iteration
        self._status.tool_events = list(context.tool_events)
        self._status.usage = dict(context.usage)
        if context.error:
            self._status.error = str(context.error)
        if self._on_activity is not None:
            failure = next(
                (
                    event for event in reversed(context.tool_events)
                    if isinstance(event, dict) and event.get("status") == "error"
                ),
                None,
            )
            if failure is not None:
                name = str(failure.get("name") or "数据源")
                if failure.get("detail") == "asset-research source priority blocked":
                    await self._on_activity("已阻止低优先级公开检索，正在先查询结构化金融数据源")
                else:
                    await self._on_activity(
                        f"{_friendly_tool_name(name)}未返回有效结果，正在换源重试"
                    )


def _friendly_tool_name(name: str) -> str:
    compact = name.lower()
    if compact in {"web_search", "search_web"}:
        return "公开资料检索"
    if compact in {"web_fetch", "fetch_url"}:
        return "网页读取"
    if compact in {"exec", "run_shell_command"}:
        return "结构化数据查询"
    if compact.startswith("mcp_juyuan_"):
        return "聚源结构化数据查询"
    if compact.startswith("mcp_caihui_mcp_"):
        return "财汇结构化数据查询"
    if compact.startswith("mcp_hexin-ifind-ds-"):
        return "同花顺 iFinD 结构化数据查询"
    if compact.startswith("mcp_anysearch_"):
        return "AnySearch 公开资料查询"
    return "当前查询"


def _subagent_activity(name: str, arguments: Any) -> str:
    args = arguments if isinstance(arguments, dict) else {}
    compact = name.lower()
    query = str(args.get("query") or args.get("q") or "").strip()
    if compact in {"web_search", "search_web"}:
        return f"正在检索公开资料：{query[:72]}" if query else "正在检索公开资料"
    if compact in {"web_fetch", "fetch_url"}:
        url = str(args.get("url") or "").strip()
        host = urlparse(url).hostname if url else None
        return f"正在读取网页：{host}" if host else "正在读取网页资料"
    if compact in {"read_file", "read"}:
        path = str(args.get("path") or args.get("file_path") or "").strip()
        if "ifind-finance-data" in path:
            return "正在加载同花顺 iFinD 金融数据能力"
        return f"正在读取资料：{Path(path).name}" if path else "正在读取研究资料"
    if compact in {"exec", "run_shell_command"}:
        command = str(args.get("command") or args.get("cmd") or "")
        if "ifind-finance-data" in command or "51ifind" in command:
            match = re.search(
                r"""["']query["']\s*:\s*["']([^"']+)["']""",
                command,
            )
            query = match.group(1).strip() if match else ""
            return (
                f"正在查询同花顺 iFinD：{query[:72]}"
                if query
                else "正在查询同花顺 iFinD 结构化金融数据"
            )
        return "正在处理研究数据"
    if compact.startswith("mcp_juyuan_"):
        return (
            f"正在查询聚源金融数据：{query[:72]}"
            if query
            else "正在查询聚源结构化金融数据"
        )
    if compact.startswith("mcp_caihui_mcp_"):
        return (
            f"正在查询财汇金融数据：{query[:72]}"
            if query
            else "正在查询财汇结构化金融数据"
        )
    if compact.startswith("mcp_hexin-ifind-ds-"):
        return (
            f"正在查询同花顺 iFinD：{query[:72]}"
            if query
            else "正在查询同花顺 iFinD 结构化金融数据"
        )
    if compact.startswith("mcp_anysearch_"):
        return (
            f"正在通过 AnySearch 补充资料：{query[:72]}"
            if query
            else "正在通过 AnySearch 补充公开资料"
        )
    return f"正在执行：{name}"


class SubagentManager:
    """Manages background subagent execution."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        max_tool_result_chars: int,
        model: str | None = None,
        tools_config: ToolsConfig | None = None,
        restrict_to_workspace: bool = False,
        disabled_skills: list[str] | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        llm_wall_timeout_for_session: Callable[[str | None], float | None] | None = None,
        parent_tools: ToolRegistry | None = None,
        trace_collector: "TraceCollector | None" = None,
    ):
        defaults = AgentDefaults()
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.tools_config = tools_config or ToolsConfig()
        self.max_tool_result_chars = max_tool_result_chars
        self.restrict_to_workspace = restrict_to_workspace
        self.disabled_skills = set(disabled_skills or [])
        self.max_iterations = (
            max_iterations
            if max_iterations is not None
            else defaults.max_tool_iterations
        )
        self.max_concurrent_subagents = (
            max_concurrent_subagents
            if max_concurrent_subagents is not None
            else defaults.max_concurrent_subagents
        )
        self.runner = AgentRunner(provider, trace_collector=trace_collector)
        self.trace_collector = trace_collector
        self.parent_tools = parent_tools
        self._llm_wall_timeout_for_session = llm_wall_timeout_for_session
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_statuses: dict[str, SubagentStatus] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}
        self._workflow_task_ids: set[str] = set()
        self._workflow_completion_futures: dict[
            str, asyncio.Future[WorkflowSubagentResult]
        ] = {}

    def _subagent_tools_config(self) -> ToolsConfig:
        """Build a ToolsConfig scoped for subagent use."""
        return ToolsConfig(
            exec=self.tools_config.exec,
            web=self.tools_config.web,
            file=self.tools_config.file,
            restrict_to_workspace=self.restrict_to_workspace,
        )

    def _build_tools(
        self,
        workspace: Path | None = None,
        tools_config: ToolsConfig | None = None,
        expert_team: dict[str, Any] | None = None,
    ) -> ToolRegistry:
        """Build isolated native tools plus explicitly bound team MCP tools."""
        root = self.workspace if workspace is None else workspace
        registry = ToolRegistry()
        cfg = tools_config if tools_config is not None else self._subagent_tools_config()
        ctx = ToolContext(
            config=cfg,
            workspace=str(root.resolve()),
            file_state_store=FileStates(),
            workspace_sandbox=workspace_sandbox_status(
                restrict_to_workspace=cfg.restrict_to_workspace,
                workspace=root,
            ),
        )
        ToolLoader().load(ctx, registry, scope="subagent")
        raw_presets = expert_team.get("mcp_presets") if isinstance(expert_team, dict) else None
        if self.parent_tools is not None and isinstance(raw_presets, list):
            prefixes = {
                f"mcp_{str(item.get('name')).strip().lower()}_"
                for item in raw_presets
                if isinstance(item, dict)
                and item.get("configured") is True
                and str(item.get("name") or "").strip()
            }
            for name in self.parent_tools.tool_names:
                if any(name.startswith(prefix) for prefix in prefixes):
                    tool = self.parent_tools.get(name)
                    if tool is not None:
                        registry.register(tool)
        return registry

    @staticmethod
    def _workflow_member_tools(
        registry: ToolRegistry,
        expert_team: dict[str, Any] | None,
    ) -> ToolRegistry:
        """Limit fixed asset-workflow branches to bound MCP and web fallback tools."""

        raw_presets = (
            expert_team.get("mcp_presets")
            if isinstance(expert_team, dict)
            else None
        )
        prefixes = tuple(
            f"mcp_{str(item.get('name') or '').strip().lower()}_"
            for item in raw_presets or []
            if isinstance(item, dict)
            and item.get("configured") is True
            and str(item.get("name") or "").strip()
        )
        scoped = ToolRegistry()
        for name in registry.tool_names:
            if name not in {"web_search", "web_fetch"} and not name.startswith(prefixes):
                continue
            tool = registry.get(name)
            if tool is not None:
                scoped.register(tool)
        return scoped

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        self.provider = provider
        self.model = model
        self.runner.provider = provider

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
        expert_team: dict[str, Any] | None = None,
        expert_team_run_id: str | None = None,
        _workflow_owned: bool = False,
    ) -> str:
        """Spawn a subagent to execute a task in the background."""
        project_context = current_project_context()
        if project_context is not None:
            if workspace_scope is None:
                return "Error: project-scoped subagents require an inherited workspace scope"
            if (
                workspace_scope.project_path.expanduser().resolve(strict=False)
                != project_context.root_path.expanduser().resolve(strict=False)
            ):
                return "Error: subagent workspace does not match the parent project"
        task_id = str(uuid.uuid4())[:8]
        child_session_key: str | None = None
        state_store = None
        if project_context is not None:
            from nanobot.storage.state import StateStore, StateStoreError

            try:
                state_store = StateStore(
                    self.workspace / ".nanobot" / "state.sqlite",
                    default_workspace=self.workspace,
                )
                child_session_key = f"subagent:{task_id}"
                state_store.bind_session(
                    child_session_key,
                    project_context.project_id,
                    title=label or task[:80],
                    metadata={
                        "parent_session_key": project_context.session_key,
                        "project_id": project_context.project_id,
                    },
                    artifact_index_initialized=True,
                )
                parent_turn_id = state_store.active_turn_id(project_context.session_key)
                if parent_turn_id is not None:
                    state_store.record_agent_edge(
                        project_id=project_context.project_id,
                        parent_session_key=project_context.session_key,
                        child_session_key=child_session_key,
                        parent_turn_id=parent_turn_id,
                    )
            except StateStoreError:
                logger.exception("Failed to persist project-scoped subagent relationship")
                return "Error: subagent project relationship could not be persisted"
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {
            "channel": origin_channel,
            "chat_id": origin_chat_id,
            "session_key": session_key,
            **(
                {"project_id": project_context.project_id}
                if project_context is not None
                else {}
            ),
        }

        status = SubagentStatus(
            task_id=task_id,
            label=display_label,
            task_description=task,
            started_at=time.monotonic(),
        )
        self._task_statuses[task_id] = status
        if _workflow_owned:
            self._workflow_task_ids.add(task_id)
            self._workflow_completion_futures[task_id] = (
                asyncio.get_running_loop().create_future()
            )

        bg_task = asyncio.create_task(
            self._run_subagent(
                task_id,
                task,
                display_label,
                origin,
                status,
                origin_message_id,
                temperature,
                workspace_scope,
                expert_team,
                expert_team_run_id,
            )
        )
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            workflow_future = self._workflow_completion_futures.get(task_id)
            if workflow_future is not None and not workflow_future.done():
                if bg_task.cancelled():
                    workflow_future.set_result(WorkflowSubagentResult(
                        task_id=task_id,
                        label=display_label,
                        status="cancelled",
                        content="The workflow member was cancelled before delivery.",
                    ))
                else:
                    task_error = bg_task.exception()
                    workflow_future.set_result(WorkflowSubagentResult(
                        task_id=task_id,
                        label=display_label,
                        status="error",
                        content=(
                            f"Error: {task_error}"
                            if task_error is not None
                            else "The workflow member ended without a terminal result."
                        ),
                    ))
            if state_store is not None and child_session_key is not None:
                child_status = (
                    "cancelled"
                    if bg_task.cancelled()
                    else "failed" if (
                        bg_task.exception() is not None
                        or status.phase == "error"
                        or status.stop_reason in {"error", "tool_error", "timeout"}
                    )
                    else "completed"
                )
                state_store.complete_session(
                    child_session_key,
                    status=child_status,
                )
            self._running_tasks.pop(task_id, None)
            self._task_statuses.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]
            self._workflow_task_ids.discard(task_id)

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        if _workflow_owned:
            return task_id
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        status: SubagentStatus,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
        expert_team: dict[str, Any] | None = None,
        expert_team_run_id: str | None = None,
        retry_count: int = 0,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)
        member_runtime = _expert_team_member_runtime(expert_team, label)
        await self._publish_team_member_update(
            origin,
            expert_team,
            expert_team_run_id,
            task_id=task_id,
            label=label,
            status="running",
            activity="自动重试已启动，正在改用结构化数据源和替代查询" if retry_count else None,
        )

        async def _on_checkpoint(payload: dict) -> None:
            status.phase = payload.get("phase", status.phase)
            status.iteration = payload.get("iteration", status.iteration)
            if not member_runtime:
                return
            completed = payload.get("completed_tool_results")
            if not isinstance(completed, list):
                return
            by_call_id = {
                str(item.get("tool_call_id") or ""): item
                for item in completed
                if isinstance(item, dict)
            }
            assistant = payload.get("assistant_message")
            tool_calls = (
                assistant.get("tool_calls")
                if isinstance(assistant, dict)
                else None
            )
            if not isinstance(tool_calls, list):
                return
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or "")
                result = by_call_id.get(call_id)
                function = call.get("function")
                name = (
                    str(function.get("name") or "")
                    if isinstance(function, dict)
                    else ""
                )
                if not name or not isinstance(result, dict):
                    continue
                detail = str(result.get("content") or "").replace("\n", " ").strip()
                if len(detail) > 240:
                    detail = detail[:240] + "..."
                event = {
                    "name": name,
                    "status": "error" if detail.startswith("Error") else "ok",
                    "detail": detail or "(empty)",
                }
                if event not in status.completed_tool_events:
                    status.completed_tool_events.append(event)

        async def _on_activity(activity: str) -> None:
            await self._publish_team_member_update(
                origin,
                expert_team,
                expert_team_run_id,
                task_id=task_id,
                label=label,
                status="running",
                activity=activity,
            )

        try:
            root = workspace_scope.project_path if workspace_scope is not None else self.workspace
            cfg = None
            if workspace_scope is not None:
                cfg = self._subagent_tools_config()
                cfg.restrict_to_workspace = workspace_scope.restrict_to_workspace
            tools = self._build_tools(
                workspace=root,
                tools_config=cfg,
                expert_team=expert_team,
            )
            workflow_owned = task_id in self._workflow_task_ids
            if workflow_owned:
                tools = self._workflow_member_tools(tools, expert_team)
            system_prompt = self._build_subagent_prompt(
                workspace=root,
                include_skills=not workflow_owned,
            )
            if expert_team is not None:
                members = expert_team.get("members")
                member = next(
                    (
                        item for item in members
                        if isinstance(item, dict) and item.get("id") == label
                    ),
                    None,
                ) if isinstance(members, list) else None
                instructions = str(member.get("instructions") or "").strip() if isinstance(member, dict) else ""
                source_prompt = self._build_expert_team_data_source_prompt(
                    expert_team,
                    label,
                    include_skills=not workflow_owned,
                )
                system_prompt = (
                    f"{system_prompt}\n\n---\n\n"
                    f"{self._build_expert_team_member_contract(label, instructions)}\n\n"
                    f"{source_prompt}"
                )
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            sess_key = origin.get("session_key")
            llm_timeout = (
                self._llm_wall_timeout_for_session(sess_key)
                if self._llm_wall_timeout_for_session
                else None
            )
            token = bind_workspace_scope(workspace_scope) if workspace_scope is not None else None
            run_max_iterations = (
                min(
                    self.max_iterations,
                    member_runtime.get("max_iterations", _EXPERT_TEAM_MAX_ITERATIONS),
                )
                if expert_team is not None
                else self.max_iterations
            )
            member_timeout_s = member_runtime.get(
                "timeout_seconds",
                _EXPERT_TEAM_MEMBER_TIMEOUT_S,
            )
            try:
                run_spec = AgentRunSpec(
                    initial_messages=messages,
                    tools=tools,
                    model=self.model,
                    temperature=temperature,
                    # Imported research workflows can otherwise keep searching
                    # up to the global 200-iteration ceiling. Expert-team
                    # members get a larger but still finite research budget so
                    # deep financial and industry work can finish without one
                    # role holding the entire team open indefinitely.
                    max_iterations=run_max_iterations,
                    max_tool_result_chars=self.max_tool_result_chars,
                    hook=_SubagentHook(
                        task_id,
                        status,
                        on_activity=_on_activity if expert_team is not None else None,
                    ),
                    max_iterations_message="Task completed but no final response was generated.",
                    finalize_on_max_iterations=False,
                    error_message=None,
                    # Research members routinely encounter unavailable pages,
                    # anti-bot responses, and stale links.  Those are soft
                    # evidence failures: return them to the model so it can use
                    # another source instead of terminating the whole member.
                    # Preserve the stricter legacy behavior for ordinary
                    # one-off subagents.
                    fail_on_tool_error=expert_team is None,
                    checkpoint_callback=_on_checkpoint,
                    session_key=sess_key,
                    workspace=root,
                    llm_timeout_s=llm_timeout,
                    agent_kind="subagent",
                    agent_label=label,
                    enforce_finance_source_priority=(
                        isinstance(expert_team, dict)
                        and expert_team.get("id") == "asset-research-team"
                    ),
                )
                result = (
                    await asyncio.wait_for(
                        self.runner.run(run_spec),
                        timeout=member_timeout_s,
                    )
                    if expert_team is not None
                    else await self.runner.run(run_spec)
                )
            finally:
                if token is not None:
                    reset_workspace_scope(token)
            status.phase = "done"
            status.stop_reason = result.stop_reason
            quality_issue = (
                self._expert_team_report_quality_issue(result)
                if expert_team is not None
                else None
            )

            if result.stop_reason == "tool_error":
                status.tool_events = list(result.tool_events)
                if await self._retry_expert_team_member(
                    retry_count=retry_count,
                    reason="首次工具链未完成，正在自动重试该角色",
                    task_id=task_id,
                    task=task,
                    label=label,
                    origin=origin,
                    status=status,
                    origin_message_id=origin_message_id,
                    temperature=temperature,
                    workspace_scope=workspace_scope,
                    expert_team=expert_team,
                    expert_team_run_id=expert_team_run_id,
                ):
                    return
                partial_result = self._format_partial_progress(result)
                artifact = await self._persist_expert_team_member_artifact(
                    content=partial_result,
                    label=label,
                    run_id=expert_team_run_id,
                    origin=origin,
                    workspace_scope=workspace_scope,
                    delivery_status="degraded",
                )
                await self._announce_result(
                    task_id, label, task,
                    partial_result + (
                        f"\n\nRole artifact: `{artifact}`" if artifact else ""
                    ),
                    origin, "error", origin_message_id,
                    expert_team=expert_team is not None,
                )
                await self._publish_team_member_update(
                    origin, expert_team, expert_team_run_id,
                    task_id=task_id, label=label, status="failed",
                    activity="该角色未形成有效报告，等待 Team Lead 重试或补齐",
                    artifact=artifact,
                )
            elif result.stop_reason == "error":
                if await self._retry_expert_team_member(
                    retry_count=retry_count,
                    reason="首次运行异常，正在自动重试该角色",
                    task_id=task_id,
                    task=task,
                    label=label,
                    origin=origin,
                    status=status,
                    origin_message_id=origin_message_id,
                    temperature=temperature,
                    workspace_scope=workspace_scope,
                    expert_team=expert_team,
                    expert_team_run_id=expert_team_run_id,
                ):
                    return
                error_result = result.error or "Error: subagent execution failed."
                artifact = await self._persist_expert_team_member_artifact(
                    content=error_result,
                    label=label,
                    run_id=expert_team_run_id,
                    origin=origin,
                    workspace_scope=workspace_scope,
                    delivery_status="degraded",
                )
                await self._announce_result(
                    task_id, label, task,
                    error_result + (
                        f"\n\nRole artifact: `{artifact}`" if artifact else ""
                    ),
                    origin, "error", origin_message_id,
                    expert_team=expert_team is not None,
                )
                await self._publish_team_member_update(
                    origin, expert_team, expert_team_run_id,
                    task_id=task_id, label=label, status="failed",
                    activity="该角色运行异常，等待 Team Lead 重试或补齐",
                    artifact=artifact,
                )
            elif quality_issue is not None:
                if await self._retry_expert_team_member(
                    retry_count=retry_count,
                    reason=f"交付质量检查未通过（{quality_issue}），正在自动重试该角色",
                    task_id=task_id,
                    task=task,
                    label=label,
                    origin=origin,
                    status=status,
                    origin_message_id=origin_message_id,
                    temperature=temperature,
                    workspace_scope=workspace_scope,
                    expert_team=expert_team,
                    expert_team_run_id=expert_team_run_id,
                ):
                    return
                status.phase = "error"
                status.error = quality_issue
                partial = (result.final_content or "").strip()
                degraded_result = (
                    f"Report quality check failed: {quality_issue}"
                    + (f"\n\nPartial result:\n{partial}" if partial else "")
                )
                artifact = await self._persist_expert_team_member_artifact(
                    content=degraded_result,
                    label=label,
                    run_id=expert_team_run_id,
                    origin=origin,
                    workspace_scope=workspace_scope,
                    delivery_status="degraded",
                )
                await self._announce_result(
                    task_id,
                    label,
                    task,
                    degraded_result + (
                        f"\n\nRole artifact: `{artifact}`" if artifact else ""
                    ),
                    origin,
                    "error",
                    origin_message_id,
                    expert_team=expert_team is not None,
                )
                await self._publish_team_member_update(
                    origin,
                    expert_team,
                    expert_team_run_id,
                    task_id=task_id,
                    label=label,
                    status="failed",
                    activity=f"未通过交付质量检查：{quality_issue}；等待 Team Lead 补齐",
                    artifact=artifact,
                )
            else:
                final_result = result.final_content or "Task completed but no final response was generated."
                artifact = await self._persist_expert_team_member_artifact(
                    content=final_result,
                    label=label,
                    run_id=expert_team_run_id,
                    origin=origin,
                    workspace_scope=workspace_scope,
                    delivery_status="completed",
                )
                if artifact:
                    final_result = f"{final_result}\n\nRole artifact: `{artifact}`"
                logger.info("Subagent [{}] completed successfully", task_id)
                await self._announce_result(
                    task_id,
                    label,
                    task,
                    final_result,
                    origin,
                    "ok",
                    origin_message_id,
                    expert_team=expert_team is not None,
                )
                await self._publish_team_member_update(
                    origin, expert_team, expert_team_run_id,
                    task_id=task_id, label=label, status="completed",
                    activity="研究完成，完整结果已交付 Team Lead",
                    artifact=artifact,
                )

        except asyncio.TimeoutError:
            status.phase = "error"
            status.stop_reason = "timeout"
            member_timeout_s = member_runtime.get(
                "timeout_seconds",
                _EXPERT_TEAM_MEMBER_TIMEOUT_S,
            )
            status.error = (
                f"expert-team member exceeded {member_timeout_s} seconds"
            )
            if member_runtime:
                partial_progress = self._format_partial_progress_from_events(
                    status.completed_tool_events or status.tool_events,
                    status.error,
                    limit=12,
                )
                timeout_result = (
                    "Error: this research member exceeded its runtime deadline and was stopped. "
                    "Treat the missing dimension as a degradable evidence gap. Use the Team Lead "
                    "shared context, configured expert-team sources, and completed member reports "
                    "to fill it; do not restart the same failed lookup loop."
                    + (
                        "\n\nPartial completed research before timeout:\n" + partial_progress
                        if partial_progress
                        else ""
                    )
                )
            else:
                timeout_result = (
                    "Error: this research member exceeded its runtime deadline and was stopped. "
                    "Treat the missing dimension as a degradable evidence gap. Use the Team Lead "
                    "data package, configured iFinD MCP, Juyuan MCP, and Caihui MCP sources, and "
                    "completed member reports to fill it; do not restart the same failed lookup "
                    "loop."
                )
            artifact = await self._persist_expert_team_member_artifact(
                content=timeout_result,
                label=label,
                run_id=expert_team_run_id,
                origin=origin,
                workspace_scope=workspace_scope,
                delivery_status="degraded",
            )
            await self._announce_result(
                task_id,
                label,
                task,
                timeout_result + (
                    f"\n\nRole artifact: `{artifact}`" if artifact else ""
                ),
                origin,
                "error",
                origin_message_id,
                expert_team=expert_team is not None,
            )
            await self._publish_team_member_update(
                origin,
                expert_team,
                expert_team_run_id,
                task_id=task_id,
                label=label,
                status="failed",
                activity=(
                    "运行超时，已停止重复取数；Team Lead 将使用已绑定来源和现有证据降级补齐"
                    if member_runtime
                    else "运行超时，已停止重复取数；Team Lead 将使用结构化数据和现有证据降级补齐"
                ),
                artifact=artifact,
            )
        except asyncio.CancelledError:
            if status.stop_reason != "cancelled":
                status.phase = "done"
                status.stop_reason = "cancelled"
                status.error = None
                await self._publish_team_member_update(
                    origin,
                    expert_team,
                    expert_team_run_id,
                    task_id=task_id,
                    label=label,
                    status="cancelled",
                    activity="已随主任务停止，并发槽位已释放",
                )
            raise
        except Exception as e:
            if await self._retry_expert_team_member(
                retry_count=retry_count,
                reason="首次运行抛出异常，正在自动重试该角色",
                task_id=task_id,
                task=task,
                label=label,
                origin=origin,
                status=status,
                origin_message_id=origin_message_id,
                temperature=temperature,
                workspace_scope=workspace_scope,
                expert_team=expert_team,
                expert_team_run_id=expert_team_run_id,
            ):
                return
            status.phase = "error"
            status.error = str(e)
            logger.exception("Subagent [{}] failed", task_id)
            exception_result = f"Error: {e}"
            artifact = await self._persist_expert_team_member_artifact(
                content=exception_result,
                label=label,
                run_id=expert_team_run_id,
                origin=origin,
                workspace_scope=workspace_scope,
                delivery_status="degraded",
            )
            await self._announce_result(
                task_id,
                label,
                task,
                exception_result + (
                    f"\n\nRole artifact: `{artifact}`" if artifact else ""
                ),
                origin,
                "error",
                origin_message_id,
                expert_team=expert_team is not None,
            )
            await self._publish_team_member_update(
                origin, expert_team, expert_team_run_id,
                task_id=task_id, label=label, status="failed",
                activity="该角色运行异常，等待 Team Lead 重试或补齐",
                artifact=artifact,
            )

    async def spawn_for_workflow(
        self,
        *,
        task: str,
        label: str,
        origin_channel: str,
        origin_chat_id: str,
        session_key: str,
        origin_message_id: str | None,
        workspace_scope: WorkspaceScope,
        expert_team: dict[str, Any],
        expert_team_run_id: str,
    ) -> str:
        """Start one graph-owned member and return its opaque runtime id."""

        return await self.spawn(
            task=task,
            label=label,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            session_key=session_key,
            origin_message_id=origin_message_id,
            workspace_scope=workspace_scope,
            expert_team=expert_team,
            expert_team_run_id=expert_team_run_id,
            _workflow_owned=True,
        )

    async def _retry_expert_team_member(
        self,
        *,
        retry_count: int,
        reason: str,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        status: SubagentStatus,
        origin_message_id: str | None,
        temperature: float | None,
        workspace_scope: WorkspaceScope | None,
        expert_team: dict[str, Any] | None,
        expert_team_run_id: str | None,
    ) -> bool:
        member_runtime = _expert_team_member_runtime(expert_team, label)
        max_retries = member_runtime.get("max_retries", 1)
        if expert_team is None or retry_count >= max_retries:
            return False
        status.phase = "initializing"
        status.error = None
        await self._publish_team_member_update(
            origin,
            expert_team,
            expert_team_run_id,
            task_id=task_id,
            label=label,
            status="running",
            activity=reason,
        )
        await self._run_subagent(
            task_id,
            task + (
                "\n\nRuntime retry: the first attempt failed. Avoid the failed source, use the "
                "remaining configured iFinD/Juyuan/Caihui sources, then AnySearch and explicit "
                "DuckDuckGo only if all three core sources miss the same field. Return a "
                "self-contained report with gaps and confidence; do not block delivery."
            ),
            label,
            origin,
            status,
            origin_message_id,
            temperature,
            workspace_scope,
            expert_team,
            expert_team_run_id,
            retry_count + 1,
        )
        return True

    async def _persist_expert_team_member_artifact(
        self,
        *,
        content: str,
        label: str,
        run_id: str | None,
        origin: dict[str, str],
        workspace_scope: WorkspaceScope | None,
        delivery_status: str,
    ) -> str | None:
        """Persist every terminal role result as a parent-turn intermediate artifact."""

        if not run_id or origin.get("channel") != "websocket":
            return None
        root = (
            workspace_scope.project_path
            if workspace_scope is not None
            else self.workspace
        ).expanduser().resolve(strict=False)
        safe_run = re.sub(r"[^a-zA-Z0-9_-]+", "-", run_id).strip("-")[:80]
        safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "-", label).strip("-")[:80]
        if not safe_run or not safe_label:
            return None
        relative = (
            _EXPERT_TEAM_MEMBER_ARTIFACT_DIR
            / safe_run
            / "members"
            / f"{safe_label}.md"
        )
        path = (root / relative).resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError:
            return None

        body = (
            f"# {label} 研究产物\n\n"
            f"- 团队运行：`{run_id}`\n"
            f"- 交付状态：`{delivery_status}`\n\n"
            "---\n\n"
            f"{content.strip() or '该角色未返回可用正文；Team Lead 必须按降级流程补齐。'}\n"
        )

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(body, encoding="utf-8")
            temporary.replace(path)

        try:
            await asyncio.to_thread(_write)
            from nanobot.storage.state import StateStore

            state = StateStore(
                self.workspace / ".nanobot" / "state.sqlite",
                default_workspace=self.workspace,
            )
            parent_session_key = str(
                origin.get("session_key")
                or f"{origin['channel']}:{origin['chat_id']}"
            )
            turn_id = state.active_turn_id(parent_session_key)
            await asyncio.to_thread(
                state.register_artifact,
                parent_session_key,
                path,
                relation_type="intermediate",
                artifact_kind="document",
                mime_type="text/markdown",
                turn_id=turn_id,
            )
        except Exception:
            logger.exception(
                "Failed to persist expert-team member artifact run={} member={}",
                run_id,
                label,
            )
            return None
        return relative.as_posix()

    @staticmethod
    def _expert_team_report_quality_issue(result: Any) -> str | None:
        """Reject process completion that did not produce a usable role report."""
        if result.stop_reason == "max_iterations":
            return "达到工具轮次上限，未形成正式报告"
        content = (result.final_content or "").strip()
        if not content:
            return "未返回报告正文"
        generic_failures = (
            "Task completed but no final response was generated.",
            "Task completed but no final response",
            "Error: subagent execution failed",
        )
        if any(marker.lower() in content.lower() for marker in generic_failures):
            return "仅返回运行时占位信息"
        if len(content) < 600:
            return f"报告正文过短（{len(content)} 字符）"
        lower = content.lower()
        has_conclusion = "结论" in content or "conclusion" in lower
        has_evidence = any(marker in lower for marker in ("来源", "source", "数据", "evidence"))
        if not has_conclusion or not has_evidence:
            return "缺少明确结论或数据来源"
        return None

    async def _publish_team_member_update(
        self,
        origin: dict[str, str],
        expert_team: dict[str, Any] | None,
        run_id: str | None,
        *,
        task_id: str,
        label: str,
        status: str,
        activity: str | None = None,
        artifact: str | None = None,
    ) -> None:
        if origin.get("channel") != "websocket" or not isinstance(expert_team, dict) or not run_id:
            return
        members = expert_team.get("members")
        member = next(
            (
                item for item in members
                if isinstance(item, dict) and item.get("id") == label
            ),
            None,
        ) if isinstance(members, list) else None
        member_id = str(member.get("id")) if isinstance(member, dict) else label
        member_name = str(member.get("name")) if isinstance(member, dict) else label
        member_description = str(member.get("description") or "").strip() if isinstance(member, dict) else ""
        visible_activity = activity or (
            member_description if status == "running" else ""
        )
        await self.bus.publish_outbound(OutboundMessage(
            channel="websocket",
            chat_id=origin["chat_id"],
            content="",
            metadata={
                "_team_member_updated": True,
                "team_member": {
                    "run_id": run_id,
                    "team_id": str(expert_team.get("id") or ""),
                    "task_id": task_id,
                    "id": member_id,
                    "name": member_name,
                    "status": status,
                    "activity": visible_activity,
                    **({"artifact": artifact} if artifact else {}),
                },
            },
        ))

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
        origin_message_id: str | None = None,
        *,
        expert_team: bool = False,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        if task_id in self._workflow_task_ids:
            future = self._workflow_completion_futures.get(task_id)
            if future is not None and not future.done():
                future.set_result(WorkflowSubagentResult(
                    task_id=task_id,
                    label=label,
                    status=status,
                    content=result,
                ))
            logger.debug(
                "Subagent [{}] returned directly to its runtime-owned workflow",
                task_id,
            )
            return

        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = render_template(
            (
                "agent/subagent_team_announce.md"
                if expert_team
                else "agent/subagent_announce.md"
            ),
            label=label,
            status_text=status_text,
            task=task,
            result=result,
        )

        # Inject as system message to trigger main agent.
        # Use session_key_override to align with the main agent's effective
        # session key (which accounts for unified sessions) so the result is
        # routed to the correct pending queue (mid-turn injection) instead of
        # being dispatched as a competing independent task.
        override = origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        metadata: dict[str, Any] = {
            "injected_event": "subagent_result",
            "subagent_task_id": task_id,
        }
        if origin_message_id:
            metadata["origin_message_id"] = origin_message_id
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            session_key_override=override,
            metadata=metadata,
        )

        await self.bus.publish_inbound(msg)
        logger.debug("Subagent [{}] announced result to {}:{}", task_id, origin['channel'], origin['chat_id'])

    async def wait_for_workflow_tasks(
        self,
        task_ids: list[str],
    ) -> list[WorkflowSubagentResult]:
        """Wait for graph-owned tasks without routing their results as new turns."""

        futures = [
            self._workflow_completion_futures[task_id]
            for task_id in task_ids
            if task_id in self._workflow_completion_futures
        ]
        tasks = [
            self._running_tasks[task_id]
            for task_id in task_ids
            if task_id in self._running_tasks
        ]
        if len(futures) != len(task_ids):
            missing = [
                task_id for task_id in task_ids
                if task_id not in self._workflow_completion_futures
            ]
            raise RuntimeError(
                "workflow subagent completion handle missing: " + ", ".join(missing)
            )
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            return list(await asyncio.gather(*futures))
        finally:
            for task_id in task_ids:
                self._workflow_completion_futures.pop(task_id, None)

    def get_running_task_ids_by_session(self, session_key: str) -> set[str]:
        """Return unfinished task IDs so team result batching can ignore announcers winding down."""
        return {
            task_id
            for task_id in self._session_tasks.get(session_key, set())
            if (task := self._running_tasks.get(task_id)) is not None and not task.done()
        }

    @staticmethod
    def _format_partial_progress(result) -> str:
        return SubagentManager._format_partial_progress_from_events(
            result.tool_events,
            result.error,
        )

    @staticmethod
    def _format_partial_progress_from_events(
        tool_events: list[dict[str, Any]],
        error: str | None,
        *,
        limit: int = 3,
    ) -> str:
        completed = [e for e in tool_events if e.get("status") == "ok"]
        failure = next(
            (e for e in reversed(tool_events) if e.get("status") == "error"),
            None,
        )
        lines: list[str] = []
        if completed:
            lines.append("Completed steps:")
            for event in completed[-max(1, limit):]:
                lines.append(
                    f"- {event.get('name', 'tool')}: {event.get('detail', '')}"
                )
        if failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {failure['name']}: {failure['detail']}")
        if error and not failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {error}")
        return "\n".join(lines) or (error or "Error: subagent execution failed.")

    def _build_subagent_prompt(
        self,
        workspace: Path | None = None,
        *,
        include_skills: bool = True,
    ) -> str:
        """Build a focused system prompt for the subagent."""
        from nanobot.agent.context import ContextBuilder

        time_ctx = ContextBuilder._build_runtime_context(None, None)
        root = workspace or self.workspace
        skills_summary = ""
        if include_skills:
            from nanobot.agent.skills import SkillsLoader

            skills_summary = SkillsLoader(
                root,
                disabled_skills=self.disabled_skills,
            ).build_skills_summary()
        return render_template(
            "agent/subagent_system.md",
            time_ctx=time_ctx,
            workspace=str(root),
            skills_summary=skills_summary or "",
        )

    def _build_expert_team_data_source_prompt(
        self,
        expert_team: dict[str, Any],
        label: str,
        *,
        include_skills: bool = True,
    ) -> str:
        """Describe the exact sources bound to an expert-team member."""

        raw_sources = expert_team.get("data_sources")
        sections: list[str] = []
        if include_skills and isinstance(raw_sources, list) and raw_sources:
            from nanobot.agent.skills import SkillsLoader

            loader = SkillsLoader(self.workspace, disabled_skills=self.disabled_skills)
            entries = {
                entry["name"]: entry
                for entry in loader.list_skills(filter_unavailable=False)
            }
            sections.extend([
                "# Required Integrated Financial Data Sources",
                "These data-source Skills are part of the expert team, not optional suggestions. "
                "For A-share key numeric fields, query the configured iFinD, Juyuan, and Caihui "
                "sources by field and cross-validate values, dates, units, and reporting scope "
                "before broad web research.",
            ])
            for source in raw_sources:
                if not isinstance(source, dict):
                    continue
                skill_name = str(source.get("skill") or "").strip()
                if not skill_name:
                    continue
                source_name = str(source.get("name") or skill_name).strip()
                assignments = source.get("assignments")
                assignment = (
                    str(assignments.get(label) or "").strip()
                    if isinstance(assignments, dict)
                    else ""
                )
                entry = entries.get(skill_name)
                available, reason = loader.get_skill_availability(skill_name)
                if skill_name in self.disabled_skills:
                    available, reason = False, "Skill is disabled"
                content = (
                    loader.load_skills_for_context([skill_name])
                    if available and entry
                    else ""
                )
                if not content:
                    required = "required" if source.get("required") is True else "optional"
                    sections.append(
                        f"## {source_name} ({required}, unavailable)\n\n"
                        f"Reason: {reason or 'Skill is not installed in the nanobot workspace'}. "
                        "Report this concrete data-source gap to the Team Lead and use "
                        "authoritative filings as fallback."
                    )
                    continue
                skill_path = entry["path"]
                sections.append(
                    f"## {source_name} (primary, active)\n\n"
                    f"Skill path: `{skill_path}`\n\n"
                    f"Your assigned use: {assignment or 'query the structured financial data needed by your role'}.\n\n"
                    "Run its commands from the Skill directory so its local configuration is "
                    "resolved. Do not print, copy, or expose credential/configuration contents.\n\n"
                    f"{content}"
                )
        raw_presets = expert_team.get("mcp_presets")
        if isinstance(raw_presets, list):
            sections.append(
                "# Team-bound MCP Financial Data Sources\n\n"
                "Configured MCP sources below are activated by the team runtime and inherited from "
                "the Team Lead. Use their exact `mcp_<name>_...` tools when those tools are present. "
                "Do not search the filesystem for MCP configuration or credentials."
            )
            for preset in raw_presets:
                if not isinstance(preset, dict):
                    continue
                name = str(preset.get("name") or "").strip()
                if not name:
                    continue
                display = str(preset.get("display_name") or name).strip()
                description = str(preset.get("description") or "").strip()
                if preset.get("configured") is True:
                    sections.append(
                        f"## {display} (configured, team-activated)\n\n"
                        f"Tool prefix: `mcp_{name}_`. {description}"
                    )
                else:
                    sections.append(
                        f"## {display} (not configured)\n\n"
                        "This source is not active for the current run. Do not attempt to discover "
                        "credentials or configuration files; use another bound structured source."
                    )
            sections.append(
                "Enforce this source state machine per field: use every configured core source among "
                "iFinD, `mcp_juyuan_...`, and `mcp_caihui_mcp_...` for cross-validation. On a hard "
                "failure, inner `call failed`/429/permission error, or repeated-query warning, stop "
                "that source and continue the other core sources; never issue cosmetic retries. "
                "Only when all three core sources fail or omit that same field, use configured "
                "`mcp_anysearch_...`, then `web_search` with `provider=duckduckgo`. Preserve source "
                "conflicts, dates, units, gaps, and confidence. Missing data never blocks the role "
                "artifact or final report."
            )
        return "\n\n".join(sections)

    @staticmethod
    def _build_expert_team_member_contract(label: str, instructions: str = "") -> str:
        """Runtime contract that keeps imported team prompts nanobot-native."""
        return f"""# Expert Team Member Runtime Contract

You are the `{label}` member of a nanobot expert-team run. These rules override
any incompatible Claude Code coordination or tool instructions embedded in the
task text:

- Use only tools that are actually present in your tool list. The nanobot web
  tools are named `web_search` and `web_fetch`; shell execution is `exec`.
- Never call or wait for Claude Code-only tools such as `WebSearch`, `WebFetch`,
  `Bash`, `Task`, `TaskUpdate`, `SendMessage`, `TeamCreate`, or `TeamDelete`.
- Work independently. Do not wait for another member and do not read or write
  shared intermediate role-report files. Return your complete research in your
  final response; nanobot delivers that response to the Team Lead automatically.
- A failed page, blocked site, missing file, or repeated-query warning is a
  recoverable evidence failure. Change the query/source or use reliable search
  snippets, and continue the remaining analysis. Never repeat an identical
  external lookup more than twice.
- For A-share key fields, cross-validate every configured core source among
  iFinD, Juyuan, and Caihui. Stop a source after its first hard failure and
  continue the remaining core sources. Only if all three fail or omit the same
  field, fall back to AnySearch and then explicit DuckDuckGo. Label conflicts,
  dates, units, gaps, and confidence; never rotate cosmetic queries.
- Your terminal response is automatically materialized by nanobot as this
  role's durable artifact. Always return the fullest supported report even in
  degraded mode; do not withhold it because a source failed.
- Keep research bounded: prioritize a small set of authoritative sources and
  synthesize once the key claims are supported. Do not keep searching for a
  perfect source. Never fabricate unavailable data; label gaps and confidence.
- Do not perform permission prechecks or ask the user to configure `.claude`,
  `/permissions`, or Claude Code settings.
""" + (f"""

---

# Role Playbook (authoritative for this member)

{instructions}
""" if instructions else "")

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        task_ids = [
            tid for tid in self._session_tasks.get(session_key, set())
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        ]
        tasks = [self._running_tasks[tid] for tid in task_ids]
        for t in tasks:
            t.cancel()
        # Release concurrency accounting synchronously. Done callbacks remain
        # idempotent and will see these entries already removed.
        for task_id in task_ids:
            self._running_tasks.pop(task_id, None)
            self._task_statuses.pop(task_id, None)
        remaining = self._session_tasks.get(session_key)
        if remaining is not None:
            remaining.difference_update(task_ids)
            if not remaining:
                self._session_tasks.pop(session_key, None)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return sum(1 for task in self._running_tasks.values() if not task.done())

    def get_running_count_by_session(self, session_key: str) -> int:
        """Return the number of currently running subagents for a session."""
        tids = self._session_tasks.get(session_key, set())
        return sum(
            1 for tid in tids
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        )
