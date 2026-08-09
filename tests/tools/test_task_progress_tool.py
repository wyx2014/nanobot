from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.task_progress import TaskProgressTool
from nanobot.bus.events import OUTBOUND_META_AGENT_UI


async def test_task_progress_tool_publishes_agent_ui_payload():
    sent = []

    async def send(msg):
        sent.append(msg)

    tool = TaskProgressTool(send_callback=send)
    tool.set_context(
        RequestContext(
            channel="websocket",
            chat_id="chat-1",
            message_id="m1",
            session_key="websocket:chat-1",
            metadata={"webui": True},
        )
    )

    result = await tool.execute(steps=[
        {"id": "research", "title": "研究阶段", "status": "running"},
        {"id": "draft", "title": "写作阶段", "status": "pending"},
    ])

    assert result == "Task progress updated"
    assert len(sent) == 1
    assert sent[0].channel == "websocket"
    assert sent[0].chat_id == "chat-1"
    assert sent[0].metadata[OUTBOUND_META_AGENT_UI] == {
        "kind": "task_progress",
        "plan_id": "plan:websocket:chat-1",
        "turn_id": "websocket:chat-1",
        "plan_kind": "dynamic",
        "owner": "agent",
        "policy": "required",
        "execution": "serial",
        "status": "running",
        "revision": 1,
        "active_step_ids": ["research"],
        "steps": [
            {"id": "research", "title": "研究阶段", "status": "running"},
            {"id": "draft", "title": "写作阶段", "status": "pending"},
        ],
        "current_step_id": "research",
    }


async def test_task_progress_tool_includes_public_note_and_current_step():
    sent = []

    async def send(msg):
        sent.append(msg)

    tool = TaskProgressTool(send_callback=send)
    tool.set_context(
        RequestContext(
            channel="websocket",
            chat_id="chat-1",
            message_id="m1",
            session_key="websocket:chat-1",
            metadata={"webui": True},
        )
    )

    await tool.execute(
        steps=[
            {"id": "research", "title": "研究阶段", "status": "running"},
            {"id": "draft", "title": "写作阶段", "status": "pending"},
        ],
        note="  凭证已保存，\n现在拉取行业数据  ",
        current_step_id="research",
    )

    assert sent[0].metadata[OUTBOUND_META_AGENT_UI] == {
        "kind": "task_progress",
        "plan_id": "plan:websocket:chat-1",
        "turn_id": "websocket:chat-1",
        "plan_kind": "dynamic",
        "owner": "agent",
        "policy": "required",
        "execution": "serial",
        "status": "running",
        "revision": 1,
        "active_step_ids": ["research"],
        "steps": [
            {"id": "research", "title": "研究阶段", "status": "running"},
            {"id": "draft", "title": "写作阶段", "status": "pending"},
        ],
        "note": "凭证已保存， 现在拉取行业数据",
        "current_step_id": "research",
    }


def test_task_progress_schema_is_for_dynamic_plans_only():
    steps_schema = TaskProgressTool().parameters["properties"]["steps"]

    assert steps_schema["minItems"] == 2
    assert steps_schema["maxItems"] == 4
    assert "2-4 stable steps" in TaskProgressTool().description
    assert "runtime-owned" in TaskProgressTool().description
    assert "before any business tool" in TaskProgressTool().description
    assert "exactly one running step" in TaskProgressTool().description


async def test_task_progress_tool_requires_exactly_one_running_until_terminal():
    sent = []

    async def send(msg):
        sent.append(msg)

    tool = TaskProgressTool(send_callback=send)
    tool.set_context(
        RequestContext(
            channel="websocket",
            chat_id="chat-1",
            message_id="m1",
            session_key="websocket:chat-1",
        )
    )

    result = await tool.execute(steps=[
        {"id": "research", "title": "完成行业研究", "status": "running"},
        {"id": "report", "title": "交付分析报告", "status": "running"},
    ])
    missing_running = await tool.execute(steps=[
        {"id": "research", "title": "完成行业研究", "status": "completed"},
        {"id": "report", "title": "交付分析报告", "status": "pending"},
    ])

    expected = "Error: a non-terminal task plan must have exactly one running step"
    assert result == expected
    assert missing_running == expected
    assert sent == []


