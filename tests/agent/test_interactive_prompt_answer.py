from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nanobot.agent.loop import AgentLoop, TurnContext, TurnState
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.request_user_input import RequestUserInputTool
from nanobot.bus.events import InboundMessage
from nanobot.session.manager import SessionManager
from nanobot.webui.interactive_prompt import (
    INBOUND_META_INTERACTIVE_PROMPT_ANSWER,
    SESSION_META_PENDING_INTERACTIVE_PROMPT,
    normalize_interactive_prompt,
)


def _ctx(tmp_path, *, text: str, answer: dict) -> tuple[TurnContext, SessionManager]:
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("websocket:prompt")
    prompt = {
        "promptId": "prompt:1",
        "question": "Pick one",
        "options": [{"id": "a", "label": "A"}],
        "allowFreeform": False,
        "allowSkip": False,
        "status": "pending",
    }
    session.metadata[SESSION_META_PENDING_INTERACTIVE_PROMPT] = dict(prompt)
    session.add_message("assistant", "", _interactive_prompt=dict(prompt))
    sessions.save(session)
    msg = InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="prompt",
        content=text,
        metadata={"webui": True, INBOUND_META_INTERACTIVE_PROMPT_ANSWER: answer},
    )
    return (
        TurnContext(
            msg=msg,
            session_key="websocket:prompt",
            state=TurnState.RESTORE,
            turn_id="turn-1",
            session=session,
        ),
        sessions,
    )


def test_interactive_prompt_answer_rejects_invalid_option(tmp_path) -> None:
    ctx, sessions = _ctx(
        tmp_path,
        text="Invalid",
        answer={"promptId": "prompt:1", "answerType": "option", "optionId": "missing"},
    )

    AgentLoop._consume_pending_interactive_prompt_answer(SimpleNamespace(sessions=sessions), ctx)

    assert SESSION_META_PENDING_INTERACTIVE_PROMPT in ctx.session.metadata
    prompt_message = ctx.session.messages[-1]["_interactive_prompt"]
    assert prompt_message["status"] == "pending"


def test_interactive_prompt_answer_accepts_valid_option(tmp_path) -> None:
    ctx, sessions = _ctx(
        tmp_path,
        text="A",
        answer={"promptId": "prompt:1", "answerType": "option", "optionId": "a"},
    )

    AgentLoop._consume_pending_interactive_prompt_answer(SimpleNamespace(sessions=sessions), ctx)

    assert SESSION_META_PENDING_INTERACTIVE_PROMPT not in ctx.session.metadata
    prompt_message = ctx.session.messages[-1]["_interactive_prompt"]
    assert prompt_message["status"] == "answered"
    assert prompt_message["answeredOptionId"] == "a"
    assert prompt_message["answeredText"] == "A"


def test_grouped_interactive_prompt_answer_accepts_all_questions(tmp_path) -> None:
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("websocket:prompt")
    prompt = {
        "promptId": "prompt:g1",
        "question": "A couple of questions",
        "options": [],
        "questions": [
            {
                "id": "topic",
                "question": "What are you learning?",
                "options": [{"id": "language", "label": "A spoken language"}],
            },
            {
                "id": "time",
                "question": "How much time can you dedicate?",
                "options": [{"id": "30m", "label": "30-60 min"}],
            },
        ],
        "status": "pending",
    }
    session.metadata[SESSION_META_PENDING_INTERACTIVE_PROMPT] = dict(prompt)
    session.add_message("assistant", "", _interactive_prompt=dict(prompt))
    sessions.save(session)
    answer = {
        "promptId": "prompt:g1",
        "answerType": "group",
        "answers": [
            {
                "questionId": "topic",
                "answerType": "option",
                "optionId": "language",
                "text": "A spoken language",
            },
            {
                "questionId": "time",
                "answerType": "option",
                "optionId": "30m",
                "text": "30-60 min",
            },
        ],
    }
    ctx = TurnContext(
        msg=InboundMessage(
            channel="websocket",
            sender_id="user",
            chat_id="prompt",
            content="Q: What are you learning? A: A spoken language\n\nQ: How much time can you dedicate? A: 30-60 min",
            metadata={"webui": True, INBOUND_META_INTERACTIVE_PROMPT_ANSWER: answer},
        ),
        session_key="websocket:prompt",
        state=TurnState.RESTORE,
        turn_id="turn-1",
        session=session,
    )

    AgentLoop._consume_pending_interactive_prompt_answer(SimpleNamespace(sessions=sessions), ctx)

    assert SESSION_META_PENDING_INTERACTIVE_PROMPT not in ctx.session.metadata
    prompt_message = ctx.session.messages[-1]["_interactive_prompt"]
    assert prompt_message["status"] == "answered"
    assert prompt_message["questions"][0]["answeredOptionId"] == "language"
    assert prompt_message["questions"][1]["answeredOptionId"] == "30m"


def test_interactive_prompt_rejects_more_than_two_questions() -> None:
    prompt = normalize_interactive_prompt(
        {
            "promptId": "prompt:too-many",
            "question": "A few questions",
            "options": [],
            "questions": [
                {"id": "a", "question": "A?", "options": [{"id": "yes", "label": "Yes"}]},
                {"id": "b", "question": "B?", "options": [{"id": "yes", "label": "Yes"}]},
                {"id": "c", "question": "C?", "options": [{"id": "yes", "label": "Yes"}]},
            ],
            "status": "pending",
        }
    )

    assert prompt is None


def test_request_user_input_rejects_third_interactive_prompt_round(tmp_path) -> None:
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("websocket:prompt")
    for index in range(2):
        prompt = {
            "promptId": f"prompt:{index}",
            "question": "Pick one",
            "options": [{"id": "a", "label": "A"}],
            "status": "answered",
        }
        session.add_message("assistant", "", _interactive_prompt=prompt)
    sessions.save(session)

    async def send_callback(_msg) -> None:
        raise AssertionError("prompt should not be sent after two rounds")

    tool = RequestUserInputTool(send_callback=send_callback, sessions=sessions)
    tool.set_context(
        RequestContext(
            channel="websocket",
            chat_id="prompt",
            session_key="websocket:prompt",
            metadata={"webui": True},
        )
    )

    result = asyncio.run(
        tool.execute(
            question="Pick one",
            options=[{"id": "a", "label": "A"}],
        )
    )

    assert "interactive prompt limit reached" in result


def test_request_user_input_rejects_second_independent_prompt_round(tmp_path) -> None:
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("websocket:prompt")
    session.add_message(
        "assistant",
        "",
        _interactive_prompt={
            "promptId": "prompt:0",
            "question": "What is your learning goal?",
            "options": [{"id": "skill", "label": "Learn a new skill"}],
            "status": "answered",
        },
    )
    sessions.save(session)

    async def send_callback(_msg) -> None:
        raise AssertionError("independent second prompt should not be sent")

    tool = RequestUserInputTool(send_callback=send_callback, sessions=sessions)
    tool.set_context(
        RequestContext(
            channel="websocket",
            chat_id="prompt",
            session_key="websocket:prompt",
            metadata={"webui": True},
        )
    )

    result = asyncio.run(
        tool.execute(
            question="How much time can you study each day?",
            options=[{"id": "1-2h", "label": "1-2 hours"}],
        )
    )

    assert "second interactive prompt is allowed only" in result
