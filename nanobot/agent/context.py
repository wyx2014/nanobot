"""Context builder for assembling agent prompts."""

import base64
import json
import mimetypes
import platform
import re
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

from loguru import logger

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skill_scope import allowed_workspace_skills_from_scope, explicit_skills_from_scope
from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools import mcp as mcp_tools
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.apps.cli import utils as cli_app_utils
from nanobot.bus.events import InboundMessage
from nanobot.session.goal_state import goal_state_runtime_lines
from nanobot.webui.interactive_prompt import interactive_prompt_answer_session_extra
from nanobot.webui.expert_teams import expert_team_system_prompt
from nanobot.utils.helpers import (
    current_time_str,
    detect_image_mime,
    load_bundled_template,
    truncate_text_to_tokens,
)
from nanobot.utils.prompt_templates import render_template


_EXPLICIT_INTERACTIVE_INTAKE_PATTERNS = (
    re.compile(r"\bask me (?:one|1|two|2)?\s*(?:or|-)?\s*(?:two|2)?\s*key questions\b", re.IGNORECASE),
    re.compile(r"\bask (?:one|1|two|2)?\s*(?:or|-)?\s*(?:two|2)?\s*key questions\b", re.IGNORECASE),
    re.compile(r"\bif you need more information\b", re.IGNORECASE),
    re.compile(r"\bif you need more context\b", re.IGNORECASE),
    re.compile(r"\bclarifying questions?\b", re.IGNORECASE),
    re.compile(r"如果你需要.*信息.*问我.*关键问题"),
    re.compile(r"如果你觉得.*更多背景.*告诉我"),
    re.compile(r"先问我.*关键问题"),
    re.compile(r"一两个关键问题"),
)


def _explicitly_invites_interactive_intake(current_message: str) -> bool:
    text = (current_message or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _EXPLICIT_INTERACTIVE_INTAKE_PATTERNS)


def _contains_any_marker(value: Any, markers: tuple[str, ...]) -> bool:
    if not markers:
        return False
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return any(marker in text for marker in markers)


def _filter_disallowed_skill_text(text: str, markers: tuple[str, ...]) -> str:
    if not markers:
        return text
    return "\n".join(
        line for line in text.splitlines()
        if not any(marker in line for marker in markers)
    ).strip()


def _filter_disallowed_skill_history(
    history: list[dict[str, Any]],
    markers: tuple[str, ...],
) -> list[dict[str, Any]]:
    if not markers:
        return history

    blocked_tool_call_ids: set[str] = set()
    for message in history:
        if not _contains_any_marker(message, markers):
            continue
        for tool_call in message.get("tool_calls") or []:
            call_id = tool_call.get("id") if isinstance(tool_call, dict) else None
            if isinstance(call_id, str):
                blocked_tool_call_ids.add(call_id)
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str):
            blocked_tool_call_ids.add(tool_call_id)

    filtered: list[dict[str, Any]] = []
    for message in history:
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id in blocked_tool_call_ids:
            continue
        tool_calls = message.get("tool_calls") or []
        if any(
            isinstance(tool_call, dict)
            and isinstance(tool_call.get("id"), str)
            and tool_call["id"] in blocked_tool_call_ids
            for tool_call in tool_calls
        ):
            continue
        if _contains_any_marker(message, markers):
            continue
        filtered.append(message)

    for index, message in enumerate(filtered):
        if message.get("role") == "user":
            return filtered[index:]
    return filtered


