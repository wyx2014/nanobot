"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import time
from contextlib import AsyncExitStack, nullcontext, suppress
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent import context as agent_context
from nanobot.agent import model_presets as preset_helpers
from nanobot.agent.autocompact import AutoCompact
from nanobot.agent.context import ContextBuilder
from nanobot.agent.cron_turns import CronTurnCoordinator
from nanobot.agent.hook import AgentHook, CompositeHook
from nanobot.agent.memory import EXPERT_TEAM_TURN_KEY, Consolidator
from nanobot.agent.progress_hook import AgentProgressHook
from nanobot.agent.runner import _MAX_INJECTIONS_PER_TURN, AgentRunner, AgentRunSpec
from nanobot.agent.skill_scope import (
    bind_allowed_workspace_skills,
    reset_allowed_workspace_skills,
)
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.context import RequestContext, bind_request_context, reset_request_context
from nanobot.agent.tools.file_state import FileStateStore, bind_file_states, reset_file_states
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.self import MyTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.progress import build_bus_progress_callback
from nanobot.bus.queue import MessageBus
from nanobot.bus.runtime_events import (
    RuntimeEventBus,
    RuntimeEventContext,
    RuntimeEventPublisher,
    ensure_runtime_event_publisher,
)
from nanobot.command import CommandContext, CommandRouter, register_builtin_commands
from nanobot.config.schema import AgentDefaults, ModelPresetConfig
from nanobot.cron.session_turns import (
    cron_history_overrides,
    is_cron_turn,
)
from nanobot.graph.workflows.asset_research import (
    MEMBER_NODES,
    REPORT_AUDIT,
    TEAM_LEAD,
    public_asset_research_state,
)
from nanobot.graph.workflows.asset_research_runtime import (
    AUDIT_MAX_TOOL_ITERATIONS,
    AgentNodeOutcome,
    AssetResearchWorkflowRuntime,
    MemberBatchOutcome,
    MemberNodeOutcome,
)
from nanobot.observability.trace_collector import TraceCollector
from nanobot.observability.trace_store import TraceStore
from nanobot.providers.base import LLMProvider
from nanobot.providers.factory import ProviderSnapshot
from nanobot.runtime.turn_lifecycle import (
    FinishReason,
    ThreadRuntimeRegistry,
    TurnLifecycleError,
    TurnLifecycleManager,
    TurnStatus,
)
from nanobot.security.project_context import (
    PROJECT_CONTEXT_METADATA_KEY,
    bind_project_context,
    project_context_from_metadata,
    reset_project_context,
)
from nanobot.security.workspace_access import (
    WorkspaceScopeResolver,
    bind_workspace_scope,
    reset_workspace_scope,
)
from nanobot.session import turn_continuation
from nanobot.session.goal_state import (
    goal_state_runtime_lines,
    runner_wall_llm_timeout_s,
    sustained_goal_active,
)
from nanobot.session.keys import UNIFIED_SESSION_KEY, session_key_for_channel
from nanobot.session.manager import (
    MODEL_REPLAY_POLICY_KEY,
    MODEL_REPLAY_UI_ONLY,
    Session,
    SessionManager,
)
from nanobot.utils.document import extract_documents, reference_non_image_attachments
from nanobot.utils.helpers import image_placeholder_text
from nanobot.utils.helpers import truncate_text as truncate_text_fn
from nanobot.utils.image_generation_intent import image_generation_prompt
from nanobot.utils.llm_runtime import LLMRuntime
from nanobot.utils.markdown_html import HTML_TEMPLATE_METADATA_KEY
from nanobot.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
)
from nanobot.webui.expert_teams import (
    ASSET_RESEARCH_TEAM_ID,
    EXPERT_TEAM_RESUME_KEY,
    EXPERT_TEAM_TURN_ROUTE_KEY,
    EXPERT_TEAM_TURN_SUPPRESSED_KEY,
    classify_expert_team_turn_with_model,
)
from nanobot.webui.interactive_prompt import (
    INBOUND_META_INTERACTIVE_PROMPT_ANSWER,
    SESSION_META_PENDING_INTERACTIVE_PROMPT,
    interactive_prompt_requested_in_turn,
    normalize_interactive_prompt,
    normalize_interactive_prompt_answer,
    reset_interactive_prompt_requested,
    set_interactive_prompt_requested,
)
from nanobot.webui.metadata import (
    ACTIVE_TURN_CLIENT_TURN_METADATA_KEY,
    ACTIVE_TURN_CORRECTION_METADATA_KEY,
    WEBUI_TURN_METADATA_KEY,
)
from nanobot.webui.project_skills_api import project_skill_grants

if TYPE_CHECKING:
    from nanobot.config.schema import (
        ChannelsConfig,
        ProviderConfig,
        ToolsConfig,
    )
    from nanobot.cron.service import CronService
    from nanobot.storage.logs import StructuredLogStore


def _project_skill_scope(workspace: Path, project_path: Path | str | None, metadata: dict[str, Any]) -> dict[str, list[str]]:
    granted = project_skill_grants(workspace, project_path)
    requested = metadata.get("skill_scope")
    explicit = requested.get("explicit_skills", []) if isinstance(requested, dict) else []
    expert_team = metadata.get("expert_team")
    team_skills = [
        str(item.get("skill")).strip()
        for item in expert_team.get("data_sources", [])
        if isinstance(item, dict) and str(item.get("skill") or "").strip()
    ] if isinstance(expert_team, dict) else []
    return {
        "project_bound_user_skills": list(dict.fromkeys([*granted, *team_skills])),
        "explicit_skills": list(dict.fromkeys([
            *(name for name in explicit if isinstance(name, str)),
            *team_skills,
        ])),
    }


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        str(item.get("text") or "").strip()
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and str(item.get("text") or "").strip()
    ]
    return "\n".join(parts)


