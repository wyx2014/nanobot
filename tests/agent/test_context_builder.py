"""Tests for ContextBuilder — system prompt and message assembly."""

from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.skills import SkillsLoader
from nanobot.bus.events import InboundMessage
from nanobot.session.goal_state import GOAL_STATE_KEY

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _builder(tmp_path: Path, **kw) -> ContextBuilder:
    return ContextBuilder(workspace=tmp_path, **kw)


def _write_skill(base: Path, name: str, description: str) -> None:
    skill_dir = base / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# _build_runtime_context (static)
# ---------------------------------------------------------------------------


class TestBuildRuntimeContext:
    def test_time_only(self):
        ctx = ContextBuilder._build_runtime_context(None, None)
        assert "[Runtime Context" in ctx
        assert "[/Runtime Context]" in ctx
        assert "Current Time:" in ctx
        assert "Channel:" not in ctx

    def test_with_channel_and_chat_id(self):
        ctx = ContextBuilder._build_runtime_context("telegram", "chat123")
        assert "Channel: telegram" in ctx
        assert "Chat ID: chat123" in ctx

    def test_with_sender_id(self):
        ctx = ContextBuilder._build_runtime_context("cli", "direct", sender_id="user1")
        assert "Sender ID: user1" in ctx

    def test_with_timezone(self):
        ctx = ContextBuilder._build_runtime_context(None, None, timezone="Asia/Shanghai")
        assert "Current Time:" in ctx

    def test_no_channel_no_chat_id_omits_both(self):
        ctx = ContextBuilder._build_runtime_context(None, None)
        assert "Channel:" not in ctx
        assert "Chat ID:" not in ctx

    def test_no_sender_id_omits(self):
        ctx = ContextBuilder._build_runtime_context("cli", "direct")
        assert "Sender ID:" not in ctx


# ---------------------------------------------------------------------------
# _merge_message_content (static)
# ---------------------------------------------------------------------------


class TestMergeMessageContent:
    def test_str_plus_str(self):
        result = ContextBuilder._merge_message_content("hello", "world")
        assert result == "hello\n\nworld"

    def test_empty_left_plus_str(self):
        result = ContextBuilder._merge_message_content("", "world")
        assert result == "world"

    def test_list_plus_list(self):
        left = [{"type": "text", "text": "a"}]
        right = [{"type": "text", "text": "b"}]
        result = ContextBuilder._merge_message_content(left, right)
        assert len(result) == 2
        assert result[0]["text"] == "a"
        assert result[1]["text"] == "b"

    def test_str_plus_list(self):
        right = [{"type": "text", "text": "b"}]
        result = ContextBuilder._merge_message_content("hello", right)
        assert len(result) == 2
        assert result[0]["text"] == "hello"
        assert result[1]["text"] == "b"

    def test_list_plus_str(self):
        left = [{"type": "text", "text": "a"}]
        result = ContextBuilder._merge_message_content(left, "world")
        assert len(result) == 2
        assert result[0]["text"] == "a"
        assert result[1]["text"] == "world"

    def test_none_plus_str(self):
        result = ContextBuilder._merge_message_content(None, "hello")
        assert result == [{"type": "text", "text": "hello"}]

    def test_str_plus_none(self):
        result = ContextBuilder._merge_message_content("hello", None)
        assert result == [{"type": "text", "text": "hello"}]

    def test_none_plus_none(self):
        result = ContextBuilder._merge_message_content(None, None)
        assert result == []

    def test_list_items_not_dicts_wrapped(self):
        result = ContextBuilder._merge_message_content(["raw_item"], None)
        assert result == [{"type": "text", "text": "raw_item"}]


# ---------------------------------------------------------------------------
# _load_bootstrap_files
# ---------------------------------------------------------------------------


