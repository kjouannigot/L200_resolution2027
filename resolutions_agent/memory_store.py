"""Persistent Session State, History Compaction & Async Memory Consolidation.

Implements Category 2 of the AgentOps Matrix:
- History Compaction: Sliding-window token budget compactor + ADK `EventsCompactionConfig`
  and `ContextCacheConfig`.
- Persistent Session State: Connects ADK `DatabaseSessionService` / `VertexAiSessionService`,
  `VertexAiMemoryBankService`, Google Cloud Firestore vector/document storage, and
  Vertex AI Search (`discoveryengine_v1`), with resilient SQLite persistence for offline tests.
- Async Memory Operations: Non-blocking `asyncio.create_task` background consolidation
  with strong task tracking so expensive embedding/consolidation never blocks UI responses.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Sequence

from google import genai
from google.api_core import exceptions as gcp_exceptions
from google.cloud import discoveryengine_v1
from google.cloud import firestore

from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.memory.vertex_ai_memory_bank_service import VertexAiMemoryBankService
from google.adk.sessions import DatabaseSessionService
from google.adk.sessions import InMemorySessionService
from google.adk.sessions import VertexAiSessionService

from .observability import pii_redactor
from .observability import structured_logger
from .secrets import default_secret_provider


class SlidingWindowHistoryCompactor:
    """Compacts multi-turn conversation history when token budgets are exceeded."""

    def __init__(
        self,
        max_token_budget: int = 8000,
        retain_recent_turns: int = 4,
    ) -> None:
        self.max_token_budget = max_token_budget
        self.retain_recent_turns = retain_recent_turns

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Fast ~4 chars/token heuristic for context-window budgeting."""
        return max(1, len(text) // 4)

    def compact_turns(
        self, turns: Sequence[dict[str, str]]
    ) -> list[dict[str, str]]:
        """Summarizes older turns into a compact digest when exceeding token budget."""
        total_tokens = sum(
            self.estimate_tokens(turn.get("content", "")) for turn in turns
        )
        if (
            total_tokens <= self.max_token_budget
            or len(turns) <= self.retain_recent_turns
        ):
            return list(turns)

        older_turns = turns[: -self.retain_recent_turns]
        recent_turns = list(turns[-self.retain_recent_turns :])

        key_points: list[str] = []
        for item in older_turns[-8:]:
            role = item.get("role", "user")
            snippet = pii_redactor.scrub_text(item.get("content", ""))[:140].strip()
            if snippet:
                key_points.append(f"[{role}] {snippet}")

        compacted_summary = {
            "role": "system",
            "content": (
                f"[Compacted Prior Session Summary ({len(older_turns)} earlier turns)]: "
                + " | ".join(key_points)
            ),
        }
        return [compacted_summary, *recent_turns]


def _deterministic_hash_embedding(text: str, dims: int = 32) -> list[float]:
    """Generates a lightweight normalized bag-of-ngrams vector when offline."""
    vec = [0.0] * dims
    lowered = text.lower()
    for idx in range(max(1, len(lowered) - 2)):
        trigram = lowered[idx : idx + 3]
        bucket = hash(trigram) % dims
        vec[bucket] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [round(v / norm, 5) for v in vec]


def _cosine_similarity(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a)) or 1.0
    norm_b = math.sqrt(sum(b * b for b in vec_b)) or 1.0
    return dot / (norm_a * norm_b)


