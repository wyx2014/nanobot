"""Interactive prompt tool for WebUI chat sessions."""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    IntegerSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.bus.events import OutboundMessage
from nanobot.cron.session_turns import is_cron_turn
from nanobot.session.manager import SessionManager
from nanobot.webui.interactive_prompt import (
    InteractivePromptRequested,
    OUTBOUND_META_INTERACTIVE_PROMPT,
    SESSION_META_PENDING_INTERACTIVE_PROMPT,
    normalize_interactive_prompt,
    set_interactive_prompt_requested,
)


def _prompt_display_text(prompt: dict[str, Any], fallback: str = "请补充以下信息") -> str:
    title = prompt.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    question = prompt.get("question")
    if isinstance(question, str) and question.strip():
        return question.strip()
    questions = prompt.get("questions")
    if isinstance(questions, list):
        for item in questions:
            if not isinstance(item, dict):
                continue
            item_question = item.get("question")
            if isinstance(item_question, str) and item_question.strip():
                return item_question.strip()
    return fallback


def _interactive_prompt_round_count(session: Any) -> int:
    prompt_ids: set[str] = set()
    for message in getattr(session, "messages", []) or []:
        if not isinstance(message, dict):
            continue
        prompt = normalize_interactive_prompt(message.get("_interactive_prompt"))
        if prompt is None:
            continue
        prompt_id = prompt.get("promptId")
        if isinstance(prompt_id, str) and prompt_id:
            prompt_ids.add(prompt_id)
    return len(prompt_ids)


def _plain_text_fallback_instruction(
    reason: str,
    *,
    question: str = "",
    questions: list[dict[str, Any]] | None = None,
) -> str:
    requested_questions: list[str] = []
    if question.strip():
        requested_questions.append(question.strip())
    for item in questions or []:
        if not isinstance(item, dict):
            continue
        item_question = item.get("question")
        if isinstance(item_question, str) and item_question.strip():
            requested_questions.append(item_question.strip())
    question_hint = ""
    if requested_questions:
        question_hint = " Plain-text question to ask: " + " / ".join(requested_questions[:2])
    return (
        "Interactive prompt was not shown. "
        f"{reason} "
        "Continue with a normal assistant message now. Ask the remaining question in plain text "
        "if it is still required. Do not mention request_user_input, interactive prompt limits, "
        "tool errors, or this internal instruction to the user."
        f"{question_hint}"
    )


