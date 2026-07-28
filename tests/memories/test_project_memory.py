"""Tests for the project-scoped two-stage memory pipeline."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory import MemoryStore
from nanobot.config.schema import Config
from nanobot.memories.project import (
    ProjectMemoryPipeline,
    ProjectMemoryPipelineConfig,
)
from nanobot.storage.state import StateStore


def test_agent_loop_reads_project_memory_config_from_agent_defaults(
    tmp_path: Path,
) -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "workspace": str(tmp_path),
                    "dream": {"project_memory_idle_s": 7},
                }
            }
        }
    )
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation.max_tokens = 4_096

    loop = AgentLoop.from_config(
        config,
        provider=provider,
        model="test-model",
    )

    assert loop.project_memory.config.idle_seconds == 7


@pytest.mark.asyncio
async def test_pipeline_extracts_consolidates_and_deduplicates_per_project(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "customer-a"
    other_root = tmp_path / "customer-b"
    project_root.mkdir()
    other_root.mkdir()
    state = StateStore(tmp_path / ".nanobot" / "state.sqlite", default_workspace=tmp_path)
    project = state.ensure_project(project_root, kind="workspace")
    other = state.ensure_project(other_root, kind="workspace")
    session = state.bind_session("websocket:customer-a", project.id)
    state.upsert_project_memory(
        project.id,
        kind="long_term",
        content="Legacy projection that Phase 2 must replace.",
    )

    async def respond(**kwargs):
        prompt = str(kwargs["messages"][-1]["content"])
        if "<rollout>" in prompt:
            return SimpleNamespace(
                finish_reason="stop",
                content=json.dumps(
                    {
                        "raw_memory": "Use pnpm. password=hunter2",
                        "rollout_summary": "The project uses pnpm.",
                        "rollout_slug": "package-manager",
                    }
                ),
            )
        stage1_id = re.search(r"stage1_id=(m1_[a-f0-9]+)", prompt)
        assert stage1_id is not None
        return SimpleNamespace(
            finish_reason="stop",
            content=json.dumps(
                {
                    "memory_summary": "Use pnpm for this project.",
                    "memory_markdown": "# Project memory\n\n- Use pnpm.",
                    "entries": [
                        {
                            "key": "package-manager",
                            "kind": "workflow",
                            "title": "Package manager",
                            "content": "Use pnpm.",
                            "confidence": 0.95,
                            "stage1_ids": [stage1_id.group(1)],
                        }
                    ],
                }
            ),
        )

    provider = SimpleNamespace(chat_with_retry=AsyncMock(side_effect=respond))
    store = MemoryStore(tmp_path / "managed-memory" / project.id)
    pipeline = ProjectMemoryPipeline(
        state=state,
        provider=provider,
        model="test-model",
        config=ProjectMemoryPipelineConfig(idle_seconds=0),
    )
    messages = [
        {"role": "user", "content": "Remember our password=hunter2 and use pnpm."},
        {"role": "assistant", "content": "I will use pnpm."},
    ]

    await pipeline.process_session_snapshot(
        project_id=project.id,
        session_id=session.id,
        session_key=session.session_key,
        revision="rev-1",
        messages=messages,
        memory_store=store,
    )

    stage1 = state.list_project_memory_stage1(project.id)
    assert len(stage1) == 1
    assert "hunter2" not in stage1[0]["raw_memory"]
    assert "[REDACTED_SECRET]" in stage1[0]["raw_memory"]
    assert store.read_memory_summary() == "Use pnpm for this project."
    assert "Use pnpm." in store.read_memory()
    [memory_skill] = list(store.memory_skills_dir.glob("*.md"))
    assert "Use pnpm." in memory_skill.read_text(encoding="utf-8")
    memories = state.list_project_memories(project.id)
    assert [row["kind"] for row in memories] == ["workflow"]
    assert state.list_project_memory_sources(project.id, memories[0]["id"])[0][
        "source_session_id"
    ] == session.id
    assert state.list_project_memories(other.id) == []

    await pipeline.process_session_snapshot(
        project_id=project.id,
        session_id=session.id,
        session_key=session.session_key,
        revision="rev-1",
        messages=messages,
        memory_store=store,
    )
    assert provider.chat_with_retry.await_count == 2


@pytest.mark.asyncio
async def test_invalid_phase2_payload_uses_sourced_fallback(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    state = StateStore(tmp_path / ".nanobot" / "state.sqlite", default_workspace=tmp_path)
    project = state.ensure_project(project_root, kind="workspace")
    session = state.bind_session("websocket:project", project.id)
    state.upsert_project_memory_stage1(
        project.id,
        source_session_id=session.id,
        source_rollout_revision="rev-1",
        raw_memory="Stable fact.",
        rollout_summary="Stable summary.",
    )
    provider = SimpleNamespace(
        chat_with_retry=AsyncMock(
            return_value=SimpleNamespace(
                finish_reason="stop",
                content='{"memory_summary": "missing the rest"}',
            )
        )
    )
    store = MemoryStore(tmp_path / "managed-memory" / project.id)
    store.write_memory("# Previous memory")
    store.write_memory_summary("Previous summary")
    pipeline = ProjectMemoryPipeline(
        state=state,
        provider=provider,
        model="test-model",
        config=ProjectMemoryPipelineConfig(idle_seconds=0),
    )

    refreshed = await pipeline.consolidate_project(project.id, store, force=True)

    assert refreshed is True
    assert "Stable fact." in store.read_memory()
    assert "Stable fact." in store.read_memory_summary()
    assert provider.chat_with_retry.await_count == 2
    assert state.project_memory_job_status(project.id)["phase2"]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_phase2_accepts_common_envelope_and_field_aliases(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    state = StateStore(tmp_path / ".nanobot" / "state.sqlite", default_workspace=tmp_path)
    project = state.ensure_project(project_root, kind="workspace")
    session = state.bind_session("websocket:project", project.id)
    stage1 = state.upsert_project_memory_stage1(
        project.id,
        source_session_id=session.id,
        source_rollout_revision="rev-1",
        raw_memory="Use pnpm.",
        rollout_summary="The project uses pnpm.",
    )
    provider = SimpleNamespace(
        chat_with_retry=AsyncMock(
            return_value=SimpleNamespace(
                finish_reason="stop",
                content=json.dumps(
                    {
                        "result": {
                            "summary": "Use pnpm for this project.",
                            "memories": {
                                "package-manager": {
                                    "kind": "workflow",
                                    "title": "Package manager",
                                    "text": "Use pnpm.",
                                    "sources": stage1,
                                }
                            },
                        }
                    }
                ),
            )
        )
    )
    store = MemoryStore(tmp_path / "managed-memory" / project.id)
    pipeline = ProjectMemoryPipeline(
        state=state,
        provider=provider,
        model="test-model",
        config=ProjectMemoryPipelineConfig(idle_seconds=0),
    )

    refreshed = await pipeline.consolidate_project(project.id, store, force=True)

    assert refreshed is True
    assert store.read_memory_summary() == "Use pnpm for this project."
    assert "# Project Memory" in store.read_memory()
    assert "Use pnpm." in store.read_memory()
    assert provider.chat_with_retry.await_count == 1


@pytest.mark.asyncio
async def test_provider_failure_keeps_previous_projection(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    state = StateStore(tmp_path / ".nanobot" / "state.sqlite", default_workspace=tmp_path)
    project = state.ensure_project(project_root, kind="workspace")
    session = state.bind_session("websocket:project", project.id)
    state.upsert_project_memory_stage1(
        project.id,
        source_session_id=session.id,
        source_rollout_revision="rev-1",
        raw_memory="Stable fact.",
        rollout_summary="Stable summary.",
    )
    provider = SimpleNamespace(
        chat_with_retry=AsyncMock(side_effect=RuntimeError("provider unavailable"))
    )
    store = MemoryStore(tmp_path / "managed-memory" / project.id)
    store.write_memory("# Previous memory")
    store.write_memory_summary("Previous summary")
    pipeline = ProjectMemoryPipeline(
        state=state,
        provider=provider,
        model="test-model",
        config=ProjectMemoryPipelineConfig(idle_seconds=0),
    )

    refreshed = await pipeline.consolidate_project(project.id, store, force=True)

    assert refreshed is False
    assert store.read_memory() == "# Previous memory"
    assert store.read_memory_summary() == "Previous summary"
    assert state.project_memory_job_status(project.id)["phase2"]["status"] == "failed"
