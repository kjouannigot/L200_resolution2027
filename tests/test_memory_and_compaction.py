"""Unit tests for Category 2: Context, Compaction & Async Memory (20 pts)."""

from __future__ import annotations

from pathlib import Path

from resolutions_agent.agent import app
from resolutions_agent.memory_store import AsyncMemoryConsolidator
from resolutions_agent.memory_store import PersistentGuestMemoryStore
from resolutions_agent.memory_store import SlidingWindowHistoryCompactor


def test_adk_app_configures_compaction_and_context_caching() -> None:
    assert app.events_compaction_config is not None
    assert app.events_compaction_config.compaction_interval == 4
    assert app.events_compaction_config.token_threshold == 12000
    assert app.context_cache_config is not None
    assert app.context_cache_config.min_tokens == 2048


def test_sliding_window_compactor_summarizes_long_histories() -> None:
    compactor = SlidingWindowHistoryCompactor(
        max_token_budget=40,
        retain_recent_turns=2,
    )
    turns = [
        {"role": "user", "content": f"Turn {idx}: planning a big 2027 adventure!"}
        for idx in range(8)
    ]
    compacted = compactor.compact_turns(turns)
    assert len(compacted) == 3
    assert "Compacted Prior Session Summary" in compacted[0]["content"]


async def test_async_memory_consolidation_and_vector_retrieval(tmp_path: Path) -> None:
    db_file = tmp_path / "test_guest_memory.db"
    store = PersistentGuestMemoryStore(sqlite_path=str(db_file))
    consolidator = AsyncMemoryConsolidator(store)

    task = consolidator.schedule_consolidation(
        guest_name="Clara",
        manifesto_payload={
            "manifesto_id": "man-test-01",
            "persona_title": "The Champagne-Fueled Moonshot Architect",
            "headline_motto": "Dream audaciously, pack light.",
            "anti_resolution_title": "No More 97-Tab Limbo",
        },
    )
    assert task is not None
    await consolidator.drain()

    memories = await store.search_guest_memories("Clara", query="Moonshot")
    assert len(memories) == 1
    assert "Moonshot Architect" in memories[0]["summary"]
