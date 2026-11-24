"""FastAPI Server hosting the ADK Runner, HITL Confirmation Endpoints & Web UI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.genai import types
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from google.adk import Runner

from resolutions_agent.agent import app as adk_app
from resolutions_agent.memory_store import build_adk_session_and_memory_services
from resolutions_agent.observability import otel_tracer
from resolutions_agent.observability import pii_redactor
from resolutions_agent.observability import structured_logger
from resolutions_agent.tools import deal_unique_social_quiz_questions
from resolutions_agent.tools import fetch_live_celebration_wall_highlights
from resolutions_agent.tools import publish_manifesto_to_public_celebration_wall
from resolutions_agent.tools import swap_single_dimension_question
from resolutions_agent.tools import synthesize_personalized_2027_manifesto

STATIC_DIR = Path(__file__).parent / "static"

session_service, memory_service = build_adk_session_and_memory_services()
adk_runner = Runner(
    app=adk_app,
    session_service=session_service,
    memory_service=memory_service,
)

fastapi_app = FastAPI(
    title="2027 Resolutions Atelier — ADK Agent API",
    version="1.0.0",
    description=(
        "Interactive 2027 Resolutions Creator powered by Google ADK Multi-Agent "
        "orchestration, HITL confirmation gates, and OpenTelemetry tracing."
    ),
)
fastapi_app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_wall_subscribers: set[asyncio.Queue[str]] = set()


def _broadcast_wall_update() -> None:
    """Pushes the latest Celebration Wall snapshot to all projected screens."""
    if not _wall_subscribers:
        return
    snapshot = fetch_live_celebration_wall_highlights(limit=15)
    payload = json.dumps(snapshot)
    for queue in list(_wall_subscribers):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass


class AgentChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1, max_length=64)
    session_id: str | None = Field(default=None, max_length=64)
    message: str = Field(..., min_length=1, max_length=2000)


class StartQuizApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guest_name: str = Field(default="Social Guest", max_length=80)
    plus_one_name: str = Field(default="", max_length=80)
    guest_mode: str = Field(default="SOLO_HOST")
    excluded_question_ids: list[str] = Field(default_factory=list)


class SwapQuestionApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: str
    excluded_question_ids: list[str] = Field(default_factory=list)


class GenerateManifestoApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    guest_name: str
    plus_one_name: str = ""
    guest_mode: str = "SOLO_HOST"
    answers: list[dict[str, Any]]
    remix_seed: int = 0


class PublishWallApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    manifesto_id: str
    human_confirmed: bool = False
    approval_token: str = ""


@fastapi_app.get("/")
async def serve_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@fastapi_app.get("/healthz")
async def health_check() -> dict[str, str]:
    return {"status": "healthy", "app": adk_app.name}


@fastapi_app.post("/api/quiz/start")
async def api_start_quiz(req: StartQuizApiRequest) -> dict[str, Any]:
    with otel_tracer.start_as_current_span("http.api_start_quiz"):
        result = deal_unique_social_quiz_questions(
            guest_name=req.guest_name,
            guest_mode=req.guest_mode,
            plus_one_name=req.plus_one_name,
            excluded_question_ids=req.excluded_question_ids,
        )
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result)
        return result


@fastapi_app.post("/api/quiz/swap")
async def api_swap_question(req: SwapQuestionApiRequest) -> dict[str, Any]:
    with otel_tracer.start_as_current_span("http.api_swap_question"):
        result = swap_single_dimension_question(
            dimension=req.dimension,
            excluded_question_ids=req.excluded_question_ids,
        )
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result)
        return result


@fastapi_app.post("/api/manifesto/generate")
async def api_generate_manifesto(req: GenerateManifestoApiRequest) -> dict[str, Any]:
    with otel_tracer.start_as_current_span("http.api_generate_manifesto"):
        result = synthesize_personalized_2027_manifesto(
            session_id=req.session_id,
            guest_name=req.guest_name,
            plus_one_name=req.plus_one_name,
            guest_mode=req.guest_mode,
            answers=req.answers,
            remix_seed=req.remix_seed,
        )
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result)
        return result


@fastapi_app.post("/api/wall/publish")
async def api_publish_wall(req: PublishWallApiRequest) -> dict[str, Any]:
    with otel_tracer.start_as_current_span("http.api_publish_wall"):
        result = publish_manifesto_to_public_celebration_wall(
            session_id=req.session_id,
            manifesto_id=req.manifesto_id,
            human_confirmed=req.human_confirmed,
            approval_token=req.approval_token,
        )
        if result.get("status") == "published":
            _broadcast_wall_update()
        return result


@fastapi_app.get("/api/wall")
async def api_list_wall(limit: int = 12) -> dict[str, Any]:
    with otel_tracer.start_as_current_span("http.api_list_wall"):
        return fetch_live_celebration_wall_highlights(limit=limit)


@fastapi_app.get("/api/wall/stream")
async def api_stream_wall() -> StreamingResponse:
    """Server-Sent Events stream for live projected laptop screens."""
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=16)
    _wall_subscribers.add(queue)

    async def event_generator():
        try:
            initial = fetch_live_celebration_wall_highlights(limit=15)
            yield f"data: {json.dumps(initial)}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _wall_subscribers.discard(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@fastapi_app.post("/api/agent/chat")
async def api_agent_chat(req: AgentChatRequest) -> dict[str, Any]:
    """Conversational multi-agent ADK endpoint with persistent session state."""
    with otel_tracer.start_as_current_span("http.api_agent_chat") as span:
        span.set_attribute("app.user_id", pii_redactor.scrub_text(req.user_id))
        session_id = req.session_id
        if not session_id:
            created = await session_service.create_session(
                app_name=adk_app.name,
                user_id=req.user_id,
            )
            session_id = created.id

        content = types.Content(
            role="user",
            parts=[types.Part(text=req.message)],
        )
        replies: list[str] = []
        async for event in adk_runner.run_async(
            user_id=req.user_id,
            session_id=session_id,
            new_message=content,
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        replies.append(part.text)

        structured_logger.info(
            "Completed conversational ADK turn",
            extra={
                "phase": "OUTCOME",
                "component": "fastapi_chat",
                "session_id": session_id,
            },
        )
        return {
            "session_id": session_id,
            "reply": "\n".join(replies).strip(),
        }