async def test_task_progress_tool_accepts_zero_running_for_all_terminal_snapshot():
    sent = []

    async def send(msg):
        sent.append(msg)

    tool = TaskProgressTool(send_callback=send)
    tool.set_context(
        RequestContext(
            channel="websocket",
            chat_id="chat-1",
            message_id="m1",
            session_key="websocket:chat-1",
        )
    )

    result = await tool.execute(steps=[
        {"id": "research", "title": "完成行业研究", "status": "completed"},
        {"id": "report", "title": "交付分析报告", "status": "error"},
    ])

    assert result == "Task progress updated"
    assert "current_step_id" not in sent[0].metadata[OUTBOUND_META_AGENT_UI]


async def test_task_progress_tool_normalizes_stale_current_step_to_sole_running_step():
    sent = []

    async def send(msg):
        sent.append(msg)

    tool = TaskProgressTool(send_callback=send)
    tool.set_context(
        RequestContext(
            channel="websocket",
            chat_id="chat-1",
            message_id="m1",
            session_key="websocket:chat-1",
        )
    )

    result = await tool.execute(
        steps=[
            {"id": "research", "title": "完成行业研究", "status": "completed"},
            {"id": "report", "title": "交付分析报告", "status": "running"},
        ],
        current_step_id="research",
    )

    assert result == "Task progress updated"
    agent_ui = sent[0].metadata[OUTBOUND_META_AGENT_UI]
    assert agent_ui["current_step_id"] == "report"
    assert agent_ui["active_step_ids"] == ["report"]


async def test_task_progress_tool_clears_stale_current_step_on_terminal_snapshot():
    sent = []

    async def send(msg):
        sent.append(msg)

    tool = TaskProgressTool(send_callback=send)
    tool.set_context(
        RequestContext(
            channel="websocket",
            chat_id="chat-1",
            message_id="m1",
            session_key="websocket:chat-1",
        )
    )

    result = await tool.execute(
        steps=[
            {"id": "research", "title": "完成行业研究", "status": "completed"},
            {"id": "report", "title": "交付分析报告", "status": "completed"},
        ],
        current_step_id="report",
    )

    assert result == "Task progress updated"
    agent_ui = sent[0].metadata[OUTBOUND_META_AGENT_UI]
    assert agent_ui["status"] == "completed"
    assert agent_ui["active_step_ids"] == []
    assert "current_step_id" not in agent_ui


async def test_task_progress_tool_rejects_partial_or_ambiguous_plan():
    sent = []

    async def send(msg):
        sent.append(msg)

    tool = TaskProgressTool(send_callback=send)
    tool.set_context(
        RequestContext(
            channel="websocket",
            chat_id="chat-1",
            message_id="m1",
            session_key="websocket:chat-1",
        )
    )

    too_short = await tool.execute(steps=[
        {"id": "report", "title": "交付分析报告", "status": "running"},
    ])
    duplicate_ids = await tool.execute(steps=[
        {"id": "report", "title": "完成初稿", "status": "running"},
        {"id": "report", "title": "交付终稿", "status": "pending"},
    ])

    assert too_short == "Error: steps must contain the complete 2-4 item task plan"
    assert duplicate_ids == "Error: every task-plan step id must be unique"
    assert sent == []


async def test_task_progress_tool_does_not_replace_expert_team_workflow():
    sent = []

    async def send(msg):
        sent.append(msg)

    tool = TaskProgressTool(send_callback=send)
    tool.set_context(
        RequestContext(
            channel="websocket",
            chat_id="chat-1",
            message_id="m1",
            session_key="websocket:chat-1",
            metadata={"expert_team": {"id": "test-expert-team"}},
        )
    )
    steps = [
        {
            "id": f"phase-{index}",
            "title": f"阶段 {index}",
            "status": "running" if index == 1 else "pending",
        }
        for index in range(1, 9)
    ]

    result = await tool.execute(steps=steps, current_step_id="phase-1")

    assert result.startswith("Workflow plan is runtime-owned")
    assert sent == []


async def test_task_progress_tool_keeps_four_step_limit_without_expert_team():
    tool = TaskProgressTool(send_callback=lambda _: None)
    tool.set_context(
        RequestContext(
            channel="websocket",
            chat_id="chat-1",
            message_id="m1",
            session_key="websocket:chat-1",
        )
    )
    steps = [
        {
            "id": f"phase-{index}",
            "title": f"阶段 {index}",
            "status": "running" if index == 1 else "pending",
        }
        for index in range(1, 6)
    ]

    result = await tool.execute(steps=steps)

    assert result == "Error: steps must contain the complete 2-4 item task plan"
