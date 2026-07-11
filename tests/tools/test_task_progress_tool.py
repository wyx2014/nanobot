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
        "steps": [
            {"id": "research", "title": "研究阶段", "status": "running"},
            {"id": "draft", "title": "写作阶段", "status": "pending"},
        ],
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
        "steps": [
            {"id": "research", "title": "研究阶段", "status": "running"},
            {"id": "draft", "title": "写作阶段", "status": "pending"},
        ],
        "note": "凭证已保存， 现在拉取行业数据",
        "current_step_id": "research",
    }