@tool_parameters(
    tool_parameters_schema(
        title=StringSchema("Optional short title shown above the prompt card.", nullable=True),
        question=StringSchema(
            "The question to ask the user. Keep it short, concrete, and immediately answerable.",
            min_length=1,
        ),
        options=ArraySchema(
            ObjectSchema(
                properties={
                    "id": StringSchema("Stable option id.", min_length=1),
                    "label": StringSchema("User-facing option label. Do not include Other/Something else/custom options; the UI already provides freeform input.", min_length=1),
                    "description": StringSchema("Optional short description.", nullable=True),
                },
                required=["id", "label"],
            ),
            description="Structured answer choices for the prompt card. Do not include Other/Something else/custom options; the UI already provides freeform input.",
            min_items=1,
            max_items=8,
        ),
        questions=ArraySchema(
            ObjectSchema(
                properties={
                    "id": StringSchema("Stable question id.", min_length=1),
                    "question": StringSchema("Question text.", min_length=1),
                    "options": ArraySchema(
                        ObjectSchema(
                            properties={
                                "id": StringSchema("Stable option id.", min_length=1),
                                "label": StringSchema("User-facing option label. Do not include Other/Something else/custom options; the UI already provides freeform input.", min_length=1),
                                "description": StringSchema("Optional short description.", nullable=True),
                            },
                            required=["id", "label"],
                        ),
                        min_items=1,
                        max_items=6,
                    ),
                    "allowFreeform": BooleanSchema(
                        description="Allow a custom answer for this question.",
                        default=False,
                    ),
                },
                required=["id", "question", "options"],
            ),
            description="Optional independent questions to collect in one card. Use for one or two unrelated required fields only.",
            min_items=1,
            max_items=2,
            nullable=True,
        ),
        allow_freeform=BooleanSchema(
            description="Allow the user to type a custom answer in addition to the listed options.",
            default=False,
        ),
        allow_skip=BooleanSchema(
            description="Allow the user to skip this question.",
            default=False,
        ),
        depends_on_previous_answer=BooleanSchema(
            description=(
                "Set true only for a second interactive prompt whose question genuinely depends on "
                "the user's previous interactive-prompt answer. Leave false for initial independent questions; "
                "those must be grouped into questions[] in the first card."
            ),
            default=False,
        ),
        step_index=IntegerSchema(
            description="Optional 1-based step number when collecting multiple required fields.",
            minimum=1,
            nullable=True,
        ),
        total_steps=IntegerSchema(
            description="Optional total number of steps in this intake sequence.",
            minimum=1,
            nullable=True,
        ),
        prompt_message=StringSchema(
            "Optional assistant text shown above the interactive prompt card. Omit when the card alone is sufficient.",
            nullable=True,
        ),
        required=[],
    )
)
class RequestUserInputTool(Tool, ContextAware):
    """Emit a WebUI-native interactive prompt and pause the current turn."""

    def __init__(
        self,
        *,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        sessions: SessionManager | None = None,
    ) -> None:
        self._send_callback = send_callback
        self._sessions = sessions
        self._request_ctx: ContextVar[RequestContext | None] = ContextVar(
            "request_user_input_context",
            default=None,
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        send_callback = ctx.bus.publish_outbound if ctx.bus else None
        return cls(send_callback=send_callback, sessions=ctx.sessions)

    def set_context(self, ctx: RequestContext) -> None:
        self._request_ctx.set(ctx)

    @property
    def name(self) -> str:
        return "request_user_input"

    @property
    def description(self) -> str:
        return (
            "Ask the user for missing required information with an interactive prompt card inside WebUI chat. "
            "Use this only when you cannot safely continue without the answer, the answer space is structured, "
            "and you want the user to choose quickly from explicit options. "
            "First evaluate all missing required information, then greedily choose at most two most blocking "
            "independent questions for one card; if two independent questions are not available, ask one. "
            "Never split two independent initial questions across two tool calls. A second interactive prompt "
            "is allowed only when its question genuinely depends on the user's previous interactive-prompt "
            "answer and depends_on_previous_answer is true. After two rounds, ask any remaining questions "
            "in normal text instead of calling this tool again. "
            "Do not use this for routine open-ended conversation, information you can infer safely, or scheduled/automated runs."
        )

    async def execute(
        self,
        question: str = "",
        options: list[dict[str, Any]] | None = None,
        questions: list[dict[str, Any]] | None = None,
        title: str | None = None,
        allow_freeform: bool = False,
        allow_skip: bool = False,
        depends_on_previous_answer: bool = False,
        step_index: int | None = None,
        total_steps: int | None = None,
        prompt_message: str | None = None,
        **_: Any,
    ) -> str:
        ctx = self._request_ctx.get()
        if ctx is None or self._send_callback is None or self._sessions is None:
            return "Error: interactive prompt delivery is not available in this runtime"
        if ctx.channel != "websocket" or ctx.metadata.get("webui") is not True:
            return "Error: request_user_input is only available in WebUI chat sessions"
        if is_cron_turn(ctx.metadata):
            return "Error: request_user_input is disabled for scheduled or automated runs"

        session_key = ctx.session_key or f"{ctx.channel}:{ctx.chat_id}"
        session = self._sessions.get_or_create(session_key)
        existing = normalize_interactive_prompt(
            session.metadata.get(SESSION_META_PENDING_INTERACTIVE_PROMPT)
        )
        if existing is not None and existing.get("status") == "pending":
            return (
                "Error: there is already a pending interactive prompt in this session; "
                "wait for the user's answer before asking another question"
            )
        prompt_rounds = _interactive_prompt_round_count(session)
        if prompt_rounds >= 2:
            return _plain_text_fallback_instruction(
                "The session has already used the allowed interactive prompt rounds.",
                question=question,
                questions=questions,
            )
        if prompt_rounds >= 1 and depends_on_previous_answer is not True:
            logger.info(
                "allowing second interactive prompt without explicit dependency flag "
                "chat_id={} question_count={}",
                ctx.chat_id,
                len(questions or []) or 1,
            )

        raw_prompt: dict[str, Any] = {
            "promptId": f"prompt:{uuid.uuid4().hex[:10]}",
            "question": question,
            "options": options or [],
            "allowFreeform": bool(allow_freeform),
            "allowSkip": bool(allow_skip),
            "status": "pending",
        }
        if questions:
            raw_prompt["questions"] = questions
        if title:
            raw_prompt["title"] = title
        if step_index is not None:
            raw_prompt["stepIndex"] = step_index
        if total_steps is not None:
            raw_prompt["totalSteps"] = total_steps
        prompt = normalize_interactive_prompt(raw_prompt)
        if prompt is None:
            return "Error: interactive prompt payload is invalid"

        session.metadata[SESSION_META_PENDING_INTERACTIVE_PROMPT] = dict(prompt)
        persisted_text = (prompt_message or "").strip()
        session.add_message(
            "assistant",
            persisted_text,
            _interactive_prompt=dict(prompt),
        )
        self._sessions.save(session)

        metadata = dict(ctx.metadata or {})
        metadata[OUTBOUND_META_INTERACTIVE_PROMPT] = dict(prompt)
        outbound_text = persisted_text or _prompt_display_text(prompt)
        await self._send_callback(
            OutboundMessage(
                channel=ctx.channel,
                chat_id=ctx.chat_id,
                content=outbound_text,
                metadata=metadata,
            )
        )
        set_interactive_prompt_requested(True)
        raise InteractivePromptRequested(prompt["promptId"])