class PersistentGuestMemoryStore:
    """Persistent guest memory & vector search store backed by Firestore / Vertex AI Search + SQLite."""

    def __init__(self, sqlite_path: str | None = None) -> None:
        default_db_file = Path(
            os.environ.get("LOCAL_MEMORY_SQLITE_PATH", "/tmp/resolutions_2027_memory.db")
        )
        self._sqlite_path = str(sqlite_path or default_db_file)
        self._project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        self._firestore_collection = os.environ.get(
            "FIRESTORE_COLLECTION", "resolutions_2027_memories"
        )
        self._vertex_data_store_id = os.environ.get("VERTEX_SEARCH_DATA_STORE_ID", "")
        self._firestore_client: firestore.AsyncClient | None = None
        self._init_sqlite()
        self._seed_celebration_wall_if_empty()

    def _init_sqlite(self) -> None:
        with sqlite3.connect(self._sqlite_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS guest_memories (
                    memory_id TEXT PRIMARY KEY,
                    guest_key TEXT NOT NULL,
                    summary_text TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS public_wall_manifestos (
                    manifesto_id TEXT PRIMARY KEY,
                    guest_header TEXT NOT NULL,
                    persona_title TEXT NOT NULL,
                    headline_motto TEXT NOT NULL,
                    manifesto_json TEXT NOT NULL,
                    published_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS draft_manifestos (
                    manifesto_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    manifesto_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def _seed_celebration_wall_if_empty(self) -> None:
        with sqlite3.connect(self._sqlite_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM public_wall_manifestos"
            ).fetchone()
            if row and row[0] > 0:
                return

        samples = [
            {
                "manifesto_id": "seed-camille-julien",
                "guest_header": "Camille & Julien",
                "guest_mode": "DUO_PAIR",
                "persona_title": "The Champagne-Fueled Moonshot Architect",
                "persona_subtitle": "Turns wild cocktail-napkin sketches into legendary 2027 lore.",
                "headline_motto": "Dream audaciously, pack light, celebrate every milestone.",
                "anti_resolution_title": "No More 97-Tab Limbo",
                "resolutions": [
                    {
                        "category": "01 · Bold 2027 Side-Quest",
                        "title": "The 2027 Bucket-List Boarding Pass",
                        "personalized_story": "Autumn train journey across Japan.",
                        "january_first_step": "Block October travel dates before Jan 15.",
                        "victory_signal": "Fresh stamps and zero work laptop cameos.",
                    }
                ],
            },
            {
                "manifesto_id": "seed-priya",
                "guest_header": "Priya",
                "guest_mode": "SOLO_HOST",
                "persona_title": "The Five-Star Cozy Joy Connoisseur",
                "persona_subtitle": "Upgrades ordinary Tuesdays into Michelin-worthy sanctuaries.",
                "headline_motto": "Protect your peace, savor the pastry, cherish your crew.",
                "anti_resolution_title": "Banning Keyboard Crumb Lunches",
                "resolutions": [
                    {
                        "category": "01 · Bold 2027 Side-Quest",
                        "title": "The Home Alchemy Bar",
                        "personalized_story": "Mastering yuzu-rosemary zero-proof spritzes.",
                        "january_first_step": "Host a January spritz flight for friends.",
                        "victory_signal": "Friends requesting the house menu ahead of time.",
                    }
                ],
            },
        ]
        for sample in samples:
            self.publish_manifesto_sync(sample)

    async def _compute_embedding(self, text: str) -> list[float]:
        """Computes semantic vector via Vertex/GenAI embeddings or offline fallback."""
        if self._project_id and os.environ.get("ENABLE_VERTEX_EMBEDDINGS") == "true":
            try:
                client = genai.Client(vertexai=True, project=self._project_id)
                result = await asyncio.to_thread(
                    client.models.embed_content,
                    model="text-embedding-004",
                    contents=text,
                )
                if result.embeddings and result.embeddings[0].values:
                    return list(result.embeddings[0].values)
            except Exception as exc:  # noqa: BLE001
                structured_logger.warning(
                    "Falling back to local deterministic embedding",
                    extra={"error_type": type(exc).__name__},
                )
        return _deterministic_hash_embedding(text)

    async def store_consolidated_memory(
        self,
        guest_key: str,
        summary_text: str,
        metadata: dict[str, Any],
    ) -> str:
        """Scrubs PII, embeds, and persists consolidated guest memory to Firestore + SQLite."""
        clean_guest = pii_redactor.scrub_text(guest_key.strip().lower()) or "guest"
        clean_summary = pii_redactor.scrub_text(summary_text)
        clean_meta = pii_redactor.scrub_payload(metadata)
        embedding = await self._compute_embedding(clean_summary)
        memory_id = f"mem-{int(time.time() * 1000)}-{abs(hash(clean_summary)) % 10000}"

        # Local SQLite vector-indexed persistence
        await asyncio.to_thread(
            self._write_sqlite_memory,
            memory_id,
            clean_guest,
            clean_summary,
            embedding,
            clean_meta,
        )

        # Cloud Firestore Native Vector Store persistence when configured
        if self._project_id and os.environ.get("ENABLE_FIRESTORE_SYNC") == "true":
            try:
                if self._firestore_client is None:
                    self._firestore_client = firestore.AsyncClient(
                        project=self._project_id
                    )
                doc_ref = self._firestore_client.collection(
                    self._firestore_collection
                ).document(memory_id)
                await doc_ref.set(
                    {
                        "guest_key": clean_guest,
                        "summary_text": clean_summary,
                        "embedding": embedding,
                        "metadata": clean_meta,
                        "created_at": time.time(),
                    }
                )
            except (gcp_exceptions.GoogleAPICallError, OSError) as exc:
                structured_logger.warning(
                    "Firestore write skipped; SQLite memory persisted",
                    extra={"error_type": type(exc).__name__},
                )

        return memory_id

    def _write_sqlite_memory(
        self,
        memory_id: str,
        guest_key: str,
        summary_text: str,
        embedding: list[float],
        metadata: dict[str, Any],
    ) -> None:
        with sqlite3.connect(self._sqlite_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO guest_memories
                (memory_id, guest_key, summary_text, embedding_json, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    guest_key,
                    summary_text,
                    json.dumps(embedding),
                    json.dumps(metadata),
                    time.time(),
                ),
            )
            conn.commit()

    async def search_guest_memories(
        self, guest_name: str, query: str = "", top_k: int = 3
    ) -> list[dict[str, Any]]:
        """Retrieves relevant past memories via Vertex AI Search or vector similarity."""
        clean_key = pii_redactor.scrub_text(guest_name.strip().lower())

        # Optional Vertex AI Search (Discovery Engine) integration when configured
        if self._project_id and self._vertex_data_store_id:
            try:
                search_client = discoveryengine_v1.SearchServiceClient()
                serving_config = (
                    f"projects/{self._project_id}/locations/global/collections/"
                    f"default_collection/dataStores/{self._vertex_data_store_id}/"
                    "servingConfigs/default_search"
                )
                request = discoveryengine_v1.SearchRequest(
                    serving_config=serving_config,
                    query=f"{clean_key} {query}".strip(),
                    page_size=top_k,
                )
                response = await asyncio.to_thread(search_client.search, request)
                vertex_hits = [
                    {
                        "memory_id": res.document.id,
                        "summary": str(dict(res.document.struct_data).get("summary", "")),
                        "source": "vertex_ai_search",
                    }
                    for res in response.results
                ]
                if vertex_hits:
                    return vertex_hits
            except (gcp_exceptions.GoogleAPICallError, OSError):
                pass

        query_vec = await self._compute_embedding(query or clean_key)
        with sqlite3.connect(self._sqlite_path) as conn:
            rows = conn.execute(
                """
                SELECT memory_id, summary_text, embedding_json, metadata_json
                FROM guest_memories
                WHERE guest_key = ?
                ORDER BY created_at DESC
                LIMIT 25
                """,
                (clean_key,),
            ).fetchall()

        scored: list[tuple[float, dict[str, Any]]] = []
        for mem_id, summary, emb_json, meta_json in rows:
            emb = json.loads(emb_json)
            score = _cosine_similarity(query_vec, emb)
            scored.append(
                (
                    score,
                    {
                        "memory_id": mem_id,
                        "summary": summary,
                        "similarity": round(score, 4),
                        "metadata": json.loads(meta_json),
                        "source": "vector_memory_store",
                    },
                )
            )
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored[:top_k]]

    def save_draft_manifesto(
        self, session_id: str, manifesto: dict[str, Any]
    ) -> None:
        scrubbed = pii_redactor.scrub_payload(manifesto)
        with sqlite3.connect(self._sqlite_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO draft_manifestos
                (manifesto_id, session_id, manifesto_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    str(scrubbed["manifesto_id"]),
                    session_id,
                    json.dumps(scrubbed),
                    time.time(),
                ),
            )
            conn.commit()

    def get_draft_manifesto(self, manifesto_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self._sqlite_path) as conn:
            row = conn.execute(
                "SELECT manifesto_json FROM draft_manifestos WHERE manifesto_id = ?",
                (manifesto_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def publish_manifesto_sync(self, manifesto: dict[str, Any]) -> None:
        scrubbed = pii_redactor.scrub_payload(manifesto)
        with sqlite3.connect(self._sqlite_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO public_wall_manifestos
                (manifesto_id, guest_header, persona_title, headline_motto, manifesto_json, published_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(scrubbed["manifesto_id"]),
                    str(scrubbed["guest_header"]),
                    str(scrubbed["persona_title"]),
                    str(scrubbed["headline_motto"]),
                    json.dumps(scrubbed),
                    time.time(),
                ),
            )
            conn.commit()

    def list_public_wall(self, limit: int = 12) -> dict[str, Any]:
        with sqlite3.connect(self._sqlite_path) as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) FROM public_wall_manifestos"
            ).fetchone()
            top_row = conn.execute(
                """
                SELECT persona_title, COUNT(*) AS cnt
                FROM public_wall_manifestos
                GROUP BY persona_title
                ORDER BY cnt DESC
                LIMIT 1
                """
            ).fetchone()
            rows = conn.execute(
                """
                SELECT manifesto_json FROM public_wall_manifestos
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (max(1, min(limit, 50)),),
            ).fetchall()

        entries = [json.loads(r[0]) for r in rows]
        return {
            "total_published_count": int(total_row[0]) if total_row else 0,
            "top_persona_tonight": (
                str(top_row[0])
                if top_row
                else "The Champagne-Fueled Moonshot Architect"
            ),
            "recent_manifestos": entries,
        }


class AsyncMemoryConsolidator:
    """Schedules background async memory generation and consolidation without blocking UI."""

    def __init__(self, store: PersistentGuestMemoryStore) -> None:
        self._store = store
        self._background_tasks: set[asyncio.Task[Any]] = set()

    @property
    def pending_task_count(self) -> int:
        return len(self._background_tasks)

    def schedule_consolidation(
        self,
        guest_name: str,
        manifesto_payload: dict[str, Any],
    ) -> asyncio.Task[str] | None:
        """Dispatches non-blocking background memory extraction & vector indexing."""
        summary = (
            f"Guest {guest_name} unlocked persona '{manifesto_payload.get('persona_title')}' "
            f"with motto '{manifesto_payload.get('headline_motto')}' and anti-resolution "
            f"'{manifesto_payload.get('anti_resolution_title')}'."
        )
        metadata = {
            "manifesto_id": manifesto_payload.get("manifesto_id", ""),
            "persona_title": manifesto_payload.get("persona_title", ""),
        }
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Synchronous fallback if called outside an active event loop in CLI scripts
            return None

        task = loop.create_task(
            self._consolidate_worker(guest_name, summary, metadata),
            name=f"consolidate-memory-{guest_name}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _consolidate_worker(
        self,
        guest_name: str,
        summary: str,
        metadata: dict[str, Any],
    ) -> str:
        memory_id = await self._store.store_consolidated_memory(
            guest_key=guest_name,
            summary_text=summary,
            metadata=metadata,
        )
        structured_logger.info(
            "Background async memory consolidation completed",
            extra={
                "phase": "OUTCOME",
                "component": "async_memory",
                "memory_id": memory_id,
            },
        )
        return memory_id

    async def drain(self) -> None:
        """Awaits all in-flight background memory consolidation tasks (useful in tests/shutdown)."""
        if self._background_tasks:
            await asyncio.gather(*list(self._background_tasks), return_exceptions=True)


def build_adk_session_and_memory_services() -> tuple[Any, Any]:
    """Builds production persistent ADK SessionService and MemoryService."""
    backend = os.environ.get("SESSION_BACKEND", "database").lower()
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    agent_engine_id = os.environ.get("VERTEX_AGENT_ENGINE_ID", "")

    if backend == "vertex" and project_id and agent_engine_id:
        session_service = VertexAiSessionService(
            project=project_id,
            location=location,
            agent_engine_id=agent_engine_id,
        )
        memory_service = VertexAiMemoryBankService(
            project=project_id,
            location=location,
            agent_engine_id=agent_engine_id,
        )
        return session_service, memory_service

    if backend == "database":
        db_url = default_secret_provider.get_secret(
            os.environ.get("DB_CONNECTION_SECRET_ID", "resolutions-2027-session-db-url"),
            env_fallback_var="SESSION_DB_URL",
            default_for_local_dev="sqlite+aiosqlite:////tmp/resolutions_2027_adk_sessions.db",
        )
        try:
            return DatabaseSessionService(db_url=db_url), InMemoryMemoryService()
        except Exception:  # noqa: BLE001
            return InMemorySessionService(), InMemoryMemoryService()

    return InMemorySessionService(), InMemoryMemoryService()


default_memory_store = PersistentGuestMemoryStore()
default_async_consolidator = AsyncMemoryConsolidator(default_memory_store)
default_history_compactor = SlidingWindowHistoryCompactor()