def session_extra(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return persisted kwargs for turn-attached capabilities."""
    return (
        cli_app_utils.session_extra(metadata)
        | mcp_tools.session_extra(metadata)
        | interactive_prompt_answer_session_extra(dict(metadata) if metadata else None)
    )


def runtime_lines(state: Any, msg: Any, workspace: Path, *, skip: bool = False) -> list[str]:
    """Return model-visible runtime annotations for turn-attached capabilities."""
    return [
        *cli_app_utils.runtime_lines(msg, workspace, skip=skip),
        *mcp_tools.runtime_lines(
            msg,
            configured_server_names=set(state._mcp_servers),
            connected_server_names=set(state._mcp_stacks),
            skip=skip,
        ),
    ]


async def connect_mcp(state: Any, tools: ToolRegistry) -> None:
    await mcp_tools.connect_missing_servers(state, tools)


async def handle_runtime_control(state: Any, msg: InboundMessage, tools: ToolRegistry) -> bool:
    return await mcp_tools.handle_runtime_control(state, msg, tools)


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    _MAX_RECENT_HISTORY = 50
    _MAX_HISTORY_TOKENS = 8_000  # hard cap on recent history section size (tokens)
    _RUNTIME_CONTEXT_END = "[/Runtime Context]"

    def __init__(self, workspace: Path, timezone: str | None = None, disabled_skills: list[str] | None = None):
        self.workspace = workspace
        self.timezone = timezone
        self.memory = MemoryStore(workspace)
        self._project_memories: dict[str, MemoryStore] = {}
        self._project_state: Any | None = None
        self.skills = SkillsLoader(workspace, disabled_skills=set(disabled_skills) if disabled_skills else None)

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        session_summary: str | None = None,
        workspace: Path | None = None,
        include_memory_recent_history: bool = True,
        session_key: str | None = None,
        unified_session: bool = False,
        skill_scope: Mapping[str, Any] | None = None,
        session_metadata: Mapping[str, Any] | None = None,
        project_id: str | None = None,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        root = workspace or self.workspace
        memory_store = self.memory_for_project(project_id, root)
        allowed_workspace_skills = allowed_workspace_skills_from_scope(skill_scope)
        disallowed_markers = self._disallowed_workspace_skill_markers(allowed_workspace_skills)
        parts = [self._get_identity(channel=channel, workspace=root)]

        bootstrap = self._load_bootstrap_files(root)
        if bootstrap:
            parts.append(bootstrap)

        parts.append(render_template(
            "agent/project_output_contract.md",
            workspace_path=str(root.expanduser().resolve(strict=False)),
        ))
        parts.append(render_template("agent/tool_contract.md"))

        if memory_store is not None:
            memory = memory_store.get_memory_context()
            if memory and not self._is_template_content(memory_store.read_memory(), "memory/MEMORY.md"):
                memory = _filter_disallowed_skill_text(memory, disallowed_markers)
            if memory:
                parts.append(f"# Memory\n\n{memory}")

        available_skills = {
            entry["name"]
            for entry in self.skills.list_skills(
                allowed_workspace_skills=allowed_workspace_skills,
            )
        }
        requested_skills = [*(skill_names or []), *explicit_skills_from_scope(skill_scope)]
        selected_skills = list(dict.fromkeys(name for name in requested_skills if name in available_skills))
        always_skills = self.skills.get_always_skills(allowed_workspace_skills=allowed_workspace_skills)
        active_skills = list(dict.fromkeys([*selected_skills, *always_skills]))
        if active_skills:
            always_content = self.skills.load_skills_for_context(active_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary(
            exclude=set(active_skills),
            allowed_workspace_skills=allowed_workspace_skills,
        )
        if skills_summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

        team_prompt = expert_team_system_prompt(session_metadata)
        if team_prompt:
            parts.append(team_prompt)

        if include_memory_recent_history and memory_store is not None:
            entries = memory_store.read_recent_history_for_prompt(
                since_cursor=memory_store.get_last_dream_cursor(),
                session_key=session_key,
                unified_session=unified_session,
            )
            if entries:
                capped = entries[-self._MAX_RECENT_HISTORY:]
                capped = [
                    {**entry, "content": content}
                    for entry in capped
                    if (content := _filter_disallowed_skill_text(str(entry.get("content", "")), disallowed_markers))
                ]
                if capped:
                    history_text = "\n".join(
                        f"- [{e['timestamp']}] {e['content']}" for e in capped
                    )
                    history_text = truncate_text_to_tokens(history_text, self._MAX_HISTORY_TOKENS)
                    parts.append("# Recent History\n\n" + history_text)

        if session_summary:
            parts.append(f"[Archived Context Summary]\n\n{session_summary}")

        return "\n\n---\n\n".join(parts)

    def memory_for_project(
        self,
        project_id: str | None,
        workspace: Path | None,
    ) -> MemoryStore | None:
        """Resolve a project-owned memory store without falling back across projects."""
        root = (workspace or self.workspace).expanduser().resolve(strict=False)
        runtime_root = self.workspace.expanduser().resolve(strict=False)
        if root == runtime_root:
            return self.memory
        normalized_id = (project_id or "").strip()
        if not re.fullmatch(r"prj_[a-f0-9]{32}", normalized_id):
            return None
        existing = self._project_memories.get(normalized_id)
        if existing is not None:
            return existing
        from nanobot.storage.state import StateStore, StateStoreError

        state = StateStore(
            runtime_root / ".nanobot" / "state.sqlite",
            default_workspace=runtime_root,
        )
        self._project_state = state
        project = state.get_project(normalized_id)
        if project is None:
            return None
        if Path(project.canonical_root_path).resolve(strict=False) != root:
            return None
        managed_root = (
            runtime_root
            / ".nanobot"
            / "project-memory"
            / normalized_id
        )

        def project_memory_changed(content: str) -> None:
            try:
                state.upsert_project_memory(
                    normalized_id,
                    kind="long_term",
                    content=content,
                )
            except StateStoreError:
                logger.warning(
                    "Project memory projection rejected for project_id={}",
                    normalized_id,
                )

        store = MemoryStore(
            managed_root,
            on_memory_write=project_memory_changed,
        )
        current_memory = store.read_memory()
        if current_memory and not state.list_project_memories(normalized_id):
            project_memory_changed(current_memory)
        self._project_memories[normalized_id] = store
        return store

    def _project_retrieval_context(
        self,
        project_id: str | None,
        workspace: Path,
        query: str,
    ) -> str:
        """Retrieve indexed chunks only after project ID/root verification."""
        normalized_id = (project_id or "").strip()
        if not normalized_id or not query.strip():
            return ""
        # memory_for_project performs the identity/root check and initializes
        # the shared StateStore handle.
        if self.memory_for_project(normalized_id, workspace) is None:
            return ""
        state = self._project_state
        if state is None:
            return ""
        terms = [
            term.strip(".,!?;:()[]{}\"'").lower()
            for term in query.split()
        ]
        terms = [term for term in terms if len(term) >= 2][:5]
        if not terms:
            return ""
        rows = state.search_project_chunks(
            normalized_id,
            " ".join(terms),
            limit=6,
        )
        if not rows:
            # Multi-term AND search can be too narrow; the longest term is a
            # deterministic scoped fallback, never a cross-project lookup.
            rows = state.search_project_chunks(
                normalized_id,
                max(terms, key=len),
                limit=6,
            )
        if not rows:
            return ""
        excerpts = "\n\n".join(
            f"[{row['relative_path']}#{int(row['ordinal']) + 1}]\n{row['text']}"
            for row in rows
        )
        return "# Project Documents\n\n" + truncate_text_to_tokens(excerpts, 3_000)

    def _project_memory_retrieval_context(
        self,
        project_id: str | None,
        workspace: Path,
        query: str,
    ) -> str:
        """Retrieve a few project memories after the summary routing layer."""
        normalized_id = (project_id or "").strip()
        if not normalized_id or not query.strip():
            return ""
        memory_store = self.memory_for_project(normalized_id, workspace)
        if memory_store is None:
            return ""
        state = self._project_state
        if state is None:
            return ""
        terms = re.findall(
            r"[A-Za-z0-9_./-]{2,}|[\u4e00-\u9fff]{2,8}",
            query,
        )[:5]
        if not terms:
            return ""
        rows = state.search_project_memories(
            normalized_id,
            " ".join(terms),
            limit=4,
        )
        if not rows:
            return ""
        memory_ids = [str(row["id"]) for row in rows]
        threading.Thread(
            target=state.record_project_memory_usage,
            args=(normalized_id, memory_ids),
            daemon=True,
            name="nanobot-project-memory-usage",
        ).start()
        excerpts = []
        for row in rows:
            sources = str(row.get("source_session_ids") or "")
            source_note = f" source_sessions={sources}" if sources else ""
            title = str(row.get("title") or row.get("kind") or "memory")
            excerpts.append(
                f"[memory:{row['id']}{source_note}] {title}\n{row['content']}"
            )
        source_summaries = state.project_memory_source_summaries(
            normalized_id,
            memory_ids,
            limit=2,
        )
        if source_summaries:
            excerpts.append(
                "# Supporting Rollout Summaries\n\n"
                + "\n\n".join(
                    (
                        f"[session:{row['source_session_key']} stage1:{row['id']}]\n"
                        f"{row['rollout_summary']}"
                    )
                    for row in source_summaries
                )
            )
        for path in sorted(memory_store.memory_skills_dir.glob("*.md")):
            try:
                skill_text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            lowered = skill_text.lower()
            if not any(term.lower() in lowered for term in terms):
                continue
            excerpts.append(
                f"# Relevant Memory-Derived Project Skill\n\n"
                f"[memory-skill:{path.stem}]\n{skill_text}"
            )
            break
        body = "\n\n".join(excerpts)
        return (
            "# Relevant Project Memory\n\n"
            "Historical project memory may be stale. Verify drift-prone facts against "
            "current project files or tools before relying on it. If the final answer "
            "materially relies on one of these memories, preserve its `[memory:...]` "
            "source marker so the UI can trace the source session.\n\n"
            + truncate_text_to_tokens(body, 2_000)
        )

    def _disallowed_workspace_skill_markers(self, allowed_workspace_skills: set[str] | None) -> tuple[str, ...]:
        if allowed_workspace_skills is None:
            return ()
        workspace_skills = {
            entry["name"]
            for entry in self.skills.list_skills(filter_unavailable=False)
            if entry.get("source") == "workspace"
        }
        disallowed = sorted(workspace_skills - allowed_workspace_skills)
        markers: list[str] = []
        for name in disallowed:
            markers.extend((name, f"/skills/{name}", f"skills/{name}"))
        return tuple(dict.fromkeys(markers))

    def _get_identity(self, channel: str | None = None, workspace: Path | None = None) -> str:
        """Get the core identity section."""
        root = workspace or self.workspace
        workspace_path = str(root.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
        )

    @staticmethod
    def _build_runtime_context(
        channel: str | None,
        chat_id: str | None,
        timezone: str | None = None,
        sender_id: str | None = None,
        supplemental_lines: Sequence[str] | None = None,
    ) -> str:
        """Build untrusted runtime metadata block appended after user content."""
        lines = [f"Current Time: {current_time_str(timezone)}"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if sender_id:
            lines += [f"Sender ID: {sender_id}"]
        if supplemental_lines:
            lines.extend(supplemental_lines)
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines) + "\n" + ContextBuilder._RUNTIME_CONTEXT_END

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in value]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def _load_bootstrap_files(self, workspace: Path | None = None) -> str:
        """Load all bootstrap files from workspace."""
        parts = []
        root = workspace or self.workspace

        for filename in self.BOOTSTRAP_FILES:
            file_path = root / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _is_template_content(content: str, template_path: str) -> bool:
        """Check if *content* is identical to the bundled template (user hasn't customized it)."""
        tpl = load_bundled_template(template_path)
        if tpl is not None:
            return content.strip() == tpl.strip()
        return False

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        sender_id: str | None = None,
        session_summary: str | None = None,
        session_metadata: Mapping[str, Any] | None = None,
        current_runtime_lines: Sequence[str] | None = None,
        workspace: Path | None = None,
        runtime_state: Any | None = None,
        inbound_message: Any | None = None,
        skip_runtime_lines: bool = False,
        include_memory_recent_history: bool = True,
        session_key: str | None = None,
        unified_session: bool = False,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        root = workspace or self.workspace
        project_id = None
        if isinstance(session_metadata, Mapping):
            raw_project_id = session_metadata.get("project_id")
            if isinstance(raw_project_id, str):
                project_id = raw_project_id
        if isinstance(msg_metadata := getattr(inbound_message, "metadata", None), Mapping):
            raw_context = msg_metadata.get("_project_context")
            if isinstance(raw_context, Mapping) and isinstance(raw_context.get("project_id"), str):
                project_id = str(raw_context["project_id"])
        skill_scope = None
        if isinstance(msg_metadata, Mapping):
            skill_scope = msg_metadata.get("skill_scope")
        allowed_workspace_skills = allowed_workspace_skills_from_scope(
            skill_scope if isinstance(skill_scope, Mapping) else None
        )
        disallowed_markers = self._disallowed_workspace_skill_markers(allowed_workspace_skills)
        history = _filter_disallowed_skill_history(history, disallowed_markers)
        extra = [
            *goal_state_runtime_lines(session_metadata),
        ]
        if runtime_state is not None and inbound_message is not None:
            extra.extend(runtime_lines(runtime_state, inbound_message, root, skip=skip_runtime_lines))
        if current_runtime_lines:
            extra.extend(line for line in current_runtime_lines if line)
        if channel == "websocket" and _explicitly_invites_interactive_intake(current_message):
            extra.append(
                "Interactive Intake Preference: The user explicitly invited one or two key clarification questions "
                "before work begins. If required information is missing and a short structured choice fits, use "
                "request_user_input now instead of a plain-text follow-up question or a partial answer. First evaluate "
                "all missing required information; if there are two independent key questions, put both in one "
                "request_user_input.questions card. Do not split independent initial questions into consecutive cards."
            )
        runtime_ctx = self._build_runtime_context(
            channel,
            chat_id,
            self.timezone,
            sender_id=sender_id,
            supplemental_lines=extra or None,
        )
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        # Runtime context is appended to keep the user-content prefix stable
        # for prompt-cache hits (the context changes every turn due to time).
        if isinstance(user_content, str):
            merged = f"{user_content}\n\n{runtime_ctx}"
        else:
            merged = user_content + [{"type": "text", "text": runtime_ctx}]
        messages = [
            {
                "role": "system",
                "content": self.build_system_prompt(
                    skill_names,
                    channel=channel,
                    session_summary=session_summary,
                    workspace=root,
                    include_memory_recent_history=include_memory_recent_history,
                    session_key=session_key,
                    unified_session=unified_session,
                    skill_scope=skill_scope if isinstance(skill_scope, Mapping) else None,
                    session_metadata=session_metadata,
                    project_id=project_id,
                ),
            },
            *history,
        ]
        memory_retrieval = self._project_memory_retrieval_context(
            project_id,
            root,
            current_message,
        )
        document_retrieval = self._project_retrieval_context(
            project_id,
            root,
            current_message,
        )
        retrieval = "\n\n---\n\n".join(
            item for item in (memory_retrieval, document_retrieval) if item
        )
        if retrieval:
            messages[0] = {
                **messages[0],
                "content": f"{messages[0]['content']}\n\n---\n\n{retrieval}",
            }
        if messages[-1].get("role") == current_role:
            last = dict(messages[-1])
            last["content"] = self._merge_message_content(last.get("content"), merged)
            messages[-1] = last
            return messages
        messages.append({"role": current_role, "content": merged})
        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        # Claude vision API only supports these MIME types.
        # SVG and other formats cause count_token_failed errors.
        _SUPPORTED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or mime not in _SUPPORTED_IMAGE_MIMES:
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(p)},
            })

        if not images:
            return text
        return images + [{"type": "text", "text": text}]
