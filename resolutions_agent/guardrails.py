"""Security Guardrails, Strategic Model Routing & Human-in-the-Loop Policy Plugin.

Implements Category 3 of the AgentOps Matrix:
- Strategic Model Routing: Routes fast quiz/wall turns to `gemini-2.5-flash` and
  complex multi-constraint resolution synthesis / self-evaluation to `gemini-2.5-pro`.
- Guardrails & Policy Plugins: Screens prompts for prompt-injection & unsafe advice
  before LLM invocation (`before_model_callback`) and scrubs/evaluates outputs after
  LLM generation (`after_model_callback`).
- Human-in-the-Loop Hooks: Enforces explicit code stops requiring human confirmation
  and cryptographic HMAC token validation before executing public/external write tools.
"""

from __future__ import annotations

import os
import re
from typing import Any

from google.genai import types

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from .observability import pii_redactor
from .observability import structured_logger
from .secrets import default_secret_provider

FAST_MODEL = os.environ.get("FAST_MODEL_NAME", "gemini-2.5-flash")
PRO_MODEL = os.environ.get("PRO_MODEL_NAME", "gemini-2.5-pro")

_PROMPT_INJECTION_PATTERNS = (
    re.compile(r"(?i)ignore\s+(all\s+)?(previous|prior|system)\s+instructions"),
    re.compile(r"(?i)reveal\s+(your\s+)?system\s+prompt"),
    re.compile(r"(?i)dump\s+(all\s+)?secret"),
    re.compile(r"(?i)bypass\s+human\s+confirmation"),
)

_DISALLOWED_ADVICE_PATTERNS = (
    re.compile(r"(?i)\b(prescribe\s+medication|medical\s+diagnosis|tax\s+evasion)\b"),
)

HITL_PROTECTED_TOOLS = frozenset(
    {
        "publish_manifesto_to_public_celebration_wall",
    }
)


class StrategicModelRouter:
    """Selects the optimal Gemini model tier (`Flash` vs `Pro`) per task complexity."""

    def __init__(
        self,
        fast_model: str = FAST_MODEL,
        pro_model: str = PRO_MODEL,
    ) -> None:
        self.fast_model = fast_model
        self.pro_model = pro_model

    def select_model(self, agent_name: str, user_prompt_text: str = "") -> str:
        """Routes synthesis & self-critique to Pro, and low-latency interactions to Flash."""
        pro_agents = {
            "resolution_composer_agent",
            "manifesto_self_eval_agent",
        }
        if agent_name in pro_agents:
            return self.pro_model

        complex_markers = ("manifesto", "synthesize", "duo", "personalize", "evaluate")
        if len(user_prompt_text) > 360 or any(
            marker in user_prompt_text.lower() for marker in complex_markers
        ):
            return self.pro_model

        return self.fast_model


default_model_router = StrategicModelRouter()


class SafetyAndPolicyGuardrailPlugin(BasePlugin):
    """ADK global plugin enforcing input/output guardrails, PII safety, and HITL gates."""

    def __init__(self, router: StrategicModelRouter | None = None) -> None:
        super().__init__(name="safety_and_policy_guardrail_plugin")
        self._router = router or default_model_router

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        combined_user_text = self._extract_latest_user_text(llm_request)

        # 1. Prompt-Injection & Safety Guardrail Check
        for pattern in (*_PROMPT_INJECTION_PATTERNS, *_DISALLOWED_ADVICE_PATTERNS):
            if pattern.search(combined_user_text):
                structured_logger.warning(
                    "Guardrail intercepted unsafe or injection prompt",
                    extra={
                        "phase": "OUTCOME",
                        "component": "guardrail",
                        "agent_name": callback_context.agent_name,
                        "action": "blocked_input",
                    },
                )
                return LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                text=(
                                    "I keep the 2027 Resolutions Atelier focused strictly on "
                                    "fun, celebratory personal resolutions and safe party vibes! "
                                    "Want to jump into 5 playful questions for 2027 instead?"
                                )
                            )
                        ],
                    )
                )

        # 2. Dynamic Strategic Model Routing (only when using string Gemini identifiers)
        if isinstance(getattr(llm_request, "model", None), str) and str(
            llm_request.model
        ).startswith("gemini-"):
            routed_model = self._router.select_model(
                callback_context.agent_name, combined_user_text
            )
            llm_request.model = routed_model

        return None

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        """Self-eval output guardrail: scrubs accidental PII from model text."""
        del callback_context
        if not llm_response.content or not llm_response.content.parts:
            return None

        modified = False
        new_parts: list[types.Part] = []
        for part in llm_response.content.parts:
            if part.text:
                scrubbed = pii_redactor.scrub_text(part.text)
                if scrubbed != part.text:
                    modified = True
                    new_parts.append(types.Part(text=scrubbed))
                    continue
            new_parts.append(part)

        if modified:
            return LlmResponse(
                content=types.Content(
                    role=llm_response.content.role or "model",
                    parts=new_parts,
                )
            )
        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        """Explicit Human-in-the-Loop (HITL) enforcement gate for high-stakes tools."""
        if tool.name not in HITL_PROTECTED_TOOLS:
            return None

        session_id = str(tool_args.get("session_id", "")).strip()
        manifesto_id = str(tool_args.get("manifesto_id", "")).strip()
        human_confirmed = bool(tool_args.get("human_confirmed", False))
        provided_token = str(tool_args.get("approval_token", "")).strip()

        expected_token = default_secret_provider.create_hitl_approval_token(
            session_id=session_id,
            manifesto_id=manifesto_id,
        )

        if not human_confirmed or provided_token != expected_token:
            tool_context.state["pending_hitl_manifesto_id"] = manifesto_id
            tool_context.state["pending_hitl_approval_token"] = expected_token
            structured_logger.info(
                "Human-in-the-Loop confirmation gate paused public wall publication",
                extra={
                    "phase": "OUTCOME",
                    "component": "hitl_gate",
                    "tool_name": tool.name,
                    "manifesto_id": manifesto_id,
                },
            )
            return {
                "status": "requires_human_confirmation",
                "hitl_required": True,
                "manifesto_id": manifesto_id,
                "approval_token": expected_token,
                "confirmation_prompt": (
                    "Publishing your 2027 Resolution Manifesto displays your display "
                    "name, 2027 persona, and motto on the shared Celebration Wall. "
                    "Please confirm explicitly if you would like to publish it!"
                ),
                "llm_recovery_instructions": (
                    "STOP and ask the human user for explicit permission to publish "
                    f"manifesto '{manifesto_id}' to the public Celebration Wall. "
                    "Only re-call `publish_manifesto_to_public_celebration_wall` with "
                    "`human_confirmed=True` and the issued `approval_token` AFTER the "
                    "user explicitly replies yes/approve."
                ),
            }

        return None

    @staticmethod
    def _extract_latest_user_text(llm_request: LlmRequest) -> str:
        contents = getattr(llm_request, "contents", None) or []
        for content in reversed(contents):
            if getattr(content, "role", "") == "user":
                parts = getattr(content, "parts", None) or []
                texts = [p.text for p in parts if getattr(p, "text", None)]
                if texts:
                    return "\n".join(texts)
        return ""