def _active_turn_objective(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _message_content_text(message.get("content"))
        if text:
            return truncate_text_fn(text, 1200)
    return ""


def _wrap_active_turn_correction(content: Any, *, objective: str) -> Any:
    correction = _message_content_text(content)
    contract = (
        "[Active-turn user correction]\n"
        "This is a correction to the task currently in progress, not a new task. "
        "Keep the active objective and the evidence gathered for it. Do not infer "
        "a different topic from older conversation history, browser "
        "cache, or unrelated files.\n"
        f"Active objective:\n{objective or '[Use the immediately preceding active user request.]'}\n"
        f"User correction:\n{correction or '[See the attached content.]'}\n"
        "Apply this correction now. If it relaxes or changes a requested source, stop "
        "treating the old source as mandatory. When a preferred source lacks the "
        "required evidence, use credible alternative sources and clearly distinguish "
        "what each source supports. Do not search local history or caches as a substitute "
        "for current external evidence unless the user explicitly asks for that."
    )
    if isinstance(content, str):
        return contract
    if isinstance(content, list):
        non_text_blocks = [
            item
            for item in content
            if not (
                isinstance(item, dict)
                and item.get("type") == "text"
            )
        ]
        return [{"type": "text", "text": contract}, *non_text_blocks]
    return contract


def _expert_team_binding(
    message_metadata: dict[str, Any] | None,
    session_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the active expert-team binding from the message or session."""
    if (
        isinstance(message_metadata, dict)
        and message_metadata.get(EXPERT_TEAM_TURN_SUPPRESSED_KEY) is True
    ):
        return None
    for metadata in (message_metadata, session_metadata):
        if not isinstance(metadata, dict):
            continue
        team = metadata.get("expert_team")
        if isinstance(team, dict):
            return team
    return None


class _SingleRewriteAuditToolRegistry(ToolRegistry):
    """Expose audit tools while permitting at most one complete report rewrite."""

    def __init__(self, source: ToolRegistry) -> None:
        super().__init__()
        self._rewrite_attempted = False
        for name in source.tool_names:
            if name == "edit_file":
                continue
            tool = source.get(name)
            if tool is not None:
                self.register(tool)

    async def execute(self, name: str, params: Any) -> Any:
        if name == "write_file":
            if self._rewrite_attempted:
                return (
                    "Audit policy: the single allowed complete report rewrite has "
                    "already been used. Do not modify files again; finish the audit "
                    "with the current report."
                )
            self._rewrite_attempted = True
        return await super().execute(name, params)


def _tool_call_names(messages: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
    return names


def _expert_team_completion_guard_message(
    team: dict[str, Any] | None,
    messages: list[dict[str, Any]],
) -> str | None:
    """Keep a report-producing team alive until required delivery tools ran."""
    completion = team.get("completion") if isinstance(team, dict) else None
    if not isinstance(completion, dict):
        return None
    required = [
        str(item).strip()
        for item in completion.get("required_tools", [])
        if isinstance(item, str) and str(item).strip()
    ]
    if not required:
        return None
    called = _tool_call_names(messages)
    missing = [name for name in required if name not in called]
    if not missing:
        return None
    artifacts = [
        str(item).strip().lower().lstrip(".")
        for item in completion.get("required_artifacts", [])
        if isinstance(item, str) and str(item).strip()
    ]
    custom = str(completion.get("instruction") or "").strip()
    delivery = ", ".join(artifacts) if artifacts else "the required final artifacts"
    missing_tools = ", ".join(f"`{name}`" for name in missing)
    return (
        "[Expert-team completion guard]\n"
        "The expert-team workflow is not complete yet. Do not give the user a short "
        "member summary and do not end the turn. Continue with Team Lead synthesis "
        "and report audit, then produce and verify "
        f"{delivery}. Required delivery tool(s) not yet attempted: {missing_tools}. "
        "Use `write_file` for the final Markdown source; do not use `write_stdin` to "
        "wait for spawn task ids. If a renderer fails after a real attempt, preserve "
        "the successful artifacts and disclose the failure in the final response."
        + (f"\n\nTeam-specific instruction:\n{custom}" if custom else "")
    )


class TurnState(Enum):
    RESTORE = auto()
    COMPACT = auto()
    COMMAND = auto()
    BUILD = auto()
    RUN = auto()
    SAVE = auto()
    RESPOND = auto()
    DONE = auto()


@dataclass
class StateTraceEntry:
    state: TurnState
    started_at: float
    duration_ms: float
    event: str
    error: str | None = None


@dataclass
class TurnContext:
    msg: InboundMessage
    session_key: str
    state: TurnState
    turn_id: str
    session: Session | None = None

    history: list[dict[str, Any]] = field(default_factory=list)
    initial_messages: list[dict[str, Any]] = field(default_factory=list)

    final_content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    all_messages: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    had_injections: bool = False
    artifact_paths: list[str] = field(default_factory=list)

    user_persisted_early: bool = False
    save_skip: int = 0

    outbound: OutboundMessage | None = None
    suppress_response: bool = False

    on_progress: Callable[..., Awaitable[None]] | None = None
    on_stream: Callable[[str], Awaitable[None]] | None = None
    on_stream_end: Callable[..., Awaitable[None]] | None = None
    on_retry_wait: Callable[[str], Awaitable[None]] | None = None

    pending_queue: asyncio.Queue | None = None
    pending_summary: str | None = None

    ephemeral: bool = False
    run_extra_hooks_for_ephemeral: bool = False
    hooks: list[AgentHook] = field(default_factory=list)
    tools: ToolRegistry | None = None

    turn_wall_started_at: float = field(default_factory=time.time)
    visible_run_started_at: float | None = None
    turn_latency_ms: int | None = None
    turn_usage: dict[str, int] = field(default_factory=dict)

    trace: list[StateTraceEntry] = field(default_factory=list)


def _generated_artifact_paths(messages: list[dict[str, Any]]) -> list[str]:
    """Extract current-turn structured tool artifacts for automatic delivery."""
    paths: list[str] = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.lstrip().startswith("{"):
            continue
        try:
            payload = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, list):
            continue
        for entry in files:
            path = entry.get("path") if isinstance(entry, dict) else None
            if isinstance(path, str) and path.strip() and Path(path).is_file():
                paths.append(path.strip())
    return list(dict.fromkeys(paths))


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    @property
    def current_iteration(self) -> int:
        return self._current_iteration

    @property
    def tool_names(self) -> list[str]:
        return self.tools.tool_names

    def llm_runtime(self) -> LLMRuntime:
        """Return the current provider/model pair owned by this loop."""
        self._refresh_provider_snapshot()
        return LLMRuntime(self.provider, self.model)

    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
    _PENDING_USER_TURN_KEY = "pending_user_turn"

    # Event-driven state transition table.
    # Handlers return an event string; the driver looks up the next state here.
    _TRANSITIONS: dict[tuple[TurnState, str], TurnState] = {
        (TurnState.RESTORE, "ok"): TurnState.COMPACT,
        (TurnState.COMPACT, "ok"): TurnState.COMMAND,
        (TurnState.COMMAND, "dispatch"): TurnState.BUILD,
        (TurnState.COMMAND, "shortcut"): TurnState.DONE,
        (TurnState.BUILD, "ok"): TurnState.RUN,
        (TurnState.RUN, "ok"): TurnState.SAVE,
        (TurnState.SAVE, "ok"): TurnState.RESPOND,
        (TurnState.RESPOND, "ok"): TurnState.DONE,
    }

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        context_window_tokens: int | None = None,
        context_block_limit: int | None = None,
        max_tool_result_chars: int | None = None,
        provider_retry_mode: str = "standard",
        tool_hint_max_length: int | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        timezone: str | None = None,
        session_ttl_minutes: int = 0,
        consolidation_ratio: float = 0.5,
        max_messages: int = 120,
        hooks: list[AgentHook] | None = None,
        unified_session: bool = False,
        disabled_skills: list[str] | None = None,
        tools_config: ToolsConfig | None = None,
        image_generation_provider_config: ProviderConfig | None = None,
        image_generation_provider_configs: dict[str, ProviderConfig] | None = None,
        provider_snapshot_loader: Callable[..., ProviderSnapshot] | None = None,
        provider_signature: tuple[object, ...] | None = None,
        model_presets: dict[str, ModelPresetConfig] | None = None,
        model_preset: str | None = None,
        preset_snapshot_loader: preset_helpers.PresetSnapshotLoader | None = None,
        runtime_events: RuntimeEventBus | None = None,
        thread_runtime_registry: ThreadRuntimeRegistry | None = None,
        runtime_model_publisher: Callable[[str, str | None], None] | None = None,
        performance_log_store: StructuredLogStore | None = None,
    ):
        from nanobot.config.schema import ToolsConfig

        _tc = tools_config or ToolsConfig()
        defaults = AgentDefaults()
        self.bus = bus
        self.runtime_events = runtime_events or RuntimeEventBus()
        self.runtime_event_publisher = RuntimeEventPublisher(self.runtime_events)
        self._performance_logs = performance_log_store
        self.trace_store = (
            TraceStore(performance_log_store.path)
            if performance_log_store is not None
            else None
        )
        self.trace_collector = (
            TraceCollector(self.trace_store)
            if self.trace_store is not None
            else None
        )
        self.thread_runtime_registry = thread_runtime_registry or ThreadRuntimeRegistry(
            runtime_events=self.runtime_events,
            trace_collector=self.trace_collector,
        )
        if thread_runtime_registry is not None:
            self.thread_runtime_registry.set_trace_collector(self.trace_collector)
        self.turn_lifecycle = TurnLifecycleManager(self.thread_runtime_registry)
        self.channels_config = channels_config
        self.provider = provider
        self._provider_snapshot_loader = provider_snapshot_loader
        self._preset_snapshot_loader = preset_snapshot_loader
        self._runtime_model_publisher = runtime_model_publisher
        self._provider_signature = provider_signature
        self._default_selection_signature = preset_helpers.default_selection_signature(provider_signature)
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = (
            max_iterations if max_iterations is not None else defaults.max_tool_iterations
        )
        self.context_window_tokens = (
            context_window_tokens
            if context_window_tokens is not None
            else defaults.context_window_tokens
        )
        self.context_block_limit = context_block_limit
        self.max_tool_result_chars = (
            max_tool_result_chars
            if max_tool_result_chars is not None
            else defaults.max_tool_result_chars
        )
        self.provider_retry_mode = provider_retry_mode
        self.tool_hint_max_length = (
            tool_hint_max_length if tool_hint_max_length is not None
            else defaults.tool_hint_max_length
        )
        self.tools_config = _tc
        self.web_config = _tc.web
        self.exec_config = _tc.exec
        self._image_generation_provider_configs = dict(image_generation_provider_configs or {})
        if (
            image_generation_provider_config is not None
            and "openrouter" not in self._image_generation_provider_configs
        ):
            self._image_generation_provider_configs["openrouter"] = image_generation_provider_config
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self.workspace_scopes = WorkspaceScopeResolver(
            default_workspace=workspace,
            default_restrict_to_workspace=restrict_to_workspace,
        )
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self._extra_hooks: list[AgentHook] = hooks or []
        self.context = ContextBuilder(workspace, timezone=timezone, disabled_skills=disabled_skills)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        # One file-read/write tracker per logical session. The tool registry is
        # shared by this loop, so tools resolve the active state via contextvars.
        self._file_state_store = FileStateStore()
        self.runner = AgentRunner(provider, trace_collector=self.trace_collector)
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            tools_config=_tc,
            max_tool_result_chars=self.max_tool_result_chars,
            restrict_to_workspace=restrict_to_workspace,
            disabled_skills=disabled_skills,
            max_iterations=self.max_iterations,
            max_concurrent_subagents=max_concurrent_subagents,
            llm_wall_timeout_for_session=lambda sk: runner_wall_llm_timeout_s(self.sessions, sk),
            parent_tools=self.tools,
            trace_collector=self.trace_collector,
        )
        self._unified_session = unified_session
        self._max_messages = max_messages if max_messages > 0 else 120
        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stacks: dict[str, AsyncExitStack] = {}
        self._mcp_connected = False
        self._mcp_connecting = False
        self._mcp_warmup_complete = not bool(self._mcp_servers)
        self._mcp_owner_task: asyncio.Task[None] | None = None
        self._mcp_shutdown_event: asyncio.Event | None = None
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: list[asyncio.Task] = []
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Per-session pending queues for mid-turn message injection.
        # When a session has an active task, new messages for that session
        # are routed here instead of creating a new task.
        self._pending_queues: dict[str, asyncio.Queue] = {}
        self._cron_turns = CronTurnCoordinator(
            publish_inbound=self.bus.publish_inbound,
            dispatch=self._dispatch,
            is_running=lambda: self._running,
        )
        # NANOBOT_MAX_CONCURRENT_REQUESTS: <=0 means unlimited; default 3.
        _max = int(os.environ.get("NANOBOT_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )
        self.consolidator = Consolidator(
            store=self.context.memory,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
            consolidation_ratio=consolidation_ratio,
            unified_session=unified_session,
        )
        # Project-bound sessions still need compaction, but their summaries
        # must remain in session metadata instead of entering global memory.
        self.session_consolidator = Consolidator(
            store=None,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
            consolidation_ratio=consolidation_ratio,
            unified_session=unified_session,
        )
        self.auto_compact = AutoCompact(
            sessions=self.sessions,
            consolidator=self.consolidator,
            session_ttl_minutes=session_ttl_minutes,
            consolidator_for_session=self._consolidator_for_session,
        )
        self.model_presets: dict[str, ModelPresetConfig] = model_presets or {}
        self._active_preset: str | None = None
        if model_preset:
            self.set_model_preset(model_preset, publish_update=False)
        self._register_default_tools()
        self._runtime_vars: dict[str, Any] = {}
        self._current_iteration: int = 0
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)

    @classmethod
    def from_config(
        cls,
        config: Any,
        bus: MessageBus | None = None,
        **extra: Any,
    ) -> AgentLoop:
        """Create an AgentLoop from config with the common parameter set.

        Extra keyword arguments are forwarded to ``AgentLoop.__init__``,
        allowing callers to override or extend the standard config-derived
        parameters (e.g. ``cron_service``, ``session_manager``).
        """
        from nanobot.providers.factory import make_provider

        if bus is None:
            bus = MessageBus()
        defaults = config.agents.defaults
        provider = extra.pop("provider", None) or make_provider(config)
        resolved = config.resolve_preset()
        model = extra.pop("model", None) or resolved.model
        context_window_tokens = extra.pop("context_window_tokens", None) or resolved.context_window_tokens
        provider_snapshot_loader = extra.pop("provider_snapshot_loader", None)
        preset_snapshot_loader = extra.pop("preset_snapshot_loader", None) or preset_helpers.make_preset_snapshot_loader(
            config,
            provider_snapshot_loader,
        )
        return cls(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            model=model,
            max_iterations=defaults.max_tool_iterations,
            max_concurrent_subagents=defaults.max_concurrent_subagents,
            context_window_tokens=context_window_tokens,
            context_block_limit=defaults.context_block_limit,
            max_tool_result_chars=defaults.max_tool_result_chars,
            provider_retry_mode=defaults.provider_retry_mode,
            tool_hint_max_length=defaults.tool_hint_max_length,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            mcp_servers=config.tools.mcp_servers,
            channels_config=config.channels,
            timezone=defaults.timezone,
            unified_session=defaults.unified_session,
            disabled_skills=defaults.disabled_skills,
            session_ttl_minutes=defaults.session_ttl_minutes,
            consolidation_ratio=defaults.consolidation_ratio,
            max_messages=defaults.max_messages,
            tools_config=config.tools,
            model_presets=preset_helpers.configured_model_presets(config),
            model_preset=defaults.model_preset,
            provider_snapshot_loader=provider_snapshot_loader,
            preset_snapshot_loader=preset_snapshot_loader,
            **extra,
        )

    def _sync_subagent_runtime_limits(self) -> None:
        """Keep subagent runtime limits aligned with mutable loop settings."""
        self.subagents.max_iterations = self.max_iterations

    def _apply_provider_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        publish_update: bool = True,
        model_preset: str | None = None,
    ) -> None:
        """Swap model/provider for future turns without disturbing an active one."""
        provider = snapshot.provider
        model = snapshot.model
        context_window_tokens = snapshot.context_window_tokens
        old_model = self.model
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.runner.provider = provider
        self.subagents.set_provider(provider, model)
        self.consolidator.set_provider(provider, model, context_window_tokens)
        self.session_consolidator.set_provider(provider, model, context_window_tokens)
        self._provider_signature = snapshot.signature
        if publish_update and self._runtime_model_publisher is not None:
            self._runtime_model_publisher(
                self.model,
                model_preset if model_preset is not None else self.model_preset,
            )
        if publish_update:
            self._runtime_events().runtime_model_changed(
                self.model,
                model_preset if model_preset is not None else self.model_preset,
            )
        logger.info("Runtime model switched for next turn: {} -> {}", old_model, model)

    def _refresh_provider_snapshot(self) -> None:
        if self._provider_snapshot_loader is None:
            return
        try:
            snapshot = self._provider_snapshot_loader()
        except Exception:
            logger.exception("Failed to refresh provider config")
            return
        default_selection = preset_helpers.default_selection_signature(snapshot.signature)
        if self._active_preset and self._default_selection_signature in (None, default_selection):
            self._default_selection_signature = default_selection
            try:
                snapshot = self._build_model_preset_snapshot(self._active_preset)
            except Exception:
                logger.exception("Failed to refresh active model preset")
                return
        else:
            self._active_preset = None
            self._default_selection_signature = default_selection
        if snapshot.signature == self._provider_signature:
            return
        self._default_selection_signature = preset_helpers.default_selection_signature(snapshot.signature)
        self._apply_provider_snapshot(snapshot)

    async def route_expert_team_turn(
        self,
        *,
        history: list[dict[str, Any]],
        user_message: str,
        awaiting_target: bool = False,
        has_media: bool = False,
    ) -> dict[str, Any] | None:
        """Classify one selected asset-team turn with the active runtime model."""

        self._refresh_provider_snapshot()

        def _record_usage(usage: dict[str, int]) -> None:
            from nanobot.webui.token_usage import record_token_usage

            record_token_usage(
                usage,
                source="user",
                timezone_name=self.context.timezone,
            )

        try:
            gate = self._concurrency_gate or nullcontext()
            async with gate:
                return await asyncio.wait_for(
                    classify_expert_team_turn_with_model(
                        provider=self.provider,
                        model=self.model,
                        history=history,
                        user_message=user_message,
                        awaiting_target=awaiting_target,
                        has_media=has_media,
                        usage_callback=_record_usage,
                    ),
                    timeout=20,
                )
        except TimeoutError:
            logger.warning("Asset-research model routing timed out; using safe fallback")
        except Exception:
            logger.exception("Asset-research model routing failed; using safe fallback")
        return None

    @property
    def model_preset(self) -> str | None:
        return self._active_preset

    @model_preset.setter
    def model_preset(self, name: str | None) -> None:
        self.set_model_preset(name)

    def _build_model_preset_snapshot(self, name: str) -> ProviderSnapshot:
        return preset_helpers.build_runtime_preset_snapshot(
            name=name,
            presets=self.model_presets,
            provider=self.provider,
            loader=self._preset_snapshot_loader,
        )

    def set_model_preset(self, name: str | None, *, publish_update: bool = True) -> None:
        """Resolve a preset by name and apply all runtime model dependents."""
        name = preset_helpers.normalize_preset_name(name, self.model_presets)
        snapshot = self._build_model_preset_snapshot(name)
        self._apply_provider_snapshot(snapshot, publish_update=publish_update, model_preset=name)
        self._active_preset = name

    def _register_default_tools(self) -> None:
        """Register the default set of tools via plugin loader."""
        from nanobot.agent.tools.context import ToolContext
        from nanobot.agent.tools.loader import ToolLoader

        ctx = ToolContext(
            config=self.tools_config,
            workspace=str(self.workspace),
            bus=self.bus,
            subagent_manager=self.subagents,
            cron_service=self.cron_service,
            sessions=self.sessions,
            provider_snapshot_loader=self._provider_snapshot_loader,
            image_generation_provider_configs=self._image_generation_provider_configs,
            timezone=self.context.timezone or "UTC",
            workspace_sandbox=self.workspace_scopes.sandbox_status,
            runtime_events=self.runtime_events,
        )
        loader = ToolLoader()
        registered = loader.load(ctx, self.tools)

        # MyTool needs runtime state reference — manual registration
        if self.tools_config.my.enable:
            self.tools.register(
                MyTool(runtime_state=self, modify_allowed=self.tools_config.my.allow_set)
            )
            registered.append("my")

        logger.info("Registered {} tools: {}", len(registered), registered)

    async def _connect_mcp(self) -> None:
        """Connect configured MCP servers."""
        await agent_context.connect_mcp(self, self.tools)

    @property
    def is_ready(self) -> bool:
        """Whether the loop can consume and dispatch inbound messages."""
        return self._running

    @property
    def mcp_status(self) -> str:
        """Compact startup status exposed to native/WebUI clients."""
        if not self._mcp_servers:
            return "disabled"
        if self._mcp_connecting or (
            self._mcp_owner_task is not None and not self._mcp_warmup_complete
        ):
            return "warming"
        if self._mcp_connected:
            return "ready"
        return "unavailable" if self._mcp_warmup_complete else "pending"

    async def _run_mcp_owner(self) -> None:
        """Own startup MCP transports for their complete async lifecycle."""
        try:
            await self._connect_mcp()
            self._mcp_warmup_complete = True
            await self.bus.publish_outbound(OutboundMessage(
                channel="websocket",
                chat_id="",
                content="",
                metadata={
                    "_runtime_status_updated": True,
                    "agent_ready": self.is_ready,
                    "mcp_status": self.mcp_status,
                },
            ))
            if self._mcp_shutdown_event is not None:
                await self._mcp_shutdown_event.wait()
        finally:
            self._mcp_warmup_complete = True
            await self._close_mcp_stacks()

    def _set_tool_context(
        self, channel: str, chat_id: str,
        message_id: str | None = None, metadata: dict | None = None,
        session_key: str | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        from nanobot.agent.tools.context import ContextAware

        effective_key = session_key or session_key_for_channel(
            channel,
            chat_id,
            unified_session=self._unified_session,
        )
        request_ctx = RequestContext(
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            session_key=effective_key,
            metadata=dict(metadata or {}),
        )

        for name in self.tools.tool_names:
            tool = self.tools.get(name)
            if tool and isinstance(tool, ContextAware):
                tool.set_context(request_ctx)

    @staticmethod
    def _runtime_chat_id(msg: InboundMessage) -> str:
        """Return the chat id shown in runtime metadata for the model."""
        return str(msg.metadata.get("context_chat_id") or msg.chat_id)

    async def _build_bus_progress_callback(
        self, msg: InboundMessage
    ) -> Callable[..., Awaitable[None]]:
        """Build a progress callback that publishes to the message bus."""
        return build_bus_progress_callback(self.bus, msg)

    async def _build_retry_wait_callback(
        self, msg: InboundMessage
    ) -> Callable[[str], Awaitable[None]]:
        """Build a retry-wait callback that publishes to the message bus."""

        async def _on_retry_wait(content: str) -> None:
            meta = dict(msg.metadata or {})
            meta["_retry_wait"] = True
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        return _on_retry_wait

    def _runtime_events(self) -> RuntimeEventPublisher:
        return ensure_runtime_event_publisher(self)

    @staticmethod
    def _ensure_runtime_turn_identity(
        msg: InboundMessage,
        session_key: str,
    ) -> InboundMessage:
        metadata = dict(msg.metadata or {})
        runtime_turn_id = metadata.get("_runtime_turn_id")
        if not isinstance(runtime_turn_id, str) or not runtime_turn_id.strip():
            webui_turn_id = metadata.get("webui_turn_id")
            runtime_turn_id = (
                str(webui_turn_id).strip()
                if isinstance(webui_turn_id, str) and webui_turn_id.strip()
                else f"{session_key}:{time.time_ns()}"
            )
            metadata["_runtime_turn_id"] = runtime_turn_id
        if metadata == (msg.metadata or {}):
            return msg
        return dataclasses.replace(msg, metadata=metadata)

    @staticmethod
    def _terminal_status_for_response(
        response: OutboundMessage | None,
    ) -> tuple[TurnStatus, FinishReason, str | None]:
        stop_reason = str((response.metadata if response is not None else {}).get(
            "_stop_reason",
            "",
        ))
        if stop_reason in {"error", "tool_error", "workflow_error"}:
            reason = (
                FinishReason.TOOL_ERROR
                if stop_reason in {"tool_error", "workflow_error"}
                else FinishReason.MODEL_ERROR
            )
            return TurnStatus.FAILED, reason, stop_reason
        if stop_reason == "cancelled":
            return TurnStatus.INTERRUPTED, FinishReason.USER_INTERRUPTED, stop_reason
        return TurnStatus.COMPLETED, FinishReason.SUCCESS, None

    async def _start_runtime_turn(
        self,
        msg: InboundMessage,
        session_key: str,
        *,
        started_at: float | None = None,
    ) -> None:
        project_context = msg.metadata.get(PROJECT_CONTEXT_METADATA_KEY)
        project_id = (
            str(project_context.get("project_id")).strip()
            if isinstance(project_context, dict) and project_context.get("project_id")
            else None
        )
        session_id = (
            str(project_context.get("session_id")).strip()
            if isinstance(project_context, dict) and project_context.get("session_id")
            else None
        )
        await self.turn_lifecycle.start_turn(
            context=RuntimeEventContext(
                channel=msg.channel,
                chat_id=msg.chat_id,
                session_key=session_key,
                metadata=dict(msg.metadata or {}),
            ),
            turn_id=str(msg.metadata.get("_runtime_turn_id") or ""),
            project_id=project_id,
            session_id=session_id,
            started_at=started_at,
        )

    async def _finish_runtime_turn(
        self,
        msg: InboundMessage,
        session_key: str,
        *,
        status: TurnStatus,
        finish_reason: FinishReason,
        error_code: str | None = None,
        error_message: str | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        turn_id = str((msg.metadata or {}).get("_runtime_turn_id") or "").strip()
        if not turn_id:
            logger.error("Cannot finish runtime turn without id for session {}", session_key)
            await self.thread_runtime_registry.set_system_error(
                session_key,
                error_code="TURN_ID_REQUIRED",
            )
            return
        try:
            await self.turn_lifecycle.finish_turn(
                session_key=session_key,
                expected_turn_id=turn_id,
                status=status,
                finish_reason=finish_reason,
                error_code=error_code,
                error_message=error_message,
                usage=usage,
            )
        except TurnLifecycleError as exc:
            logger.error(
                "Runtime turn finalization failed session={} turn={} code={}: {}",
                session_key,
                turn_id,
                exc.code,
                exc,
            )
            await self.thread_runtime_registry.set_system_error(
                session_key,
                error_code=exc.code,
            )
        except Exception:
            logger.exception(
                "Runtime terminal persistence failed session={} turn={}",
                session_key,
                turn_id,
            )
            await self.thread_runtime_registry.set_system_error(
                session_key,
                error_code="TURN_TERMINAL_PERSIST_FAILED",
            )

    async def _commit_runtime_final_answer(
        self,
        msg: InboundMessage,
        session_key: str,
        response: OutboundMessage,
    ) -> OutboundMessage:
        """Durably commit one final answer before entering the terminal barrier."""
        turn_id = str((msg.metadata or {}).get("_runtime_turn_id") or "").strip()
        if not turn_id:
            raise TurnLifecycleError(
                "TURN_ID_REQUIRED",
                f"cannot commit a final answer for {session_key!r} without a turn id",
            )
        metadata = {
            **dict(msg.metadata or {}),
            **dict(response.metadata or {}),
        }
        prepared = dataclasses.replace(response, metadata=metadata)
        await self.turn_lifecycle.commit_final_answer(
            session_key=session_key,
            expected_turn_id=turn_id,
        )
        canonical_event = await self._runtime_events().final_answer_committed(
            prepared,
            session_key,
        )
        if canonical_event is None:
            return prepared
        return dataclasses.replace(
            prepared,
            metadata={
                **prepared.metadata,
                "_final_answer_persisted": True,
                "_canonical_event": canonical_event,
            },
        )

    async def submit_cron_turn(self, msg: InboundMessage) -> OutboundMessage | None:
        return await self._cron_turns.submit(msg)

    def pending_cron_job_ids_for_session(self, session_key: str) -> set[str]:
        return self._cron_turns.pending_job_ids_for_session(session_key)

    def _persist_user_message_early(
        self,
        msg: InboundMessage,
        session: Session,
        **kwargs: Any,
    ) -> bool:
        """Persist the triggering user message before the turn starts.

        Returns True if the message was persisted.
        """
        if not turn_continuation.should_persist_user_message(msg.metadata):
            return False
        media_paths = [p for p in (msg.media or []) if isinstance(p, str) and p]
        has_text = isinstance(msg.content, str) and msg.content.strip()
        if has_text or media_paths:
            extra: dict[str, Any] = ({"media": list(media_paths)} if media_paths else {}) | agent_context.session_extra(msg.metadata)
            expert_team = _expert_team_binding(msg.metadata)
            if expert_team is not None:
                extra[EXPERT_TEAM_TURN_KEY] = expert_team.get("id") or True
            extra.update(kwargs)
            text = msg.content if isinstance(msg.content, str) else ""
            text_override, cron_extra = cron_history_overrides(msg.metadata)
            if text_override is not None:
                text = text_override
            extra.update(cron_extra)
            session.add_message("user", text, **extra)
            self._mark_pending_user_turn(session)
            self.sessions.save(session)
            return True
        return False

    def _build_initial_messages(
        self,
        msg: InboundMessage,
        session: Session,
        history: list[dict[str, Any]],
        pending_summary: str | None,
        include_memory_recent_history: bool = True,
    ) -> list[dict[str, Any]]:
        """Build the initial message list for the LLM turn."""
        scope = self.workspace_scopes.for_message(msg, session.metadata)
        msg.metadata["skill_scope"] = _project_skill_scope(self.workspace, scope.project_path, msg.metadata)
        return self.context.build_messages(
            history=history,
            current_message=image_generation_prompt(msg.content, msg.metadata),
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=self._runtime_chat_id(msg),
            sender_id=msg.sender_id,
            session_summary=pending_summary,
            session_metadata=session.metadata,
            workspace=scope.project_path,
            runtime_state=self,
            inbound_message=msg,
            include_memory_recent_history=include_memory_recent_history,
            session_key=session.key,
            unified_session=self._unified_session,
        )

    def _memory_store_for_session(
        self,
        session: Session,
        msg: InboundMessage | None = None,
    ) -> Any | None:
        # Project-bound chats must never feed their turn summaries into the
        # global history, even when the project's canonical root happens to be
        # the runtime workspace itself (the desktop app's common case).
        if isinstance(session.metadata.get("project_id"), str):
            return None
        if msg is not None:
            scope = self.workspace_scopes.for_message(msg, session.metadata)
        else:
            channel = session.key.split(":", 1)[0] if ":" in session.key else None
            scope = self.workspace_scopes.for_turn(
                channel=channel,
                message_metadata=None,
                session_metadata=session.metadata,
            )
        runtime_root = self.workspace.expanduser().resolve(strict=False)
        active_root = scope.project_path.expanduser().resolve(strict=False)
        return self.context.memory if active_root == runtime_root else None

    def _consolidator_for_session(
        self,
        session: Session,
        msg: InboundMessage | None = None,
    ) -> Consolidator | None:
        store = self._memory_store_for_session(session, msg)
        if store is self.context.memory:
            return self.consolidator
        return self.session_consolidator

    async def _dispatch_command_inline(
        self,
        msg: InboundMessage,
        key: str,
        raw: str,
        dispatch_fn: Callable[[CommandContext], Awaitable[OutboundMessage | None]],
    ) -> None:
        """Dispatch a command directly from the run() loop and publish the result."""
        ctx = CommandContext(msg=msg, session=None, key=key, raw=raw, loop=self)
        result = await dispatch_fn(ctx)
        if result:
            await self.bus.publish_outbound(result)
        else:
            logger.warning("Command '{}' matched but dispatch returned None", raw)

    async def _cancel_active_tasks(self, key: str) -> int:
        """Cancel and await all active tasks and subagents for *key*.

        Returns the total number of cancelled tasks + subagents.
        """
        tasks = self._active_tasks.pop(key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        # Cancel subagents before awaiting the main turn. The main turn may be
        # blocked waiting for those same subagents in _drain_pending.
        sub_cancelled = await self.subagents.cancel_by_session(key)
        for t in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await t
        active_turn = await self.thread_runtime_registry.active_turn(key)
        if active_turn is not None:
            await self.turn_lifecycle.finish_turn(
                session_key=key,
                expected_turn_id=active_turn.id,
                status=TurnStatus.INTERRUPTED,
                finish_reason=FinishReason.USER_INTERRUPTED,
            )
        return cancelled + sub_cancelled

    def _effective_session_key(self, msg: InboundMessage) -> str:
        """Return the session key used for task routing and mid-turn injections."""
        if self._unified_session and not msg.session_key_override:
            return UNIFIED_SESSION_KEY
        return msg.session_key

    def _replay_token_budget(self) -> int:
        """Derive a token budget for session history replay from the context window."""
        if self.context_window_tokens <= 0:
            return 0
        max_output = getattr(getattr(self.provider, "generation", None), "max_tokens", 4096)
        try:
            reserved_output = int(max_output)
        except (TypeError, ValueError):
            reserved_output = 4096
        budget = self.context_window_tokens - max(1, reserved_output) - 1024
        return budget if budget > 0 else max(128, self.context_window_tokens // 2)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
        *,
        session: Session | None = None,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        pending_queue: asyncio.Queue | None = None,
        ephemeral: bool = False,
        run_extra_hooks_for_ephemeral: bool = False,
        hooks: list[AgentHook] | None = None,
        tools: ToolRegistry | None = None,
        max_iterations: int | None = None,
    ) -> tuple[str | None, list[str], list[dict], str, bool]:
        """Run the agent iteration loop.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming, stream_kind)*: called when a stream segment finishes.
        ``resuming=True`` means another model/tool segment follows (spinner should restart);
        ``resuming=False`` means this is the final response.
        ``stream_kind`` classifies the completed segment as public pre-tool
        ``narration`` or normal ``answer`` text when the callback accepts it.

        Returns (final_content, tools_used, messages, stop_reason, had_injections).
        """
        self._sync_subagent_runtime_limits()

        loop_hook = AgentProgressHook(
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            metadata=metadata,
            session_key=session_key,
            tool_hint_max_length=self.tool_hint_max_length,
            set_tool_context=self._set_tool_context,
            on_iteration=lambda iteration: setattr(self, "_current_iteration", iteration),
        )
        run_hooks = [*self._extra_hooks, *(hooks or [])]
        hook: AgentHook = loop_hook
        if run_hooks and (not ephemeral or run_extra_hooks_for_ephemeral):
            hook = CompositeHook([loop_hook, *run_hooks])
        active_objective = _active_turn_objective(initial_messages)
        active_corrections: list[str] = []
        correction_review_generation = 0
        correction_reviewed_generation = 0

        async def _checkpoint(payload: dict[str, Any]) -> None:
            if session is None:
                return
            checkpoint = dict(payload)
            source_turn_id = str((metadata or {}).get("_runtime_turn_id") or "").strip()
            if source_turn_id:
                checkpoint["_source_turn_id"] = source_turn_id
            self._set_runtime_checkpoint(session, checkpoint)

        async def _drain_pending(*, limit: int = _MAX_INJECTIONS_PER_TURN) -> list[dict[str, Any]]:
            """Drain follow-up messages from the pending queue.

            When no messages are immediately available but sub-agents
            spawned in this dispatch are still running, blocks until at
            least one result arrives (or timeout).  This keeps the runner
            loop alive so subsequent sub-agent completions are consumed
            in-order rather than dispatched separately.
            """
            if pending_queue is None:
                return []

            def _to_user_message(pending_msg: InboundMessage) -> dict[str, Any]:
                nonlocal correction_review_generation
                content = pending_msg.content
                media = pending_msg.media if pending_msg.media else None
                if media:
                    content, media = self._prepare_message_media(content, media)
                    media = media or None
                user_content = self.context._build_user_content(content, media)
                if (
                    isinstance(pending_msg.metadata, dict)
                    and pending_msg.metadata.get(ACTIVE_TURN_CORRECTION_METADATA_KEY) is True
                ):
                    user_content = _wrap_active_turn_correction(
                        user_content,
                        objective=active_objective,
                    )
                    correction_text = pending_msg.content.strip()
                    if correction_text:
                        active_corrections.append(truncate_text_fn(correction_text, 600))
                    correction_review_generation += 1
                return {"role": "user", "content": user_content}

            expert_team_wait = _expert_team_binding(
                metadata,
                session.metadata if session is not None else None,
            ) is not None

            def _is_subagent_result(pending_msg: InboundMessage) -> bool:
                return bool(
                    isinstance(pending_msg.metadata, dict)
                    and pending_msg.metadata.get("injected_event") == "subagent_result"
                )

            def _announced_task_ids(pending_messages: list[InboundMessage]) -> set[str]:
                return {
                    str(msg.metadata.get("subagent_task_id"))
                    for msg in pending_messages
                    if _is_subagent_result(msg) and msg.metadata.get("subagent_task_id")
                }

            def _remaining_unannounced(pending_messages: list[InboundMessage]) -> set[str]:
                if session is None:
                    return set()
                return (
                    self.subagents.get_running_task_ids_by_session(session.key)
                    - _announced_task_ids(pending_messages)
                )

            pending_messages: list[InboundMessage] = []
            while len(pending_messages) < limit:
                try:
                    pending_messages.append(pending_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            # Block if nothing drained but sub-agents spawned in this dispatch
            # are still running.  Keeps the runner loop alive so subsequent
            # completions are injected in-order rather than dispatched separately.
            if (not pending_messages
                    and session is not None
                    and self.subagents.get_running_count_by_session(session.key) > 0):
                try:
                    # A four-role research team can legitimately take longer
                    # than a single background helper.  Do not resume the Team
                    # Lead after five minutes while members are still running;
                    # that causes it to misclassify unfinished members as
                    # failed.  Keep a finite upper bound for truly hung work.
                    wait_timeout = 600 if expert_team_wait else 300
                    msg = await asyncio.wait_for(
                        pending_queue.get(),
                        timeout=wait_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timeout waiting for sub-agent completion in session {}",
                        session.key,
                    )
                    return []
                pending_messages.append(msg)
                while len(pending_messages) < limit:
                    try:
                        pending_messages.append(pending_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

            # A team must be handed to its lead as one internal result bundle. If
            # each member completion is processed independently, the model tends
            # to summarize that member and treats the final summary as the whole
            # turn's answer. Wait for every still-unannounced member here, then
            # append a deterministic continuation contract below.
            if expert_team_wait and session is not None and pending_messages:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 600
                while _remaining_unannounced(pending_messages):
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        logger.warning(
                            "Timeout batching expert-team results in session {}",
                            session.key,
                        )
                        break
                    try:
                        pending_messages.append(await asyncio.wait_for(
                            pending_queue.get(),
                            timeout=remaining,
                        ))
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Timeout waiting for remaining expert-team members in session {}",
                            session.key,
                        )
                        break

                # Consume results that raced with the last awaited announcement.
                while True:
                    try:
                        pending_messages.append(pending_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                result_messages = [msg for msg in pending_messages if _is_subagent_result(msg)]
                other_messages = [msg for msg in pending_messages if not _is_subagent_result(msg)]
                items: list[dict[str, Any]] = []
                if result_messages:
                    bundle = "\n\n---\n\n".join(msg.content for msg in result_messages)
                    remaining_ids = _remaining_unannounced(pending_messages)
                    if remaining_ids:
                        bundle += (
                            "\n\n[Expert-team runtime coordination]\n"
                            "Some team members are still running. Treat the material above as "
                            "internal evidence only. Do not publish a member summary, do not ask "
                            "the user what to do, and do not close the workflow; continue waiting "
                            "for the remaining member deliveries."
                        )
                    else:
                        bundle += (
                            "\n\n[Expert-team runtime coordination: all members terminal]\n"
                            "All research members have now completed or exhausted their retry. "
                            "Do not answer with separate member summaries and do not ask the user "
                            "whether to continue. Immediately continue the canonical workflow: "
                            "(1) update member steps from these actual results; (2) mark `team-lead` "
                            "running and synthesize the completed reports; (3) personally fill any "
                            "failed dimension from the verified structured data package and the "
                            "other reports; (4) write the full report artifact; (5) mark `team-lead` "
                            "completed and `report-audit` running, execute the required data audit "
                            "and correct material discrepancies; (6) mark `report-audit` completed "
                            "and only then deliver the final report to the user. A single failed "
                            "member is a degradable gap, not permission to end the team workflow."
                        )
                    items.append({"role": "user", "content": bundle})
                items.extend(_to_user_message(msg) for msg in other_messages)
                return items[:limit]

            return [_to_user_message(msg) for msg in pending_messages[:limit]]

        active_session_key = session.key if session else session_key
        effective_scope = self.workspace_scopes.for_turn(
            channel=channel,
            message_metadata=metadata,
            session_metadata=session.metadata if session is not None else None,
        )
        request_ctx = RequestContext(
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            session_key=active_session_key,
            metadata=dict(metadata or {}),
        )
        file_state_token = bind_file_states(self._file_state_store.for_session(active_session_key))
        request_token = bind_request_context(request_ctx)
        workspace_token = bind_workspace_scope(effective_scope)
        bound_project_context = project_context_from_metadata(
            (metadata or {}).get(PROJECT_CONTEXT_METADATA_KEY),
            session_key=active_session_key,
            root_path=effective_scope.project_path,
        )
        project_context_token = bind_project_context(bound_project_context)
        skill_scope_token = bind_allowed_workspace_skills(
            _project_skill_scope(self.workspace, effective_scope.project_path, metadata or {})
        )
        # Compute lazily because long_task may create goal metadata during this run.
        def _goal_continue() -> str | None:
            _goal_lines = goal_state_runtime_lines(session.metadata if session is not None else None)
            if not _goal_lines:
                return None
            return (
                "You have an active sustained goal:\n\n"
                + "\n".join(_goal_lines)
                + "\n\nPlease continue working toward the objective using your tools, "
                "or call complete_goal if the work is truly finished."
            )

        session_metadata = session.metadata if session is not None else None
        expert_team = _expert_team_binding(metadata, session_metadata)
        expert_team_active = expert_team is not None
        expert_team_completion = (
            expert_team.get("completion")
            if isinstance(expert_team, dict)
            and isinstance(expert_team.get("completion"), dict)
            else None
        )
        initial_message_count = len(initial_messages)
        run_max_iterations = (
            self.max_iterations
            if max_iterations is None
            else max(1, int(max_iterations))
        )

        async def _provider_timing(payload: dict[str, Any]) -> None:
            logs = self._performance_logs
            if logs is None:
                return
            event = str(payload.get("event") or "")
            duration_ms = (
                max(0, int(payload.get("provider_ttft_ms") or 0))
                if event == "first_event"
                else None
            )
            project_id = (
                bound_project_context.project_id
                if bound_project_context is not None
                else None
            )
            state_session_id = (
                bound_project_context.session_id
                if bound_project_context is not None
                else None
            )
            turn_id = str((metadata or {}).get("_runtime_turn_id") or "").strip() or None
            details = dict(payload)
            details["session_key"] = active_session_key
            if event == "first_event":
                logger.info(
                    "provider_ttft_ms={} measurement={} first_event={} provider={} "
                    "model={} iteration={} prompt_estimate={} project_id={} "
                    "session_id={} turn_id={}",
                    duration_ms,
                    details.get("measurement"),
                    details.get("first_event"),
                    details.get("provider"),
                    details.get("model"),
                    details.get("iteration"),
                    details.get("prompt_estimate"),
                    project_id or "-",
                    state_session_id or "-",
                    turn_id or "-",
                )
            await asyncio.to_thread(
                logs.write,
                level="info",
                component="provider",
                event_name=(
                    "provider_ttft"
                    if event == "first_event"
                    else "provider_request_started"
                ),
                message=(
                    f"provider first event after {duration_ms} ms"
                    if event == "first_event"
                    else "provider request started"
                ),
                request_id=message_id,
                project_id=project_id,
                session_id=state_session_id,
                turn_id=turn_id,
                duration_ms=duration_ms,
                details=details,
            )

        def _expert_team_injections_pending() -> bool:
            """Allow team results past the normal cycle cap while work is active."""
            if not expert_team_active or pending_queue is None or session is None:
                return False
            return (
                not pending_queue.empty()
                or self.subagents.get_running_count_by_session(session.key) > 0
            )

        def _final_response_guard(messages: list[dict[str, Any]]) -> str | None:
            nonlocal correction_reviewed_generation
            guards: list[str] = []
            if expert_team_completion is not None:
                expert_guard = _expert_team_completion_guard_message(
                    expert_team,
                    messages[initial_message_count:],
                )
                if expert_guard:
                    guards.append(expert_guard)
            if correction_review_generation > correction_reviewed_generation:
                correction_reviewed_generation = correction_review_generation
                correction_summary = "\n".join(
                    f"- {item}" for item in active_corrections[-3:]
                )
                guards.append(
                    "[Active-turn correction review]\n"
                    "Before finalizing, review the draft against the active objective and "
                    "the user's latest correction below. If the draft answers an older or "
                    "unrelated topic, discard it and answer the active task instead. Ensure "
                    "the result contains the requested subject and deliverable, and that any "
                    "source fallback requested by the user was actually applied.\n"
                    f"Active objective:\n{active_objective}\n"
                    f"Corrections:\n{correction_summary or '- Apply the latest injected correction.'}"
                )
            return "\n\n".join(guards) or None

        try:
            result = await self.runner.run(AgentRunSpec(
                initial_messages=initial_messages,
                tools=tools or self.tools,
                model=self.model,
                max_iterations=run_max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
                hook=hook,
                error_message="Sorry, I encountered an error calling the AI model.",
                concurrent_tools=True,
                workspace=effective_scope.project_path,
                session_key=session.key if session else None,
                context_window_tokens=self.context_window_tokens,
                context_block_limit=self.context_block_limit,
                provider_retry_mode=self.provider_retry_mode,
                progress_callback=on_progress,
                stream_progress_deltas=on_stream is not None,
                retry_wait_callback=on_retry_wait,
                checkpoint_callback=_checkpoint,
                injection_callback=_drain_pending,
                injection_overflow_predicate=(
                    _expert_team_injections_pending if expert_team_active else None
                ),
                final_response_guard=_final_response_guard,
                # Sustained goals may legitimately exceed NANOBOT_LLM_TIMEOUT_S; idle stall
                # is still capped by NANOBOT_STREAM_IDLE_TIMEOUT_S in streaming providers.
                llm_timeout_s=runner_wall_llm_timeout_s(
                    self.sessions,
                    session.key if session is not None else session_key,
                    metadata=session_metadata,
                    message_metadata=metadata,
                ),
                goal_active_predicate=lambda: sustained_goal_active(session.metadata) if session is not None else False,
                goal_continue_message=_goal_continue,
                enforce_finance_source_priority=(
                    (
                        isinstance(expert_team, dict)
                        and expert_team.get("id") == "asset-research-team"
                    )
                    or bool((metadata or {}).get("_enforce_finance_source_priority"))
                ),
                finalize_on_max_iterations=turn_continuation.should_finalize_on_max_iterations(
                    pending_queue_available=(
                        pending_queue is not None and session is not None
                    ),
                    session_metadata=session_metadata,
                    message_metadata=metadata,
                ),
                provider_timing_callback=(
                    _provider_timing if self._performance_logs is not None else None
                ),
            ))
        finally:
            reset_allowed_workspace_skills(skill_scope_token)
            reset_project_context(project_context_token)
            reset_workspace_scope(workspace_token)
            reset_request_context(request_token)
            reset_file_states(file_state_token)
        self._last_usage = result.usage
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", run_max_iterations)
            should_stream = turn_continuation.should_stream_budget_response(
                stop_reason=result.stop_reason,
                pending_queue_available=pending_queue is not None and session is not None,
                session_metadata=session_metadata,
                message_metadata=metadata,
            )
            # Push final content through stream so streaming channels (e.g. Feishu)
            # update the card instead of leaving it empty.
            if on_stream and on_stream_end and should_stream:
                await on_stream(result.final_content or "")
                await on_stream_end(resuming=False)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", (result.final_content or "")[:200])
        return result.final_content, result.tools_used, result.messages, result.stop_reason, result.had_injections

    @staticmethod
    def _workflow_tool_subset(
        registry: ToolRegistry,
        *,
        allowed_names: set[str],
        allowed_prefixes: tuple[str, ...],
    ) -> ToolRegistry:
        """Build a capability-scoped registry for one fixed graph node."""

        scoped = ToolRegistry()
        for name in registry.tool_names:
            if name not in allowed_names and not name.startswith(allowed_prefixes):
                continue
            tool = registry.get(name)
            if tool is not None:
                scoped.register(tool)
        return scoped

    async def _run_asset_research_workflow(
        self,
        ctx: TurnContext,
    ) -> tuple[str, list[str], list[dict[str, Any]], str, bool]:
        """Execute the runtime-owned asset-research DAG for one user turn."""

        team = _expert_team_binding(ctx.msg.metadata, ctx.session.metadata)
        if not isinstance(team, dict) or team.get("id") != ASSET_RESEARCH_TEAM_ID:
            raise RuntimeError("asset-research workflow invoked without its team binding")
        run_id = str(ctx.msg.metadata.get("expert_team_run_id") or "").strip()
        if not run_id:
            raise RuntimeError("asset-research workflow requires a run id")
        raw_route = ctx.msg.metadata.get(EXPERT_TEAM_TURN_ROUTE_KEY)
        target = (
            str(raw_route.get("target") or "").strip()
            if isinstance(raw_route, dict)
            else ""
        )
        if not target:
            raise RuntimeError("asset-research workflow requires a validated target")

        source_registry = ctx.tools or self.tools
        configured_prefixes = tuple(
            f"mcp_{str(item.get('name') or '').strip().lower()}_"
            for item in team.get("mcp_presets", [])
            if isinstance(item, dict)
            and item.get("configured") is True
            and str(item.get("name") or "").strip()
        )
        data_tools = self._workflow_tool_subset(
            source_registry,
            allowed_names={"web_search", "web_fetch"},
            allowed_prefixes=configured_prefixes,
        )
        report_tools = self._workflow_tool_subset(
            source_registry,
            allowed_names={
                "web_search",
                "web_fetch",
                "read_file",
                "write_file",
                "edit_file",
                "create_research_chart",
            },
            allowed_prefixes=configured_prefixes,
        )
        audit_tools = _SingleRewriteAuditToolRegistry(
            self._workflow_tool_subset(
                source_registry,
                allowed_names={
                    "read_file",
                    "write_file",
                    "web_search",
                    "web_fetch",
                },
                allowed_prefixes=configured_prefixes,
            )
        )

        system_content = next(
            (
                message.get("content")
                for message in ctx.initial_messages
                if message.get("role") == "system"
            ),
            "",
        )
        system_text = _message_content_text(system_content)
        node_system = (
            "# Runtime-owned Asset Research Graph\n\n"
            "The graph runtime is the sole control-flow authority. Execute only the "
            "node named in the user message. Never select, skip, or simulate another "
            "node and never publish model-authored workflow progress.\n\n"
            f"{system_text}"
        )
        node_metadata = {
            **dict(ctx.msg.metadata or {}),
            EXPERT_TEAM_TURN_SUPPRESSED_KEY: True,
            "webui": False,
            "_enforce_finance_source_priority": True,
        }
        node_tool_map = {
            "data-package": data_tools,
            "team-lead": report_tools,
            "report-audit": audit_tools,
        }

        async def _run_node(
            node_id: str,
            prompt: str,
            final_stream: bool,
        ) -> AgentNodeOutcome:
            node_messages = [
                {"role": "system", "content": node_system},
                {"role": "user", "content": prompt},
            ]
            render_template = (
                "research_report"
                if node_id in {TEAM_LEAD, REPORT_AUDIT}
                else "simple"
            )
            final_content, tools_used, messages, stop_reason, _had_injections = (
                await self._run_agent_loop(
                    node_messages,
                    on_progress=ctx.on_progress,
                    on_stream=ctx.on_stream if final_stream else None,
                    on_stream_end=ctx.on_stream_end if final_stream else None,
                    on_retry_wait=ctx.on_retry_wait,
                    session=None,
                    channel=ctx.msg.channel,
                    chat_id=ctx.msg.chat_id,
                    message_id=f"{ctx.msg.metadata.get('message_id') or ctx.turn_id}:{node_id}",
                    metadata={
                        **node_metadata,
                        "_asset_research_graph_node": node_id,
                        HTML_TEMPLATE_METADATA_KEY: render_template,
                    },
                    session_key=ctx.session_key,
                    pending_queue=None,
                    ephemeral=True,
                    tools=node_tool_map[node_id],
                    max_iterations=(
                        AUDIT_MAX_TOOL_ITERATIONS
                        if node_id == REPORT_AUDIT
                        else None
                    ),
                )
            )
            return AgentNodeOutcome(
                content=final_content or "",
                stop_reason=stop_reason,
                tools_used=list(tools_used or []),
                messages=messages,
                usage=dict(self._last_usage),
                artifacts=_generated_artifact_paths(messages),
            )

        effective_scope = self.workspace_scopes.for_turn(
            channel=ctx.msg.channel,
            message_metadata=ctx.msg.metadata,
            session_metadata=ctx.session.metadata,
        )
        workflow_corrections: list[str] = []

        async def _run_members(tasks: dict[str, str]) -> MemberBatchOutcome:
            task_ids: list[str] = []
            member_by_task: dict[str, str] = {}
            immediate: dict[str, MemberNodeOutcome] = {}
            for member_id in MEMBER_NODES:
                task_id = await self.subagents.spawn_for_workflow(
                    task=tasks[member_id],
                    label=member_id,
                    origin_channel=ctx.msg.channel,
                    origin_chat_id=ctx.msg.chat_id,
                    session_key=ctx.session_key,
                    origin_message_id=str(ctx.msg.metadata.get("message_id") or "") or None,
                    workspace_scope=effective_scope,
                    expert_team=team,
                    expert_team_run_id=run_id,
                )
                if re.fullmatch(r"[0-9a-f]{8}", task_id):
                    task_ids.append(task_id)
                    member_by_task[task_id] = member_id
                else:
                    immediate[member_id] = MemberNodeOutcome(
                        member_id=member_id,
                        status="failed",
                        content=task_id,
                        activity="该角色未能启动，主笔将按降级流程补齐",
                    )

            completed = (
                await self.subagents.wait_for_workflow_tasks(task_ids)
                if task_ids
                else []
            )
            members = dict(immediate)
            for result in completed:
                member_id = member_by_task[result.task_id]
                artifact_match = re.search(r"Role artifact: `([^`]+)`", result.content)
                status = (
                    "completed" if result.status == "ok"
                    else "cancelled" if result.status == "cancelled"
                    else "failed"
                )
                members[member_id] = MemberNodeOutcome(
                    member_id=member_id,
                    status=status,
                    content=result.content,
                    artifact=artifact_match.group(1) if artifact_match else None,
                    activity=(
                        "研究完成，完整结果已交付 Team Lead"
                        if status == "completed"
                        else "该角色结果已降级，Team Lead 将补齐缺失维度"
                    ),
                )

            supplements: list[str] = []
            if ctx.pending_queue is not None:
                while True:
                    try:
                        pending = ctx.pending_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if not isinstance(pending, InboundMessage):
                        continue
                    content = pending.content.strip()
                    if content:
                        supplements.append(content)
                    supplements.extend(
                        str(path) for path in (pending.media or []) if str(path).strip()
                    )
            workflow_corrections.extend(supplements)
            return MemberBatchOutcome(members=members, supplements=supplements)

        async def _publish_state(
            state: dict[str, Any],
            event: str,
            activity: str,
        ) -> None:
            public_state = public_asset_research_state(state)
            await self.bus.publish_outbound(OutboundMessage(
                channel=ctx.msg.channel,
                chat_id=ctx.msg.chat_id,
                content="",
                metadata={
                    **dict(ctx.msg.metadata or {}),
                    "_team_graph_updated": True,
                    "team_graph": {
                        "run_id": run_id,
                        "team_id": ASSET_RESEARCH_TEAM_ID,
                        "event": event,
                        "activity": activity,
                        "state": public_state,
                    },
                },
            ))

        resume_metadata = ctx.msg.metadata.get(EXPERT_TEAM_RESUME_KEY)
        resume_state = (
            resume_metadata.get("graph_state")
            if isinstance(resume_metadata, dict)
            and isinstance(resume_metadata.get("graph_state"), dict)
            else None
        )
        resume_artifacts = (
            [
                str(item) for item in resume_metadata.get("artifacts", [])
                if str(item).strip()
            ]
            if isinstance(resume_metadata, dict)
            else []
        )
        request = ctx.msg.content.strip()
        safe_target = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "-", target).strip("-")
        report_path = f"reports/{(safe_target or 'stock')[:48]}-{run_id}-投资研究报告.md"

        file_state_token = bind_file_states(
            self._file_state_store.for_session(ctx.session_key)
        )
        request_token = bind_request_context(RequestContext(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            message_id=str(ctx.msg.metadata.get("message_id") or "") or None,
            session_key=ctx.session_key,
            metadata=dict(ctx.msg.metadata or {}),
        ))
        workspace_token = bind_workspace_scope(effective_scope)
        bound_project_context = project_context_from_metadata(
            ctx.msg.metadata.get(PROJECT_CONTEXT_METADATA_KEY),
            session_key=ctx.session_key,
            root_path=effective_scope.project_path,
        )
        project_context_token = bind_project_context(bound_project_context)
        try:
            runtime = AssetResearchWorkflowRuntime(
                run_agent_node=_run_node,
                run_member_wave=_run_members,
                publish_state=_publish_state,
            )
            outcome = await runtime.run(
                run_id=run_id,
                target=target,
                request=request,
                team=team,
                report_path=report_path,
                resume_from=resume_state,
                supplemental_artifacts=[*resume_artifacts, *(ctx.msg.media or [])],
            )
        finally:
            reset_project_context(project_context_token)
            reset_workspace_scope(workspace_token)
            reset_request_context(request_token)
            reset_file_states(file_state_token)

        self._last_usage = dict(outcome.usage)
        ctx.artifact_paths = list(outcome.artifacts)
        messages = [
            *ctx.initial_messages,
            *(
                [{
                    "role": "user",
                    "content": (
                        "[Active-turn user correction]\n"
                        + "\n".join(workflow_corrections)
                    ),
                }]
                if workflow_corrections
                else []
            ),
            {"role": "assistant", "content": outcome.final_content},
        ]
        return (
            outcome.final_content,
            outcome.tools_used,
            messages,
            outcome.stop_reason,
            bool(workflow_corrections),
        )

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        self._mcp_shutdown_event = asyncio.Event()
        if self.trace_collector is not None:
            abandoned = await self.trace_collector.recover_abandoned(
                runtime_epoch=self.thread_runtime_registry.runtime_epoch,
            )
            if abandoned:
                logger.warning("Recovered {} abandoned runtime trace(s)", abandoned)
            self._schedule_background(self.trace_collector.apply_retention())
        if self._mcp_servers:
            self._mcp_warmup_complete = False
            self._mcp_owner_task = asyncio.create_task(
                self._run_mcp_owner(),
                name="nanobot-mcp-warmup",
            )
        logger.info("Agent loop started")
        try:
            while self._running:
                try:
                    msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
                except asyncio.TimeoutError:
                    self.auto_compact.check_expired(
                        self._schedule_background,
                        active_session_keys=self._pending_queues.keys(),
                    )
                    continue
                except asyncio.CancelledError:
                    # Preserve real task cancellation so shutdown can complete cleanly.
                    # Only ignore non-task CancelledError signals that may leak from integrations.
                    if not self._running or asyncio.current_task().cancelling():
                        raise
                    continue
                except Exception as e:
                    logger.warning("Error consuming inbound message: {}, continuing...", e)
                    continue

                raw = msg.content.strip()
                effective_key = self._effective_session_key(msg)
                if await agent_context.handle_runtime_control(self, msg, self.tools):
                    continue
                if self.commands.is_priority(raw):
                    await self._dispatch_command_inline(
                        msg, effective_key, raw,
                        self.commands.dispatch_priority,
                    )
                    continue
                if self._cron_turns.defer_if_active(
                    msg,
                    session_key=effective_key,
                    active_session_keys=self._pending_queues.keys(),
                ):
                    logger.info(
                        "Deferred cron turn for active session {}",
                        effective_key,
                    )
                    continue
                # If this session already has an active pending queue (i.e. a task
                # is processing this session), route the message there for mid-turn
                # injection instead of creating a competing task.
                if effective_key in self._pending_queues:
                    # Non-priority commands must not be queued for injection;
                    # dispatch them directly (same pattern as priority commands).
                    if self.commands.is_dispatchable_command(raw):
                        await self._dispatch_command_inline(
                            msg, effective_key, raw,
                            self.commands.dispatch,
                        )
                        continue
                    pending_msg = msg
                    if effective_key != msg.session_key:
                        pending_msg = dataclasses.replace(
                            msg,
                            session_key_override=effective_key,
                        )
                    active_turn = await self.thread_runtime_registry.active_turn(effective_key)
                    if active_turn is not None:
                        pending_metadata = dict(pending_msg.metadata or {})
                        client_turn_id = str(
                            pending_metadata.get(WEBUI_TURN_METADATA_KEY) or ""
                        ).strip()
                        pending_metadata["_runtime_turn_id"] = active_turn.id
                        pending_metadata[WEBUI_TURN_METADATA_KEY] = active_turn.id
                        if not pending_metadata.get("injected_event"):
                            pending_metadata[ACTIVE_TURN_CORRECTION_METADATA_KEY] = True
                            if client_turn_id and client_turn_id != active_turn.id:
                                pending_metadata[
                                    ACTIVE_TURN_CLIENT_TURN_METADATA_KEY
                                ] = client_turn_id
                        pending_msg = dataclasses.replace(
                            pending_msg,
                            metadata=pending_metadata,
                        )
                    try:
                        self._pending_queues[effective_key].put_nowait(pending_msg)
                    except asyncio.QueueFull:
                        logger.warning(
                            "Pending queue full for session {}, falling back to queued task",
                            effective_key,
                        )
                    else:
                        logger.info(
                            "Routed follow-up message to pending queue for session {}",
                            effective_key,
                        )
                        continue
                # Compute the effective session key before dispatching
                # This ensures /stop command can find tasks correctly when unified session is enabled
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(effective_key, []).append(task)
                task.add_done_callback(
                    lambda t, k=effective_key: self._active_tasks.get(k, [])
                    and self._active_tasks[k].remove(t)
                    if t in self._active_tasks.get(k, [])
                    else None
                )
        finally:
            await self.shutdown()

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message: per-session serial, cross-session concurrent."""
        session_key = self._effective_session_key(msg)
        if session_key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=session_key)
        msg = self._ensure_runtime_turn_identity(msg, session_key)
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()

        pending: asyncio.Queue | None = None
        try:
            async with lock, gate:
                # Only the task that owns the session lock may publish the
                # active mid-turn injection queue for this session.
                pending = asyncio.Queue(maxsize=20)
                self._pending_queues[session_key] = pending
                try:
                    await self._start_runtime_turn(
                        msg,
                        session_key,
                        started_at=time.time(),
                    )
                    on_stream = on_stream_end = None
                    if msg.metadata.get("_wants_stream"):
                        # Split one answer into distinct stream segments.
                        stream_base_id = f"{msg.session_key}:{time.time_ns()}"
                        stream_segment = 0

                        def _current_stream_id() -> str:
                            return f"{stream_base_id}:{stream_segment}"

                        async def on_stream(delta: str) -> None:
                            meta = dict(msg.metadata or {})
                            meta["_stream_delta"] = True
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content=delta,
                                metadata=meta,
                            ))

                        async def on_stream_end(
                            *,
                            resuming: bool = False,
                            stream_kind: str = "answer",
                        ) -> None:
                            nonlocal stream_segment
                            meta = dict(msg.metadata or {})
                            meta["_stream_end"] = True
                            meta["_resuming"] = resuming
                            meta["_stream_kind"] = stream_kind
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="",
                                metadata=meta,
                            ))
                            stream_segment += 1

                    response = await self._process_message(
                        msg, on_stream=on_stream, on_stream_end=on_stream_end,
                        pending_queue=pending,
                    )
                    continuing = turn_continuation.internal_continuation_pending(msg.metadata)
                    if not continuing:
                        orphaned = await self.subagents.cancel_by_session(session_key)
                        if orphaned:
                            logger.error(
                                "Cancelled {} same-turn subagent(s) before final-answer "
                                "commit for session {}",
                                orphaned,
                                session_key,
                            )
                    completed_channel = msg.channel
                    completed_chat_id = msg.chat_id
                    if response is not None:
                        if not continuing:
                            response = await self._commit_runtime_final_answer(
                                msg,
                                session_key,
                                response,
                            )
                        await self.bus.publish_outbound(response)
                        completed_channel = response.channel
                        completed_chat_id = response.chat_id
                    elif msg.channel == "cli":
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="", metadata=msg.metadata or {},
                        ))
                    if not continuing:
                        terminal_status, finish_reason, terminal_error = (
                            self._terminal_status_for_response(response)
                        )
                        await self._finish_runtime_turn(
                            msg,
                            session_key,
                            status=terminal_status,
                            finish_reason=finish_reason,
                            error_code=(
                                "TURN_FAILED"
                                if terminal_status is TurnStatus.FAILED
                                else None
                            ),
                            error_message=terminal_error,
                            usage=(
                                response.metadata.get("usage")
                                if response is not None
                                and isinstance(response.metadata.get("usage"), dict)
                                else None
                            ),
                        )
                        completion_metadata = dict(msg.metadata or {})
                        if response is not None:
                            completion_metadata.update(response.metadata or {})
                        await self._runtime_events().turn_completed(
                            channel=completed_channel,
                            chat_id=completed_chat_id,
                            session_key=session_key,
                            metadata=completion_metadata,
                        )
                    self._cron_turns.complete(msg, response=response)
                except asyncio.CancelledError:
                    self._cron_turns.complete(
                        msg,
                        error=asyncio.CancelledError(),
                    )
                    logger.info("Task cancelled for session {}", session_key)
                    # Preserve partial context from the interrupted turn so
                    # the user does not lose tool results and assistant
                    # messages accumulated before /stop.  The checkpoint was
                    # already persisted to session metadata by
                    # _emit_checkpoint during tool execution; materializing
                    # it into session history now makes it visible in the
                    # next conversation turn.
                    try:
                        key = self._effective_session_key(msg)
                        session = self.sessions.get_or_create(key)
                        if self._restore_runtime_checkpoint(session):
                            self._clear_pending_user_turn(session)
                            self.sessions.save(session)
                            logger.info(
                                "Restored partial context for cancelled session {}",
                                key,
                            )
                    except Exception:
                        logger.debug(
                            "Could not restore checkpoint for cancelled session {}",
                            session_key,
                            exc_info=True,
                        )
                    if not turn_continuation.internal_continuation_pending(msg.metadata):
                        await self.subagents.cancel_by_session(session_key)
                        await self._finish_runtime_turn(
                            msg,
                            session_key,
                            status=TurnStatus.INTERRUPTED,
                            finish_reason=FinishReason.USER_INTERRUPTED,
                        )
                        await self._runtime_events().turn_completed(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            session_key=session_key,
                            metadata={
                                **(msg.metadata or {}),
                                "_stop_reason": "cancelled",
                            },
                        )
                    raise
                except Exception as exc:
                    logger.exception("Error processing message for session {}", session_key)
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                    ))
                    if not turn_continuation.internal_continuation_pending(msg.metadata):
                        await self.subagents.cancel_by_session(session_key)
                        await self._finish_runtime_turn(
                            msg,
                            session_key,
                            status=TurnStatus.FAILED,
                            finish_reason=FinishReason.INTERNAL_ERROR,
                            error_code="TURN_INTERNAL_ERROR",
                            error_message=str(exc),
                        )
                        await self._runtime_events().turn_completed(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            session_key=session_key,
                            metadata={
                                **(msg.metadata or {}),
                                "_stop_reason": "error",
                            },
                        )
                    self._cron_turns.complete(msg, error=exc)
                finally:
                    # Drain any messages still in the pending queue and re-publish
                    # them to the bus so they are processed as fresh inbound messages
                    # rather than silently lost.  Only remove our own queue; a
                    # later task waiting on the lock must not be able to steal
                    # cleanup ownership.
                    queue = None
                    if self._pending_queues.get(session_key) is pending:
                        queue = self._pending_queues.pop(session_key, None)
                    else:
                        queue = pending
                    if queue is not None:
                        leftover = 0
                        while True:
                            try:
                                item = queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            await self.bus.publish_inbound(item)
                            leftover += 1
                        if leftover:
                            logger.info(
                                "Re-published {} leftover message(s) to bus for session {}",
                                leftover, session_key,
                            )
                    if not turn_continuation.internal_continuation_pending(msg.metadata):
                        orphaned = await self.subagents.cancel_by_session(session_key)
                        if orphaned:
                            logger.warning(
                                "Cancelled {} orphaned subagent(s) while finalizing "
                                "session {}",
                                orphaned,
                                session_key,
                            )
                        await self._runtime_events().run_status_changed(
                            msg, session_key, "idle"
                        )
                        self._runtime_events().clear_turn(session_key)
                    await self._cron_turns.publish_next_deferred(session_key)
        finally:
            if pending is None:
                await self._runtime_events().run_status_changed(
                    msg, session_key, "idle"
                )
                self._runtime_events().clear_turn(session_key)
                await self._cron_turns.publish_next_deferred(session_key)

    async def _close_mcp_stacks(self) -> None:
        """Close live MCP transports from their owning task."""
        for name, stack in list(self._mcp_stacks.items()):
            try:
                await stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                logger.debug("MCP server '{}' cleanup error (can be ignored)", name)
        self._mcp_stacks.clear()
        self._mcp_connected = False
        # AnyIO's stdio MCP process context waits for the child, while asyncio
        # completes pipe connection_lost callbacks on subsequent loop turns.
        # Drain them now so BaseSubprocessTransport isn't finalized after the
        # gateway event loop has already closed.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def close_mcp(self) -> None:
        """Drain background work and stop the startup MCP owner task."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        owner = self._mcp_owner_task
        if owner is not None and owner is not asyncio.current_task():
            if self._mcp_shutdown_event is not None:
                self._mcp_shutdown_event.set()
            await asyncio.gather(owner, return_exceptions=True)
            if self._mcp_owner_task is owner:
                self._mcp_owner_task = None
            self._mcp_shutdown_event = None
            return
        await self._close_mcp_stacks()

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._background_tasks.remove)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def shutdown(self) -> None:
        """Stop accepting work and durably interrupt every active turn."""
        self.stop()
        for session_key in list(self._active_tasks):
            await self._cancel_active_tasks(session_key)
        background = list(self._background_tasks)
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        self._background_tasks.clear()
        from nanobot.agent.tools.exec_session import DEFAULT_EXEC_SESSION_MANAGER

        await DEFAULT_EXEC_SESSION_MANAGER.shutdown()
        await self.close_mcp()
        if self.trace_collector is not None:
            await self.trace_collector.close()

    async def _process_system_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        """Process a system inbound message (e.g. subagent announce)."""
        channel, chat_id = (
            msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
        )
        logger.info("Processing system message from {}", msg.sender_id)
        key = msg.session_key_override or f"{channel}:{chat_id}"
        session = self.sessions.get_or_create(key)
        if self._restore_runtime_checkpoint(session):
            self.sessions.save(session)
        if self._restore_pending_user_turn(session):
            self.sessions.save(session)

        session, pending = self.auto_compact.prepare_session(session, key)
        if pending:
            logger.info("Memory compact triggered for session {}", key)

        consolidator = self._consolidator_for_session(session, msg)
        if consolidator is not None:
            await consolidator.maybe_consolidate_by_tokens(
                session,
                replay_max_messages=self._max_messages,
            )
            # Consolidation can advance last_consolidated during this very
            # turn. Refresh the summary immediately so the archived prefix is
            # never absent from the provider request that follows.
            pending = self.auto_compact.summary_for_session(session) or pending
        is_subagent = msg.sender_id == "subagent"
        if is_subagent and self._persist_subagent_followup(session, msg):
            logger.debug("Subagent result persisted for session {}", key)
            self.sessions.save(session)
        self._set_tool_context(
            channel, chat_id, msg.metadata.get("message_id"),
            msg.metadata, session_key=key,
        )
        current_role = "assistant" if is_subagent else "user"
        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
            "extend_to_user": is_subagent,
            # A subagent result belongs to the still-running logical turn and
            # may resume its checkpoint. Normal/new user turns never receive
            # raw interrupted assistant/tool payloads.
            "include_ui_only": is_subagent,
        }
        history = session.get_history(**_hist_kwargs)
        workspace_scope = self.workspace_scopes.for_message(msg, session.metadata)
        msg.metadata["skill_scope"] = _project_skill_scope(self.workspace, workspace_scope.project_path, msg.metadata)

        messages = self.context.build_messages(
            history=history,
            current_message="" if is_subagent else msg.content,
            channel=channel,
            chat_id=chat_id,
            current_role=current_role,
            sender_id=msg.sender_id,
            session_summary=pending,
            session_metadata=session.metadata,
            workspace=workspace_scope.project_path,
            runtime_state=self,
            inbound_message=msg,
            skip_runtime_lines=is_subagent,
            session_key=key,
            unified_session=self._unified_session,
        )
        t_wall = time.time()
        final_content, _, all_msgs, stop_reason, _ = await self._run_agent_loop(
            messages, session=session, channel=channel, chat_id=chat_id,
            message_id=msg.metadata.get("message_id"),
            metadata=msg.metadata,
            session_key=key,
            pending_queue=pending_queue,
        )
        wall_done = time.time()
        latency_ms = max(0, int((wall_done - t_wall) * 1000))
        self._save_turn(session, all_msgs, 1 + len(history), turn_latency_ms=latency_ms)
        self._runtime_events().record_turn_latency(key, latency_ms)
        session.enforce_file_cap()
        self._clear_runtime_checkpoint(session)
        self.sessions.save(session)
        if consolidator is not None:
            self._schedule_background(
                consolidator.maybe_consolidate_by_tokens(
                    session,
                    replay_max_messages=self._max_messages,
                )
            )
        content = final_content or "Background task completed."
        outbound_metadata: dict[str, Any] = {}
        if channel == "slack" and key.startswith("slack:") and key.count(":") >= 2:
            outbound_metadata["slack"] = {"thread_ts": key.split(":", 2)[2]}
        if origin_message_id := msg.metadata.get("origin_message_id"):
            outbound_metadata["origin_message_id"] = origin_message_id
        transcript_session_key = msg.metadata.get("_webui_transcript_session_key")
        if isinstance(transcript_session_key, str) and transcript_session_key.strip():
            outbound_metadata["_webui_transcript_session_key"] = transcript_session_key.strip()
        webui_source = msg.metadata.get("_webui_message_source")
        if isinstance(webui_source, dict):
            outbound_metadata["_webui_message_source"] = dict(webui_source)
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            metadata=outbound_metadata,
        )

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
        ephemeral: bool = False,
        run_extra_hooks_for_ephemeral: bool = False,
        hooks: list[AgentHook] | None = None,
        tools: ToolRegistry | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        self._refresh_provider_snapshot()

        if msg.channel == "system":
            return await self._process_system_message(
                msg,
                session_key=session_key,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                pending_queue=pending_queue,
            )

        key = session_key or msg.session_key
        msg = self._ensure_runtime_turn_identity(msg, key)
        t0 = time.time()
        ctx = TurnContext(
            msg=msg,
            session=None,
            session_key=key,
            state=TurnState.RESTORE,
            turn_id=str(msg.metadata["_runtime_turn_id"]),
            turn_wall_started_at=t0,
            visible_run_started_at=turn_continuation.internal_continuation_run_started_at(
                msg.metadata,
            ),
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
            ephemeral=ephemeral,
            run_extra_hooks_for_ephemeral=run_extra_hooks_for_ephemeral,
            hooks=list(hooks or []),
            tools=tools,
        )

        while ctx.state is not TurnState.DONE:
            handler_name = f"_state_{ctx.state.name.lower()}"
            handler = getattr(self, handler_name, None)
            if handler is None:
                raise RuntimeError(f"Missing state handler for {ctx.state}")

            t0 = time.perf_counter()
            try:
                event = await handler(ctx)
            except Exception:
                duration = (time.perf_counter() - t0) * 1000
                ctx.trace.append(
                    StateTraceEntry(
                        state=ctx.state,
                        started_at=t0,
                        duration_ms=duration,
                        event="",
                        error="exception",
                    )
                )
                raise

            duration = (time.perf_counter() - t0) * 1000
            ctx.trace.append(
                StateTraceEntry(
                    state=ctx.state,
                    started_at=t0,
                    duration_ms=duration,
                    event=event,
                )
            )
            logger.debug(
                "[turn {}] State {} took {:.1f}ms -> event {}",
                ctx.turn_id,
                ctx.state.name,
                duration,
                event,
            )

            next_state = self._TRANSITIONS.get((ctx.state, event))
            if next_state is None:
                raise RuntimeError(
                    f"[turn {ctx.turn_id}] No transition from {ctx.state} "
                    f"on event {event!r}"
                )
            ctx.state = next_state

        logger.debug(
            "[turn {}] Turn completed after {} states",
            ctx.turn_id,
            len(ctx.trace),
        )
        return ctx.outbound

    def _assemble_outbound(
        self,
        msg: InboundMessage,
        final_content: str,
        all_msgs: list[dict[str, Any]],
        stop_reason: str,
        had_injections: bool,
        on_stream: Callable[[str], Awaitable[None]] | None,
        *,
        turn_latency_ms: int | None = None,
        turn_usage: dict[str, int] | None = None,
        artifact_paths: list[str] | None = None,
    ) -> OutboundMessage | None:
        """Assemble the final outbound message from turn results."""
        # MessageTool suppression
        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            if not had_injections or stop_reason == "empty_final_response":
                return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        if on_stream is not None and stop_reason not in {"error", "tool_error", "workflow_error"}:
            meta["_streamed"] = True
        if turn_latency_ms is not None:
            meta["latency_ms"] = int(turn_latency_ms)
        if turn_usage:
            meta["usage"] = {
                str(key): int(value)
                for key, value in turn_usage.items()
                if isinstance(value, int | float)
            }
        meta["_stop_reason"] = stop_reason

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            media=list(dict.fromkeys([
                *_generated_artifact_paths(all_msgs),
                *(artifact_paths or []),
            ])),
            metadata=meta,
        )

    async def _state_restore(self, ctx: TurnContext) -> TurnState:
        """Restore checkpoint / pending user turn; extract documents."""
        msg = ctx.msg

        if msg.media:
            new_content, image_only = self._prepare_message_media(msg.content, msg.media)
            ctx.msg = dataclasses.replace(msg, content=new_content, media=image_only)
            msg = ctx.msg

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        # Session is already fetched by the caller (_process_message) but
        # ensure it exists in case this handler is invoked independently.
        if ctx.session is None:
            ctx.session = self.sessions.get_or_create(ctx.session_key)
        self.workspace_scopes.persist_message_scope(ctx.session, msg)
        self._ensure_inherited_project_session(ctx)
        self._repair_legacy_project_consolidation(ctx.session)
        await self._runtime_events().session_turn_started(msg, ctx.session_key)

        if self._restore_runtime_checkpoint(ctx.session):
            self.sessions.save(ctx.session)
        if self._restore_pending_user_turn(ctx.session):
            self.sessions.save(ctx.session)
        self._consume_pending_interactive_prompt_answer(ctx)

        return "ok"

    def _ensure_inherited_project_session(self, ctx: TurnContext) -> None:
        """Materialize cron/child sessions that inherit an explicit project ID."""
        if isinstance(ctx.msg.metadata.get(PROJECT_CONTEXT_METADATA_KEY), dict):
            return
        project_id = ctx.msg.metadata.get("project_id")
        if not isinstance(project_id, str) or not project_id.strip():
            return
        from nanobot.storage.state import StateStore, StateStoreError

        scope = self.workspace_scopes.for_message(ctx.msg, ctx.session.metadata)
        try:
            state = StateStore(
                self.workspace / ".nanobot" / "state.sqlite",
                default_workspace=self.workspace,
            )
            project = state.get_project(project_id)
            if project is None:
                raise StateStoreError("inherited project is not registered")
            if (
                Path(project.canonical_root_path).resolve(strict=False)
                != scope.project_path.expanduser().resolve(strict=False)
            ):
                raise StateStoreError(
                    "inherited project does not match the effective workspace"
                )
            state_session = state.bind_session(
                ctx.session_key,
                project.id,
                title=str(ctx.session.metadata.get("title") or ""),
                metadata={
                    "project_id": project.id,
                    "workspace_scope": scope.metadata(),
                    "parent_project_id": ctx.msg.metadata.get("_parent_project_id"),
                },
                artifact_index_initialized=True,
            )
        except StateStoreError:
            logger.exception(
                "Inherited project session binding failed for session={}",
                ctx.session_key,
            )
            raise
        context_metadata = {
            "project_id": project.id,
            "session_id": state_session.id,
            "session_key": ctx.session_key,
        }
        ctx.msg.metadata[PROJECT_CONTEXT_METADATA_KEY] = context_metadata
        ctx.session.metadata["project_id"] = project.id
        ctx.session.metadata["session_id"] = state_session.id

    def _repair_legacy_project_consolidation(self, session: Session) -> bool:
        """Restore raw replay hidden by the removed project-memory pipeline.

        Older project turns used the global SNIP memory summarizer.  It could
        advance ``last_consolidated`` while replacing one-off research with a
        list of ``[skip]`` items.  The raw JSONL messages still exist, so reset
        only that legacy cursor.  Session-continuity summaries created by the
        new consolidator carry ``kind=session`` and are left intact.
        """
        if not isinstance(session.metadata.get("project_id"), str):
            return False
        if session.last_consolidated <= 0:
            return False
        summary = session.metadata.get("_last_summary")
        if isinstance(summary, dict) and summary.get("kind") == "session":
            return False
        logger.info(
            "Restoring {} legacy project messages for same-session replay in {}",
            session.last_consolidated,
            session.key,
        )
        session.last_consolidated = 0
        session.metadata.pop("_last_summary", None)
        self.sessions.save(session)
        return True

    def _consume_pending_interactive_prompt_answer(self, ctx: TurnContext) -> None:
        pending = normalize_interactive_prompt(
            ctx.session.metadata.get(SESSION_META_PENDING_INTERACTIVE_PROMPT)
        )
        if pending is None or pending.get("status") != "pending":
            return
        if ctx.msg.channel != "websocket":
            return
        if ctx.msg.metadata.get("webui") is not True:
            return
        if is_cron_turn(ctx.msg.metadata):
            return

        answer = normalize_interactive_prompt_answer(
            ctx.msg.metadata.get(INBOUND_META_INTERACTIVE_PROMPT_ANSWER)
        )
        text = ctx.msg.content.strip() if isinstance(ctx.msg.content, str) else ""
        if answer is None and text and pending.get("allowFreeform") is True:
            answer = {
                "promptId": pending["promptId"],
                "answerType": "freeform",
            }
        if answer is None:
            return
        if answer.get("promptId") != pending["promptId"]:
            return

        answer_type = answer.get("answerType")
        option_id = answer.get("optionId")
        group_answers = answer.get("answers")
        option_ids = {
            option.get("id")
            for option in pending.get("options", [])
            if isinstance(option, dict) and isinstance(option.get("id"), str)
        }
        if answer_type == "option":
            if not isinstance(option_id, str) or option_id not in option_ids:
                return
        elif answer_type == "freeform":
            if pending.get("allowFreeform") is not True or not text:
                return
        elif answer_type == "skip":
            if pending.get("allowSkip") is not True:
                return
        elif answer_type == "group":
            if not isinstance(group_answers, list):
                return
            pending_questions = pending.get("questions")
            if not isinstance(pending_questions, list) or not pending_questions:
                return
            question_by_id = {
                question.get("id"): question
                for question in pending_questions
                if isinstance(question, dict) and isinstance(question.get("id"), str)
            }
            if len(group_answers) != len(question_by_id):
                return
            seen_question_ids: set[str] = set()
            for item in group_answers:
                if not isinstance(item, dict):
                    return
                question_id = item.get("questionId")
                if not isinstance(question_id, str) or question_id in seen_question_ids:
                    return
                question = question_by_id.get(question_id)
                if not isinstance(question, dict):
                    return
                seen_question_ids.add(question_id)
                item_type = item.get("answerType")
                item_text = item.get("text")
                if not isinstance(item_text, str) or not item_text.strip():
                    return
                if item_type == "option":
                    item_option_id = item.get("optionId")
                    question_option_ids = {
                        option.get("id")
                        for option in question.get("options", [])
                        if isinstance(option, dict) and isinstance(option.get("id"), str)
                    }
                    if not isinstance(item_option_id, str) or item_option_id not in question_option_ids:
                        return
                elif item_type == "freeform":
                    if question.get("allowFreeform") is not True:
                        return
                else:
                    return
        else:
            return

        answered_prompt = dict(pending)
        answered_prompt["status"] = "skipped" if answer_type == "skip" else "answered"
        if isinstance(option_id, str) and option_id:
            answered_prompt["answeredOptionId"] = option_id
        if text:
            answered_prompt["answeredText"] = text
        if answer_type == "group" and isinstance(group_answers, list):
            answered_questions: list[dict[str, Any]] = []
            answers_by_question_id = {
                item.get("questionId"): item
                for item in group_answers
                if isinstance(item, dict) and isinstance(item.get("questionId"), str)
            }
            for question in answered_prompt.get("questions", []):
                if not isinstance(question, dict):
                    continue
                item = answers_by_question_id.get(question.get("id"))
                answered_question = dict(question)
                if isinstance(item, dict):
                    item_option_id = item.get("optionId")
                    item_text = item.get("text")
                    if isinstance(item_option_id, str) and item_option_id:
                        answered_question["answeredOptionId"] = item_option_id
                    if isinstance(item_text, str):
                        answered_question["answeredText"] = item_text
                answered_questions.append(answered_question)
            answered_prompt["questions"] = answered_questions
        self._update_session_prompt_message(ctx.session, answered_prompt)
        ctx.session.metadata.pop(SESSION_META_PENDING_INTERACTIVE_PROMPT, None)
        metadata = dict(ctx.msg.metadata or {})
        metadata[INBOUND_META_INTERACTIVE_PROMPT_ANSWER] = answer
        ctx.msg = dataclasses.replace(ctx.msg, metadata=metadata)
        self.sessions.save(ctx.session)

    @staticmethod
    def _update_session_prompt_message(session: Session, prompt: dict[str, Any]) -> None:
        prompt_id = prompt.get("promptId")
        if not isinstance(prompt_id, str) or not prompt_id:
            return
        for index in range(len(session.messages) - 1, -1, -1):
            message = session.messages[index]
            if message.get("role") != "assistant":
                continue
            candidate = normalize_interactive_prompt(message.get("_interactive_prompt"))
            if candidate is None or candidate.get("promptId") != prompt_id:
                continue
            updated = dict(message)
            updated["_interactive_prompt"] = dict(prompt)
            session.messages[index] = updated
            return

    def _prepare_message_media(self, content: str, media: list[str]) -> tuple[str, list[str]]:
        if self._should_extract_document_text():
            return extract_documents(content, media)
        return reference_non_image_attachments(content, media)

    def _should_extract_document_text(self) -> bool:
        if self.channels_config is None:
            return True
        return self.channels_config.extract_document_text

    async def _state_compact(self, ctx: TurnContext) -> str:
        ctx.session, pending = self.auto_compact.prepare_session(ctx.session, ctx.session_key)
        ctx.pending_summary = pending
        return "ok"

    async def _state_command(self, ctx: TurnContext) -> str:
        raw = ctx.msg.content.strip()
        cmd_ctx = CommandContext(
            msg=ctx.msg, session=ctx.session, key=ctx.session_key, raw=raw, loop=self
        )
        result = await self.commands.dispatch(cmd_ctx)
        if result is not None:
            ctx.outbound = result
            # Shortcut commands skip BUILD and SAVE, so we must persist the
            # turn here so WebUI history hydration after _turn_end sees the
            # message.  Mark messages with _command so get_history can filter
            # them out of LLM context.  /new is excluded because it
            # intentionally clears the session.
            if raw.lower() != "/new":
                ctx.user_persisted_early = self._persist_user_message_early(
                    ctx.msg, ctx.session, _command=True
                )
                ctx.session.add_message(
                    "assistant", result.content, _command=True
                )
                self.sessions.save(ctx.session)
                self._clear_pending_user_turn(ctx.session)
            return "shortcut"
        return "dispatch"

    async def _state_build(self, ctx: TurnContext) -> str:
        if not ctx.ephemeral:
            consolidator = self._consolidator_for_session(ctx.session, ctx.msg)
            if consolidator is not None:
                await consolidator.maybe_consolidate_by_tokens(
                    ctx.session,
                    replay_max_messages=self._max_messages,
                )
                # maybe_consolidate_by_tokens may have hidden old raw messages
                # after _state_compact captured its summary. Pull the newly
                # persisted summary into this same provider request.
                ctx.pending_summary = (
                    self.auto_compact.summary_for_session(ctx.session)
                    or ctx.pending_summary
                )
        self._set_tool_context(
            ctx.msg.channel,
            ctx.msg.chat_id,
            ctx.msg.metadata.get("message_id"),
            ctx.msg.metadata,
            session_key=ctx.session_key,
        )
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
            "extend_to_user": False,
            "include_ui_only": turn_continuation.internal_continuation_inbound(
                ctx.msg.metadata
            ),
        }
        ctx.history = ctx.session.get_history(**_hist_kwargs)
        self._runtime_events().record_turn_runtime(
            ctx.session_key,
            self.llm_runtime(),
        )

        ctx.initial_messages = self._build_initial_messages(
            ctx.msg,
            ctx.session,
            ctx.history,
            ctx.pending_summary,
            include_memory_recent_history=not ctx.ephemeral,
        )
        ctx.user_persisted_early = self._persist_user_message_early(
            ctx.msg, ctx.session
        )

        if ctx.on_progress is None:
            ctx.on_progress = await self._build_bus_progress_callback(ctx.msg)
        if ctx.on_retry_wait is None:
            ctx.on_retry_wait = await self._build_retry_wait_callback(ctx.msg)

        return "ok"

    async def _state_run(self, ctx: TurnContext) -> str:
        prompt_token = set_interactive_prompt_requested(False)
        try:
            if ctx.visible_run_started_at is None:
                ctx.visible_run_started_at = time.time()
            await self._runtime_events().run_status_changed(
                ctx.msg,
                ctx.session_key,
                "running",
                started_at=ctx.visible_run_started_at,
            )
            expert_team = _expert_team_binding(ctx.msg.metadata, ctx.session.metadata)
            if (
                isinstance(expert_team, dict)
                and expert_team.get("id") == ASSET_RESEARCH_TEAM_ID
                and isinstance(ctx.msg.metadata.get("expert_team_run_id"), str)
            ):
                result = await self._run_asset_research_workflow(ctx)
            else:
                result = await self._run_agent_loop(
                    ctx.initial_messages,
                    on_progress=ctx.on_progress,
                    on_stream=ctx.on_stream,
                    on_stream_end=ctx.on_stream_end,
                    on_retry_wait=ctx.on_retry_wait,
                    session=ctx.session,
                    channel=ctx.msg.channel,
                    chat_id=ctx.msg.chat_id,
                    message_id=ctx.msg.metadata.get("message_id"),
                    metadata=ctx.msg.metadata,
                    session_key=ctx.session_key,
                    pending_queue=ctx.pending_queue,
                    ephemeral=ctx.ephemeral,
                    run_extra_hooks_for_ephemeral=ctx.run_extra_hooks_for_ephemeral,
                    hooks=ctx.hooks,
                    tools=ctx.tools,
                )
        finally:
            prompt_requested = interactive_prompt_requested_in_turn()
            reset_interactive_prompt_requested(prompt_token)
        ctx.turn_usage = dict(self._last_usage)
        final_content, tools_used, all_msgs, stop_reason, had_injections = result
        ctx.final_content = final_content
        ctx.tools_used = tools_used
        ctx.all_messages = all_msgs
        ctx.stop_reason = stop_reason
        ctx.had_injections = had_injections
        if stop_reason == "interactive_prompt" or prompt_requested:
            ctx.suppress_response = True
        await turn_continuation.maybe_continue_turn(ctx)
        return "ok"

    async def _state_save(self, ctx: TurnContext) -> str:
        turn_continuation.prepare_save_boundary(ctx)

        if (
            (ctx.final_content is None or not ctx.final_content.strip())
            and not ctx.suppress_response
        ):
            ctx.final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        latency_started_at = (
            ctx.visible_run_started_at
            if turn_continuation.internal_continuation_inbound(ctx.msg.metadata)
            and ctx.visible_run_started_at is not None
            else ctx.turn_wall_started_at
        )
        ctx.turn_latency_ms = max(0, int((time.time() - latency_started_at) * 1000))
        self._save_turn(
            ctx.session, ctx.all_messages, ctx.save_skip,
            turn_latency_ms=ctx.turn_latency_ms,
            turn_usage=ctx.turn_usage,
            expert_team_id=(
                str(expert_team["id"])
                if (expert_team := _expert_team_binding(ctx.msg.metadata, ctx.session.metadata))
                and expert_team.get("id")
                else None
            ),
        )
        self._runtime_events().record_turn_latency(
            ctx.session_key,
            ctx.turn_latency_ms,
        )
        if not ctx.ephemeral:
            consolidator = self._consolidator_for_session(ctx.session, ctx.msg)
            ctx.session.enforce_file_cap()
            if consolidator is not None:
                self._schedule_background(
                    consolidator.maybe_consolidate_by_tokens(
                        ctx.session,
                        replay_max_messages=self._max_messages,
                    )
                )
        self._clear_pending_user_turn(ctx.session)
        self._clear_runtime_checkpoint(ctx.session)
        self.sessions.save(ctx.session)
        return "ok"

    async def _state_respond(self, ctx: TurnContext) -> str:
        if ctx.suppress_response:
            ctx.outbound = None
            return "ok"
        ctx.outbound = self._assemble_outbound(
            ctx.msg,
            ctx.final_content,
            ctx.all_messages[ctx.save_skip:],
            ctx.stop_reason,
            ctx.had_injections,
            ctx.on_stream,
            turn_latency_ms=ctx.turn_latency_ms,
            turn_usage=ctx.turn_usage,
            artifact_paths=ctx.artifact_paths,
        )
        return "ok"

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        should_truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip volatile multimodal payloads before writing session history."""
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            if block.get("type") == "image_url" and block.get("image_url", {}).get(
                "url", ""
            ).startswith("data:image/"):
                path = (block.get("_meta") or {}).get("path", "")
                filtered.append({"type": "text", "text": image_placeholder_text(path)})
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if should_truncate_text and len(text) > self.max_tool_result_chars:
                    text = truncate_text_fn(text, self.max_tool_result_chars)
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _save_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
        *,
        turn_latency_ms: int | None = None,
        turn_usage: dict[str, int] | None = None,
        expert_team_id: str | None = None,
    ) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime

        declared_tool_call_ids = {
            str(tc["id"])
            for m in session.messages
            if m.get("role") == "assistant"
            for tc in m.get("tool_calls") or []
            if isinstance(tc, dict) and tc.get("id")
        }
        last_assistant_idx: int | None = None
        for m in messages[skip:]:
            entry = dict(m)
            if expert_team_id:
                entry[EXPERT_TEAM_TURN_KEY] = expert_team_id
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool":
                tool_call_id = entry.get("tool_call_id")
                if not tool_call_id or str(tool_call_id) not in declared_tool_call_ids:
                    # Undeclared tool results corrupt future provider requests.
                    logger.warning(
                        "Dropping orphaned tool result {} from session {} during persistence",
                        tool_call_id or "(missing id)",
                        session.key,
                    )
                    continue
                if isinstance(content, str) and len(content) > self.max_tool_result_chars:
                    entry["content"] = truncate_text_fn(content, self.max_tool_result_chars)
                elif isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, should_truncate_text=True)
                    if not filtered:
                        # Preserve the tool_call/result pair after block filtering.
                        filtered = [
                            {"type": "text", "text": "[tool result omitted during persistence]"}
                        ]
                    entry["content"] = filtered
            elif role == "user":
                if isinstance(content, str) and ContextBuilder._RUNTIME_CONTEXT_TAG in content:
                    # Strip the runtime-context block appended at the end.
                    tag_pos = content.find(ContextBuilder._RUNTIME_CONTEXT_TAG)
                    before = content[:tag_pos].rstrip("\n ")
                    if before:
                        entry["content"] = before
                    else:
                        continue
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, drop_runtime=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
            if role == "assistant":
                last_assistant_idx = len(session.messages) - 1
                declared_tool_call_ids.update(
                    str(tc["id"])
                    for tc in entry.get("tool_calls") or []
                    if isinstance(tc, dict) and tc.get("id")
                )
        if turn_latency_ms is not None and last_assistant_idx is not None:
            session.messages[last_assistant_idx]["latency_ms"] = int(turn_latency_ms)
        if turn_usage and last_assistant_idx is not None:
            session.messages[last_assistant_idx]["usage"] = {
                str(key): int(value)
                for key, value in turn_usage.items()
                if isinstance(value, int | float)
            }
        session.updated_at = datetime.now()

    def _persist_subagent_followup(self, session: Session, msg: InboundMessage) -> bool:
        """Persist subagent follow-ups before prompt assembly so history stays durable.

        Returns True if a new entry was appended; False if the follow-up was
        deduped (same ``subagent_task_id`` already in session) or carries no
        content worth persisting.
        """
        if not msg.content:
            return False
        task_id = msg.metadata.get("subagent_task_id") if isinstance(msg.metadata, dict) else None
        if task_id and any(
            m.get("injected_event") == "subagent_result" and m.get("subagent_task_id") == task_id
            for m in session.messages
        ):
            return False
        session.add_message(
            "assistant",
            msg.content,
            sender_id=msg.sender_id,
            injected_event="subagent_result",
            subagent_task_id=task_id,
            **(
                {EXPERT_TEAM_TURN_KEY: msg.metadata.get("expert_team_run_id") or True}
                if isinstance(msg.metadata, dict) and msg.metadata.get("expert_team_run_id")
                else {}
            ),
        )
        return True

    def _set_runtime_checkpoint(self, session: Session, payload: dict[str, Any]) -> None:
        """Persist the latest in-flight turn state into session metadata."""
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = payload
        self.sessions.save(session)

    def _mark_pending_user_turn(self, session: Session) -> None:
        session.metadata[self._PENDING_USER_TURN_KEY] = True

    def _clear_pending_user_turn(self, session: Session) -> None:
        session.metadata.pop(self._PENDING_USER_TURN_KEY, None)

    def _clear_runtime_checkpoint(self, session: Session) -> None:
        if self._RUNTIME_CHECKPOINT_KEY in session.metadata:
            session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)

    @staticmethod
    def _checkpoint_message_key(message: dict[str, Any]) -> tuple[Any, ...]:
        return (
            message.get("role"),
            message.get("content"),
            message.get("tool_call_id"),
            message.get("name"),
            message.get("tool_calls"),
            message.get("reasoning_content"),
            message.get("thinking_blocks"),
        )

    def _restore_runtime_checkpoint(self, session: Session) -> bool:
        """Materialize unfinished work for UI replay, not normal model replay."""
        from datetime import datetime

        checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return False

        assistant_message = checkpoint.get("assistant_message")
        completed_tool_results = checkpoint.get("completed_tool_results") or []
        pending_tool_calls = checkpoint.get("pending_tool_calls") or []
        source_turn_id = str(checkpoint.get("_source_turn_id") or "").strip() or None

        def _ui_only(message: dict[str, Any]) -> dict[str, Any]:
            return {
                **message,
                MODEL_REPLAY_POLICY_KEY: MODEL_REPLAY_UI_ONLY,
                **(
                    {"_source_turn_id": source_turn_id}
                    if source_turn_id is not None
                    else {}
                ),
            }

        restored_messages: list[dict[str, Any]] = []
        for index in range(len(session.messages) - 1, -1, -1):
            if session.messages[index].get("role") != "user":
                continue
            session.messages[index] = _ui_only(session.messages[index])
            break
        if isinstance(assistant_message, dict):
            restored = dict(assistant_message)
            restored.setdefault("timestamp", datetime.now().isoformat())
            restored_messages.append(_ui_only(restored))
        for message in completed_tool_results:
            if isinstance(message, dict):
                restored = dict(message)
                restored.setdefault("timestamp", datetime.now().isoformat())
                restored_messages.append(_ui_only(restored))
        for tool_call in pending_tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_id = tool_call.get("id")
            name = ((tool_call.get("function") or {}).get("name")) or "tool"
            restored_messages.append(_ui_only(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": "Error: Task interrupted before this tool finished.",
                    "timestamp": datetime.now().isoformat(),
                }
            ))

        overlap = 0
        max_overlap = min(len(session.messages), len(restored_messages))
        for size in range(max_overlap, 0, -1):
            existing = session.messages[-size:]
            restored = restored_messages[:size]
            if all(
                self._checkpoint_message_key(left) == self._checkpoint_message_key(right)
                for left, right in zip(existing, restored)
            ):
                overlap = size
                break
        if overlap:
            overlap_start = len(session.messages) - overlap
            for offset in range(overlap):
                session.messages[overlap_start + offset] = {
                    **session.messages[overlap_start + offset],
                    MODEL_REPLAY_POLICY_KEY: MODEL_REPLAY_UI_ONLY,
                    **(
                        {"_source_turn_id": source_turn_id}
                        if source_turn_id is not None
                        else {}
                    ),
                }
        session.messages.extend(restored_messages[overlap:])

        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
        return True

    def _restore_pending_user_turn(self, session: Session) -> bool:
        """Close a turn that only persisted the user message before crashing."""
        from datetime import datetime

        if not session.metadata.get(self._PENDING_USER_TURN_KEY):
            return False

        if session.messages and session.messages[-1].get("role") == "user":
            session.messages[-1] = {
                **session.messages[-1],
                MODEL_REPLAY_POLICY_KEY: MODEL_REPLAY_UI_ONLY,
            }
            session.messages.append(
                {
                    "role": "assistant",
                    "content": "Error: Task interrupted before a response was generated.",
                    "timestamp": datetime.now().isoformat(),
                    MODEL_REPLAY_POLICY_KEY: MODEL_REPLAY_UI_ONLY,
                }
            )
            session.updated_at = datetime.now()

        self._clear_pending_user_turn(session)
        return True

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        sender_id: str = "user",
        media: list[str] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        ephemeral: bool = False,
        _run_extra_hooks_for_ephemeral: bool = False,
        hooks: list[AgentHook] | None = None,
        tools: ToolRegistry | None = None,
        persist_user_message: bool = True,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload."""
        await self._connect_mcp()
        metadata: dict[str, Any] = {}
        if not persist_user_message:
            metadata[turn_continuation.SKIP_USER_PERSIST_META] = True
        msg = InboundMessage(
            channel=channel, sender_id=sender_id, chat_id=chat_id,
            content=content, media=media or [], metadata=metadata,
        )
        msg = self._ensure_runtime_turn_identity(msg, session_key)
        # Share the dispatch lock so direct calls serialize with bus turns.
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        try:
            async with lock:
                if channel != "system":
                    await self.turn_lifecycle.start_turn(
                        context=RuntimeEventContext(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            session_key=session_key,
                            metadata=dict(msg.metadata or {}),
                        ),
                        turn_id=str(msg.metadata.get("_runtime_turn_id") or ""),
                    )
                kwargs: dict[str, Any] = {
                    "session_key": session_key,
                    "on_progress": on_progress,
                    "on_stream": on_stream,
                    "on_stream_end": on_stream_end,
                    "ephemeral": ephemeral,
                }
                if _run_extra_hooks_for_ephemeral:
                    kwargs["run_extra_hooks_for_ephemeral"] = True
                if hooks is not None:
                    kwargs["hooks"] = hooks
                if tools is not None:
                    kwargs["tools"] = tools
                response = await self._process_message(
                    msg,
                    **kwargs,
                )
                if channel != "system":
                    await self.subagents.cancel_by_session(session_key)
                    if response is not None:
                        response = await self._commit_runtime_final_answer(
                            msg,
                            session_key,
                            response,
                        )
                    terminal_status, finish_reason, terminal_error = (
                        self._terminal_status_for_response(response)
                    )
                    await self._finish_runtime_turn(
                        msg,
                        session_key,
                        status=terminal_status,
                        finish_reason=finish_reason,
                        error_code=(
                            "TURN_FAILED"
                            if terminal_status is TurnStatus.FAILED
                            else None
                        ),
                        error_message=terminal_error,
                        usage=(
                            response.metadata.get("usage")
                            if response is not None
                            and isinstance(response.metadata.get("usage"), dict)
                            else None
                        ),
                    )
                return response
        except asyncio.CancelledError:
            if channel != "system":
                await self._finish_runtime_turn(
                    msg,
                    session_key,
                    status=TurnStatus.INTERRUPTED,
                    finish_reason=FinishReason.USER_INTERRUPTED,
                )
            raise
        except Exception as exc:
            if channel != "system":
                await self._finish_runtime_turn(
                    msg,
                    session_key,
                    status=TurnStatus.FAILED,
                    finish_reason=FinishReason.INTERNAL_ERROR,
                    error_code="TURN_INTERNAL_ERROR",
                    error_message=str(exc),
                )
            raise
        finally:
            await self._runtime_events().run_status_changed(msg, session_key, "idle")
            self._runtime_events().clear_turn(session_key)