class TestLoadBootstrapFiles:
    def test_no_bootstrap_files(self, tmp_path):
        builder = _builder(tmp_path)
        assert builder._load_bootstrap_files() == ""

    def test_agents_md(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("Be helpful.", encoding="utf-8")
        builder = _builder(tmp_path)
        result = builder._load_bootstrap_files()
        assert "## AGENTS.md" in result
        assert "Be helpful." in result

    def test_multiple_bootstrap_files(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("Rules.", encoding="utf-8")
        (tmp_path / "SOUL.md").write_text("Soul.", encoding="utf-8")
        builder = _builder(tmp_path)
        result = builder._load_bootstrap_files()
        assert "## AGENTS.md" in result
        assert "## SOUL.md" in result
        assert "Rules." in result
        assert "Soul." in result

    def test_all_bootstrap_files(self, tmp_path):
        for name in ContextBuilder.BOOTSTRAP_FILES:
            (tmp_path / name).write_text(f"Content of {name}", encoding="utf-8")
        builder = _builder(tmp_path)
        result = builder._load_bootstrap_files()
        for name in ContextBuilder.BOOTSTRAP_FILES:
            assert f"## {name}" in result

    def test_legacy_tools_md_is_not_bootstrapped(self, tmp_path):
        (tmp_path / "TOOLS.md").write_text("workspace tool notes", encoding="utf-8")
        builder = _builder(tmp_path)
        result = builder._load_bootstrap_files()
        assert "TOOLS.md" not in result
        assert "workspace tool notes" not in result

    def test_utf8_content(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("用中文回复", encoding="utf-8")
        builder = _builder(tmp_path)
        result = builder._load_bootstrap_files()
        assert "用中文回复" in result


# ---------------------------------------------------------------------------
# _is_template_content (static)
# ---------------------------------------------------------------------------


class TestIsTemplateContent:
    def test_nonexistent_template_returns_false(self):
        assert ContextBuilder._is_template_content("anything", "nonexistent/path.md") is False

    def test_content_matching_template(self):
        from importlib.resources import files as pkg_files
        tpl = pkg_files("nanobot") / "templates" / "memory" / "MEMORY.md"
        if not tpl.is_file():
            pytest.skip("MEMORY.md template not bundled")
        original = tpl.read_text(encoding="utf-8")
        assert ContextBuilder._is_template_content(original, "memory/MEMORY.md") is True

    def test_modified_content_returns_false(self):
        from importlib.resources import files as pkg_files
        tpl = pkg_files("nanobot") / "templates" / "memory" / "MEMORY.md"
        if not tpl.is_file():
            pytest.skip("MEMORY.md template not bundled")
        assert ContextBuilder._is_template_content("totally different", "memory/MEMORY.md") is False


# ---------------------------------------------------------------------------
# Bundled bootstrap templates
# ---------------------------------------------------------------------------


class TestBundledToolContract:
    def test_tool_contract_balances_general_and_coding_workflows(self):
        from importlib.resources import files as pkg_files

        tpl = pkg_files("nanobot") / "templates" / "agent" / "tool_contract.md"
        content = tpl.read_text(encoding="utf-8")

        assert "## General Tool Contract" in content
        assert "Use the narrowest structured tool" in content
        assert "Do not use `exec` as a universal workaround" in content
        assert "## File and Coding Workflows" in content
        assert "apply_patch" in content
        assert "## Web and External Information" in content
        assert "## Messaging and Media" in content
        assert "## Scheduling and Background Work" in content
        assert "pure coding" not in content.lower()

    def test_tool_contract_is_injected_without_workspace_file(self, tmp_path):
        builder = _builder(tmp_path)
        prompt = builder.build_system_prompt()

        assert "# Tool Usage Notes" in prompt
        assert "## General Tool Contract" in prompt
        assert "Do not use `exec` as a universal workaround" in prompt

    def test_websocket_identity_requests_public_pre_tool_narration(self, tmp_path):
        builder = _builder(tmp_path)
        prompt = builder.build_system_prompt(channel="websocket")

        assert "make `update_task_progress` the first tool call in the first batch" in prompt
        assert "user goals or deliverables" in prompt
        assert "preserving the exact step ids, titles, and order" in prompt
        assert "Every non-terminal snapshot must have exactly one `running` step" in prompt
        assert "every step is terminal" in prompt
        assert "one short public action sentence" in prompt
        assert "never private reasoning or hidden chain-of-thought" in prompt
        assert "Keep the final answer separate" in prompt

    def test_current_project_output_boundary_overrides_bootstrap_archive_path(self, tmp_path):
        external = tmp_path.parent / "other-project" / "reports"
        (tmp_path / "USER.md").write_text(
            f"# Habits\n\n- Archive path: `{external}`\n",
            encoding="utf-8",
        )
        builder = _builder(tmp_path)

        prompt = builder.build_system_prompt(channel="websocket")

        boundary = "## Current Project Output Boundary"
        assert boundary in prompt
        assert f"The authoritative project root for this turn is: `{tmp_path.resolve()}`" in prompt
        assert "A default workspace is still a real project boundary" in prompt
        assert "They never override the current project root" in prompt
        assert prompt.index(str(external)) < prompt.index(boundary)


# ---------------------------------------------------------------------------
# _build_user_content
# ---------------------------------------------------------------------------


class TestBuildUserContent:
    def test_no_media_returns_string(self, tmp_path):
        builder = _builder(tmp_path)
        result = builder._build_user_content("hello", None)
        assert result == "hello"

    def test_empty_media_returns_string(self, tmp_path):
        builder = _builder(tmp_path)
        result = builder._build_user_content("hello", [])
        assert result == "hello"

    def test_nonexistent_media_file_returns_string(self, tmp_path):
        builder = _builder(tmp_path)
        result = builder._build_user_content("hello", ["/nonexistent/image.png"])
        assert result == "hello"

    def test_non_image_file_returns_string(self, tmp_path):
        txt = tmp_path / "doc.txt"
        txt.write_text("not an image", encoding="utf-8")
        builder = _builder(tmp_path)
        result = builder._build_user_content("hello", [str(txt)])
        assert result == "hello"

    def test_valid_image_returns_list(self, tmp_path):
        png = tmp_path / "test.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        builder = _builder(tmp_path)
        result = builder._build_user_content("hello", [str(png)])
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["type"] == "image_url"
        assert result[0]["image_url"]["url"].startswith("data:image/png;base64,")
        assert result[1]["type"] == "text"
        assert result[1]["text"] == "hello"

    def test_image_meta_includes_path(self, tmp_path):
        png = tmp_path / "test.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        builder = _builder(tmp_path)
        result = builder._build_user_content("hello", [str(png)])
        assert "_meta" in result[0]
        assert "path" in result[0]["_meta"]


# ---------------------------------------------------------------------------
# build_system_prompt
# ---------------------------------------------------------------------------


class TestBuildSystemPrompt:
    def test_returns_nonempty_string(self, tmp_path):
        builder = _builder(tmp_path)
        result = builder.build_system_prompt()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_includes_identity_section(self, tmp_path):
        builder = _builder(tmp_path)
        result = builder.build_system_prompt()
        assert "workspace" in result.lower() or "python" in result.lower()

    def test_includes_bootstrap_files(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("Be helpful and concise.", encoding="utf-8")
        builder = _builder(tmp_path)
        result = builder.build_system_prompt()
        assert "Be helpful and concise." in result

    def test_includes_session_summary(self, tmp_path):
        builder = _builder(tmp_path)
        result = builder.build_system_prompt(session_summary="Previous chat about Python.")
        assert "Previous chat about Python." in result
        assert "[Archived Context Summary]" in result

    def test_sections_separated_by_separator(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("Rules.", encoding="utf-8")
        builder = _builder(tmp_path)
        result = builder.build_system_prompt(session_summary="Summary.")
        assert "\n\n---\n\n" in result

    def test_no_bootstrap_no_summary(self, tmp_path):
        builder = _builder(tmp_path)
        result = builder.build_system_prompt()
        assert "## AGENTS.md" not in result
        assert "[Archived Context Summary]" not in result

    def test_runtime_memory_is_not_injected_into_another_project(self, tmp_path):
        builder = _builder(tmp_path)
        builder.memory.write_memory("Inbox-only customer secret")
        builder.memory.append_history(
            "Prior Inbox-only discussion",
            session_key="websocket:inbox",
        )
        project = tmp_path / "customer-a"
        project.mkdir()

        result = builder.build_system_prompt(
            workspace=project,
            session_key="websocket:project-a",
        )

        assert "Inbox-only customer secret" not in result
        assert "Prior Inbox-only discussion" not in result

    def test_project_uses_global_profile_and_only_project_agents(self, tmp_path):
        builder = _builder(tmp_path)
        (tmp_path / "SOUL.md").write_text("Global calm style", encoding="utf-8")
        (tmp_path / "USER.md").write_text(
            "The user prefers concise replies everywhere.",
            encoding="utf-8",
        )
        project = tmp_path / "customer-a"
        project.mkdir()
        (project / "AGENTS.md").write_text("Project release rules", encoding="utf-8")
        (project / "SOUL.md").write_text("Project-only persona", encoding="utf-8")
        (project / "USER.md").write_text("Project-only user profile", encoding="utf-8")

        result = builder.build_system_prompt(
            workspace=project,
            project_id="prj_customer_a",
        )

        assert "Project release rules" in result
        assert "Global calm style" in result
        assert "prefers concise replies everywhere" in result
        assert "Project-only persona" not in result
        assert "Project-only user profile" not in result

# ---------------------------------------------------------------------------
# build_messages
# ---------------------------------------------------------------------------


class TestBuildMessages:
    def test_basic_empty_history(self, tmp_path):
        builder = _builder(tmp_path)
        messages = builder.build_messages([], "hello")
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "hello" in str(messages[1]["content"])

    def test_runtime_context_injected(self, tmp_path):
        builder = _builder(tmp_path)
        messages = builder.build_messages([], "hello", channel="cli", chat_id="direct")
        user_msg = str(messages[-1]["content"])
        assert "[Runtime Context" in user_msg
        assert "hello" in user_msg

    def test_project_memory_is_not_loaded_but_project_documents_still_are(self, tmp_path):
        from nanobot.storage.state import StateStore

        builder = _builder(tmp_path)
        project = tmp_path / "customer-a"
        other = tmp_path / "customer-b"
        project.mkdir()
        other.mkdir()
        state = StateStore(
            tmp_path / ".nanobot" / "state.sqlite",
            default_workspace=tmp_path,
        )
        project_id = state.ensure_project(project).id
        other_id = state.ensure_project(other).id
        state.upsert_project_memory(
            project_id,
            kind="workflow",
            title="Release verification",
            content="Run the release smoke test before packaging.",
            memory_key="release",
        )
        state.upsert_project_memory(
            other_id,
            kind="workflow",
            title="Release verification",
            content="Customer B private release process.",
            memory_key="release",
        )
        state.replace_project_document(
            project_id,
            relative_path="docs/release.txt",
            chunks=["release verification uses the current project document"],
        )

        messages = builder.build_messages(
            [],
            "Please run the release verification",
            workspace=project,
            session_metadata={"project_id": project_id},
        )
        system = str(messages[0]["content"])

        assert "# Relevant Project Memory" not in system
        assert "[memory:mem_" not in system
        assert "Run the release smoke test before packaging." not in system
        assert "Customer B private release process." not in system
        assert "# Project Documents" in system
        assert "release verification uses the current project document" in system

    def test_skill_scope_filters_workspace_skills_in_system_prompt(self, tmp_path):
        ws_skills = tmp_path / "skills"
        ws_skills.mkdir()
        _write_skill(ws_skills, "project-skill", "Project scoped skill")
        _write_skill(ws_skills, "other-skill", "Other user skill")
        builtin = tmp_path / "builtin"
        _write_skill(builtin, "pdf", "Builtin pdf skill")
        builder = _builder(tmp_path)
        builder.skills = SkillsLoader(tmp_path, builtin_skills_dir=builtin)
        msg = InboundMessage(
            channel="websocket",
            sender_id="u",
            chat_id="c",
            content="hello",
            metadata={
                "skill_scope": {
                    "project_bound_user_skills": ["project-skill"],
                    "explicit_skills": [],
                },
            },
        )

        system = builder.build_messages([], "hello", inbound_message=msg)[0]["content"]
        assert "project-skill" in system
        assert "pdf" in system
        assert "other-skill" not in system

    def test_explicit_skill_does_not_expand_project_skill_scope(self, tmp_path):
        ws_skills = tmp_path / "skills"
        ws_skills.mkdir()
        _write_skill(ws_skills, "project-skill", "Project scoped skill")
        _write_skill(ws_skills, "explicit-only", "Explicit but ungranted skill")
        builder = _builder(tmp_path)
        msg = InboundMessage(
            channel="websocket",
            sender_id="u",
            chat_id="c",
            content="hello",
            metadata={
                "skill_scope": {
                    "project_bound_user_skills": ["project-skill"],
                    "explicit_skills": ["explicit-only"],
                },
            },
        )

        system = builder.build_messages([], "hello", inbound_message=msg)[0]["content"]
        assert "project-skill" in system
        assert "explicit-only" not in system

    def test_explicit_project_skill_is_loaded_without_a_read_file_turn(self, tmp_path):
        ws_skills = tmp_path / "skills"
        ws_skills.mkdir()
        _write_skill(ws_skills, "selected", "Selected skill")
        _write_skill(ws_skills, "other", "Other skill")
        builder = _builder(tmp_path)
        msg = InboundMessage(
            channel="websocket",
            sender_id="u",
            chat_id="c",
            content="hello",
            metadata={
                "skill_scope": {
                    "project_bound_user_skills": ["selected", "other"],
                    "explicit_skills": ["selected"],
                },
            },
        )

        system = builder.build_messages([], "hello", inbound_message=msg)[0]["content"]
        assert "### Skill: selected" in system
        assert "- **selected**" not in system
        assert "- **other**" in system

    def test_skill_scope_filters_disallowed_skill_from_replayed_tool_history(self, tmp_path):
        ws_skills = tmp_path / "skills"
        ws_skills.mkdir()
        _write_skill(ws_skills, "allowed-skill", "Allowed skill")
        _write_skill(ws_skills, "ifind-finance-data", "Finance skill")
        builder = _builder(tmp_path)
        msg = InboundMessage(
            channel="websocket",
            sender_id="u",
            chat_id="c",
            content="current question",
            metadata={"skill_scope": {"project_bound_user_skills": ["allowed-skill"]}},
        )
        history = [
            {"role": "user", "content": "/ifind-finance-data\nold stock question"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "exec",
                        "arguments": "{\"command\":\"cd /workspace/skills/ifind-finance-data && node call-node.js\"}",
                    },
                }],
            },
            {"role": "tool", "tool_call_id": "call-1", "name": "exec", "content": "ok"},
            {"role": "assistant", "content": "used ifind-finance-data successfully"},
            {"role": "user", "content": "safe follow-up"},
        ]

        messages = builder.build_messages(history, "current question", inbound_message=msg)
        replay = "\n".join(str(message) for message in messages[1:])

        assert "ifind-finance-data" not in replay
        assert "call-1" not in replay
        assert "safe follow-up" in replay
        assert "current question" in replay

    def test_legacy_memory_is_not_injected_and_recent_history_is_skill_filtered(self, tmp_path):
        ws_skills = tmp_path / "skills"
        ws_skills.mkdir()
        _write_skill(ws_skills, "allowed-skill", "Allowed skill")
        _write_skill(ws_skills, "ifind-finance-data", "Finance skill")
        builder = _builder(tmp_path)
        builder.memory.write_memory(
            "- allowed-skill is useful\n"
            "- ifind-finance-data calls /workspace/skills/ifind-finance-data\n",
        )
        builder.memory.append_history(
            "safe recent fact\nifind-finance-data used call-node.js",
            session_key="websocket:c",
        )
        msg = InboundMessage(
            channel="websocket",
            sender_id="u",
            chat_id="c",
            content="hello",
            metadata={"skill_scope": {"project_bound_user_skills": ["allowed-skill"]}},
        )

        system = builder.build_messages(
            [],
            "hello",
            inbound_message=msg,
            session_key="websocket:c",
        )[0]["content"]

        assert "allowed-skill is useful" not in system
        assert "safe recent fact" in system
        assert "ifind-finance-data" not in system

    def test_session_metadata_injects_active_goal_state(self, tmp_path):
        builder = _builder(tmp_path)
        meta = {
            GOAL_STATE_KEY: {"status": "active", "objective": "Finish docs migration."},
        }
        messages = builder.build_messages(
            [],
            "hi",
            channel="cli",
            chat_id="x",
            session_metadata=meta,
        )
        user_msg = str(messages[-1]["content"])
        assert "Goal (active):" in user_msg
        assert "Finish docs migration." in user_msg

    def test_goal_state_does_not_leak_without_session_metadata(self, tmp_path):
        builder = _builder(tmp_path)
        other_session_meta = {
            GOAL_STATE_KEY: {"status": "active", "objective": "Other chat goal."},
        }

        with_goal = builder.build_messages(
            [],
            "hi",
            channel="websocket",
            chat_id="chat-a",
            session_metadata=other_session_meta,
        )
        without_goal = builder.build_messages(
            [],
            "hi",
            channel="websocket",
            chat_id="chat-b",
            session_metadata={},
        )

        assert "Other chat goal." in str(with_goal[-1]["content"])
        assert "Other chat goal." not in str(without_goal[-1]["content"])
        assert "Goal (active):" not in str(without_goal[-1]["content"])

    def test_current_runtime_lines_are_injected(self, tmp_path):
        builder = _builder(tmp_path)
        messages = builder.build_messages(
            [],
            "please use @zoom tonight",
            current_runtime_lines=[
                "CLI App Attachment: @zoom (installed; tool=run_cli_app; entry_point=cli-anything-zoom).",
            ],
        )
        user_msg = str(messages[-1]["content"])

        assert "CLI App Attachment: @zoom" in user_msg
        assert "tool=run_cli_app" in user_msg
        assert "entry_point=cli-anything-zoom" in user_msg

    def test_consecutive_same_role_merged(self, tmp_path):
        builder = _builder(tmp_path)
        history = [{"role": "user", "content": "previous user message"}]
        messages = builder.build_messages(history, "new message")
        assert len(messages) == 2  # system + merged user
        assert "previous user message" in str(messages[1]["content"])
        assert "new message" in str(messages[1]["content"])

    def test_different_role_appended(self, tmp_path):
        builder = _builder(tmp_path)
        history = [{"role": "assistant", "content": "previous response"}]
        messages = builder.build_messages(history, "new message")
        assert len(messages) == 3  # system + assistant + user
        assert "Same-session Conversation Continuity" in messages[0]["content"]
        assert "preceded by 1 replayed message" in messages[0]["content"]
        assert "Never claim that the current request is the first message" in messages[0]["content"]

    def test_empty_history_does_not_add_continuity_contract(self, tmp_path):
        builder = _builder(tmp_path)
        messages = builder.build_messages([], "first message")

        assert "Same-session Conversation Continuity" not in messages[0]["content"]

    def test_project_session_does_not_inject_global_memory(self, tmp_path):
        builder = _builder(tmp_path)
        builder.memory.write_memory("unrelated global stock memory")

        prompt = builder.build_system_prompt(
            workspace=tmp_path,
            project_id="prj_current",
        )

        assert "unrelated global stock memory" not in prompt

    def test_media_with_history(self, tmp_path):
        png = tmp_path / "img.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        builder = _builder(tmp_path)
        history = [{"role": "assistant", "content": "see this"}]
        messages = builder.build_messages(history, "check image", media=[str(png)])
        user_msg = messages[-1]["content"]
        assert isinstance(user_msg, list)
        assert any(b.get("type") == "image_url" for b in user_msg)
