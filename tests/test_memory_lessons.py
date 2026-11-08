"""ADR 0125 compatibility without requiring either version of the host SDK."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from project_board.loop import BoardLoop

FEATURE = {"title": "Fix retry handling", "files_to_modify": ["loop/drive.py"]}
ROOT = Path(__file__).resolve().parent.parent


async def test_memory_and_knowledge_lessons_merge_by_content(monkeypatch):
    # The post-ADR 0125 ``memories`` store has NO ``domain`` field, so ``memory_search``
    # takes only (query, k). Use a strict signature here rather than an AsyncMock: a
    # stray ``domain=`` must raise (which the per-store except would swallow, dropping
    # every migrated lesson) so the regression is caught instead of silently passing.
    memory_calls = []

    async def memory_search(query, *, k):
        memory_calls.append((query, k))
        return [{"content": " shared "}, {"content": "memory lesson"}]

    knowledge = AsyncMock(
        return_value=[{"content": "shared", "preview": "different preview"}, {"preview": "legacy lesson"}]
    )
    monkeypatch.setattr("graph.sdk.memory_search", memory_search, raising=False)
    monkeypatch.setattr("graph.sdk.knowledge_search", knowledge)

    result = await BoardLoop({"kg_lessons_k": 2})._fetch_kg_lessons(FEATURE)

    assert result == "- shared\n- memory lesson\n- legacy lesson"
    # memories store queried WITHOUT domain; legacy chunks store still scoped by domain.
    assert memory_calls == [("Fix retry handling loop/drive.py", 2)]
    knowledge.assert_awaited_once_with("Fix retry handling loop/drive.py", k=2, domain="loop-lessons")


async def test_legacy_sdk_keeps_query_and_formatting(monkeypatch):
    monkeypatch.delattr("graph.sdk.memory_search", raising=False)
    knowledge = AsyncMock(return_value=[{"preview": " summary ", "content": "full"}, {"content": "body"}, {}])
    monkeypatch.setattr("graph.sdk.knowledge_search", knowledge)

    assert await BoardLoop({"kg_lessons_domain": ""})._fetch_kg_lessons(FEATURE) == "- summary\n- body"
    knowledge.assert_awaited_once_with("Fix retry handling loop/drive.py", k=3, domain=None)


@pytest.mark.parametrize("failing_store", ["memory_search", "knowledge_search"])
async def test_store_failure_preserves_other_lessons(monkeypatch, failing_store):
    for name in ("memory_search", "knowledge_search"):
        search = (
            AsyncMock(side_effect=RuntimeError("store down"))
            if name == failing_store
            else AsyncMock(return_value=[{"content": "surviving lesson"}])
        )
        monkeypatch.setattr(f"graph.sdk.{name}", search, raising=False)

    assert await BoardLoop({})._fetch_kg_lessons(FEATURE) == "- surviving lesson"


@pytest.mark.parametrize("config, feature", [({"kg_lessons": False}, FEATURE), ({}, {})])
async def test_disabled_or_empty_query_never_reads_either_store(monkeypatch, config, feature):
    memory, knowledge = AsyncMock(), AsyncMock()
    monkeypatch.setattr("graph.sdk.memory_search", memory, raising=False)
    monkeypatch.setattr("graph.sdk.knowledge_search", knowledge)
    assert await BoardLoop(config)._fetch_kg_lessons(feature) == ""
    memory.assert_not_awaited()
    knowledge.assert_not_awaited()


def test_retro_declares_the_current_memory_tools_only():
    # Core 0.231.0 (the plugin's floor) removed memory_recall/memory_ingest (ADR 0125).
    skill = (ROOT / "skills/loop-retro/SKILL.md").read_text()
    frontmatter = yaml.safe_load(skill.split("---", 2)[1])
    assert {"memory_search", "memory_save"} <= set(frontmatter["tools"])
    for removed in ("memory_recall", "memory_ingest", "forget_memory", "memory_stats"):
        assert removed not in skill
    assert "memory_remember" not in skill
    assert 'kind="fact"' in skill
    assert "`subject` = the failure class" in skill
    # The new memories store has no domain field (ADR 0125): the skill must scope a
    # saved lesson by subject.
    assert "memories store has no domain field" in skill
