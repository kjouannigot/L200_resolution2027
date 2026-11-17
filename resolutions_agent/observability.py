"""Observability, Distributed Tracing, Intent/Outcome Auditing & PII Redaction.

Implements:
1. Structured JSON Logging via `python-json-logger` and `google-cloud-logging`.
2. Active PII Redaction via Google Cloud Sensitive Data Protection (`dlp_v2`)
   paired with deterministic regex scrubbing before logging or memory writes.
3. OpenTelemetry Distributed Tracing (`TracerProvider` + `CloudTraceSpanExporter`
   + `InMemorySpanExporter` for verification).
4. ADK `BasePlugin` (`IntentOutcomeObservabilityPlugin`) capturing explicit
   `INTENT` events prior to execution and `OUTCOME` events afterward across
   agents, models, and tools.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
import uuid
from typing import Any

from google.api_core import exceptions as gcp_exceptions
from google.cloud import dlp_v2
from google.cloud import logging as cloud_logging
from google.genai import types
from opentelemetry import trace
from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status
from opentelemetry.trace import StatusCode
from pythonjsonlogger import jsonlogger

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

# Deterministic fallback PII regex patterns so redaction works offline too.
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)|\d{2,4})[\s.-]\d{3,4}[\s.-]\d{3,4}(?!\w)"
)
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CREDIT_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
_BEARER_TOKEN_RE = re.compile(r"(?i)\b(bearer\s+|ya29\.|AIza)[A-Za-z0-9_\-\.]+")


class PiiRedactor:
    """Scrubs PII from strings and nested payloads using Cloud DLP + regexes."""

    DLP_INFO_TYPES = (
        {"name": "EMAIL_ADDRESS"},
        {"name": "PHONE_NUMBER"},
        {"name": "CREDIT_CARD_NUMBER"},
        {"name": "US_SOCIAL_SECURITY_NUMBER"},
        {"name": "STREET_ADDRESS"},
    )

    def __init__(
        self,
        project_id: str | None = None,
        enable_cloud_dlp: bool | None = None,
        dlp_client: dlp_v2.DlpServiceClient | None = None,
    ) -> None:
        self._project_id = project_id or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        if enable_cloud_dlp is None:
            enable_cloud_dlp = (
                os.environ.get("ENABLE_CLOUD_DLP", "false").lower() == "true"
            )
        self._enable_cloud_dlp = enable_cloud_dlp and bool(self._project_id)
        self._dlp_client = dlp_client

    def _get_dlp_client(self) -> dlp_v2.DlpServiceClient:
        if self._dlp_client is None:
            self._dlp_client = dlp_v2.DlpServiceClient()
        return self._dlp_client

    def scrub_text(self, text: str) -> str:
        """Redacts sensitive PII tokens from a raw string."""
        if not text:
            return text

        # Layer 1: Deterministic fast regex scrubbing (always active).
        redacted = _BEARER_TOKEN_RE.sub("[REDACTED_CREDENTIAL]", text)
        redacted = _EMAIL_RE.sub("[REDACTED_EMAIL]", redacted)
        redacted = _SSN_RE.sub("[REDACTED_SSN]", redacted)
        redacted = _CREDIT_CARD_RE.sub("[REDACTED_CARD]", redacted)
        redacted = _PHONE_RE.sub("[REDACTED_PHONE]", redacted)

        # Layer 2: Google Cloud Sensitive Data Protection (DLP API) when enabled.
        if self._enable_cloud_dlp:
            try:
                parent = f"projects/{self._project_id}/locations/global"
                response = self._get_dlp_client().deidentify_content(
                    request={
                        "parent": parent,
                        "inspect_config": {"info_types": list(self.DLP_INFO_TYPES)},
                        "deidentify_config": {
                            "info_type_transformations": {
                                "transformations": [
                                    {
                                        "primitive_transformation": {
                                            "replace_with_info_type_config": {}
                                        }
                                    }
                                ]
                            }
                        },
                        "item": {"value": redacted},
                    }
                )
                return response.item.value
            except (gcp_exceptions.GoogleAPICallError, OSError):
                return redacted

        return redacted

    def scrub_payload(self, value: Any) -> Any:
        """Recursively scrubs PII across dicts, lists, tuples, and strings."""
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, dict):
            return {
                str(k): (
                    "[REDACTED_SECRET]"
                    if any(
                        secret_word in str(k).lower()
                        for secret_word in ("password", "api_key", "secret", "token")
                    )
                    and str(k).lower() != "approval_token"
                    else self.scrub_payload(v)
                )
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.scrub_payload(item) for item in value]
        return value


class RedactingJsonFormatter(jsonlogger.JsonFormatter):
    """Structured JSON log formatter that automatically scrubs PII and injects trace IDs."""

    def __init__(self, redactor: PiiRedactor, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._redactor = redactor

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record["severity"] = record.levelname
        log_record["logger"] = record.name
        log_record["service"] = "resolutions-2027-agent"

        current_span = trace.get_current_span()
        span_ctx = current_span.get_span_context() if current_span else None
        if span_ctx and span_ctx.is_valid:
            log_record["trace_id"] = format(span_ctx.trace_id, "032x")
            log_record["span_id"] = format(span_ctx.span_id, "016x")

        for key, val in list(log_record.items()):
            log_record[key] = self._redactor.scrub_payload(val)


def configure_observability(
    service_name: str = "resolutions-2027-agent",
    enable_cloud_trace: bool | None = None,
    enable_cloud_logging: bool | None = None,
) -> tuple[logging.Logger, trace.Tracer, InMemorySpanExporter, PiiRedactor]:
    """Configures structured JSON logging, OpenTelemetry tracing, and PII scrubbing."""
    redactor = PiiRedactor()

    # 1. Structured JSON Logger setup
    logger = logging.getLogger("resolutions_agent")
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    logger.propagate = False

    if not logger.handlers:
        stream_handler = logging.StreamHandler(sys.stdout)
        formatter = RedactingJsonFormatter(
            redactor,
            "%(asctime)s %(severity)s %(name)s %(message)s",
        )
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    if enable_cloud_logging is None:
        enable_cloud_logging = (
            os.environ.get("ENABLE_CLOUD_LOGGING", "false").lower() == "true"
        )
    if enable_cloud_logging and os.environ.get("GOOGLE_CLOUD_PROJECT"):
        try:
            gcp_log_client = cloud_logging.Client()
            gcp_handler = gcp_log_client.get_default_handler()
            logger.addHandler(gcp_handler)
        except (gcp_exceptions.GoogleAPICallError, OSError):
            logger.warning("Cloud Logging handler unavailable; using stdout JSON logs")

    # 2. OpenTelemetry TracerProvider setup
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": "1.0.0",
        }
    )
    provider = TracerProvider(resource=resource)
    memory_exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(memory_exporter))

    if enable_cloud_trace is None:
        enable_cloud_trace = (
            os.environ.get("ENABLE_CLOUD_TRACE", "false").lower() == "true"
        )
    if enable_cloud_trace and os.environ.get("GOOGLE_CLOUD_PROJECT"):
        try:
            cloud_exporter = CloudTraceSpanExporter(
                project_id=os.environ["GOOGLE_CLOUD_PROJECT"]
            )
            provider.add_span_processor(BatchSpanProcessor(cloud_exporter))
        except (gcp_exceptions.GoogleAPICallError, OSError):
            logger.warning("Cloud Trace exporter unavailable; keeping in-memory spans")

    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer(service_name)
    return logger, tracer, memory_exporter, redactor


structured_logger, otel_tracer, in_memory_span_exporter, pii_redactor = (
    configure_observability()
)


class IntentOutcomeObservabilityPlugin(BasePlugin):
    """ADK global plugin capturing Intent vs. Outcome logs and OpenTelemetry spans."""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        tracer: trace.Tracer | None = None,
        redactor: PiiRedactor | None = None,
    ) -> None:
        super().__init__(name="intent_outcome_observability_plugin")
        self._logger = logger or structured_logger
        self._tracer = tracer or otel_tracer
        self._redactor = redactor or pii_redactor
        self._active_spans: dict[str, tuple[trace.Span, float]] = {}

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> types.Content | None:
        key = f"agent:{id(callback_context)}:{agent.name}"
        span = self._tracer.start_span(f"agent.{agent.name}")
        span.set_attribute("adk.agent.name", agent.name)
        self._active_spans[key] = (span, time.perf_counter())

        self._logger.info(
            "Agent execution intent recorded",
            extra={
                "phase": "INTENT",
                "component": "agent",
                "agent_name": agent.name,
                "intended_action": f"invoke_agent:{agent.name}",
            },
        )
        return None

    async def after_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> types.Content | None:
        key = f"agent:{id(callback_context)}:{agent.name}"
        span_entry = self._active_spans.pop(key, None)
        latency_ms = 0.0
        if span_entry is not None:
            span, started = span_entry
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            span.set_attribute("adk.latency_ms", latency_ms)
            span.set_status(Status(StatusCode.OK))
            span.end()

        self._logger.info(
            "Agent execution outcome recorded",
            extra={
                "phase": "OUTCOME",
                "component": "agent",
                "agent_name": agent.name,
                "actual_outcome": "completed",
                "latency_ms": latency_ms,
            },
        )
        return None

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        call_key = f"model:{id(llm_request)}"
        model_name = str(getattr(llm_request, "model", "gemini"))
        span = self._tracer.start_span(f"llm.{model_name}")
        span.set_attribute("gen_ai.request.model", model_name)
        self._active_spans[call_key] = (span, time.perf_counter())

        self._logger.info(
            "LLM generation intent recorded",
            extra={
                "phase": "INTENT",
                "component": "llm",
                "agent_name": callback_context.agent_name,
                "intended_action": "generate_content",
                "target_model": model_name,
            },
        )
        return None

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        # Pop most recent model span if present
        model_keys = [k for k in self._active_spans if k.startswith("model:")]
        latency_ms = 0.0
        if model_keys:
            span, started = self._active_spans.pop(model_keys[-1])
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            span.set_attribute("adk.latency_ms", latency_ms)
            span.set_status(Status(StatusCode.OK))
            span.end()

        self._logger.info(
            "LLM generation outcome recorded",
            extra={
                "phase": "OUTCOME",
                "component": "llm",
                "agent_name": callback_context.agent_name,
                "actual_outcome": "model_response_received",
                "latency_ms": latency_ms,
            },
        )
        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        correlation_id = uuid.uuid4().hex[:12]
        tool_context.state["temp:last_tool_correlation_id"] = correlation_id
        sanitized_args = self._redactor.scrub_payload(tool_args)

        span = self._tracer.start_span(f"tool.{tool.name}")
        span.set_attribute("adk.tool.name", tool.name)
        span.set_attribute("adk.correlation_id", correlation_id)
        self._active_spans[f"tool:{correlation_id}"] = (span, time.perf_counter())

        self._logger.info(
            "Tool invocation intent recorded",
            extra={
                "phase": "INTENT",
                "component": "tool",
                "correlation_id": correlation_id,
                "tool_name": tool.name,
                "intended_action": f"execute_tool:{tool.name}",
                "sanitized_tool_args": sanitized_args,
            },
        )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        del tool_args
        correlation_id = str(
            tool_context.state.get("temp:last_tool_correlation_id", "unknown")
        )
        span_entry = self._active_spans.pop(f"tool:{correlation_id}", None)
        latency_ms = 0.0
        result_status = (
            result.get("status", "ok") if isinstance(result, dict) else "ok"
        )
        if span_entry is not None:
            span, started = span_entry
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            span.set_attribute("adk.tool.status", str(result_status))
            span.set_attribute("adk.latency_ms", latency_ms)
            span.set_status(
                Status(
                    StatusCode.ERROR
                    if result_status == "error"
                    else StatusCode.OK
                )
            )
            span.end()

        self._logger.info(
            "Tool invocation outcome recorded",
            extra={
                "phase": "OUTCOME",
                "component": "tool",
                "correlation_id": correlation_id,
                "tool_name": tool.name,
                "actual_outcome": result_status,
                "latency_ms": latency_ms,
                "sanitized_result_keys": (
                    sorted(result.keys()) if isinstance(result, dict) else []
                ),
            },
        )
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict[str, Any] | None:
        del tool_args
        correlation_id = str(
            tool_context.state.get("temp:last_tool_correlation_id", "unknown")
        )
        span_entry = self._active_spans.pop(f"tool:{correlation_id}", None)
        if span_entry is not None:
            span, _ = span_entry
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, str(error)))
            span.end()

        self._logger.error(
            "Tool invocation error outcome recorded; returning guided recovery payload",
            extra={
                "phase": "OUTCOME",
                "component": "tool",
                "correlation_id": correlation_id,
                "tool_name": tool.name,
                "actual_outcome": "guided_error_recovery",
                "error_type": type(error).__name__,
            },
        )
        return {
            "status": "error",
            "error_code": "UNEXPECTED_TOOL_EXECUTION_ERROR",
            "human_explanation": pii_redactor.scrub_text(str(error)),
            "llm_recovery_instructions": (
                f"Inspect the parameters passed to '{tool.name}', verify all "
                "required fields match the Pydantic JSON schema, and retry once "
                "with corrected arguments or explain the issue politely to the user."
            ),
        }
