"""Unit tests for Category 4 (Observability & PII) and Category 5 (Secret Hygiene)."""

from __future__ import annotations

from pathlib import Path
import re

from resolutions_agent.observability import PiiRedactor
from resolutions_agent.observability import in_memory_span_exporter
from resolutions_agent.observability import otel_tracer
from resolutions_agent.secrets import SecretManagerProvider


def test_pii_redactor_scrubs_emails_phones_ssns_and_secrets() -> None:
    redactor = PiiRedactor(enable_cloud_dlp=False)
    dummy_oauth = "ya29." + "dummy_redaction_fixture"
    raw = (
        "Reach me at alex.guest@example.com or +1 (415) 555-0199; "
        f"SSN 123-45-6789; token Bearer {dummy_oauth}"
    )
    scrubbed = redactor.scrub_text(raw)
    assert "alex.guest@example.com" not in scrubbed
    assert "555-0199" not in scrubbed
    assert "123-45-6789" not in scrubbed
    assert "dummy_redaction_fixture" not in scrubbed
    assert "[REDACTED_EMAIL]" in scrubbed
    assert "[REDACTED_PHONE]" in scrubbed
    assert "[REDACTED_SSN]" in scrubbed

    nested = redactor.scrub_payload(
        {"guest": "Sam (sam@example.org)", "api_key": "should-never-appear"}
    )
    assert nested["api_key"] == "[REDACTED_SECRET]"
    assert "[REDACTED_EMAIL]" in nested["guest"]


def test_opentelemetry_spans_recorded_in_exporter() -> None:
    in_memory_span_exporter.clear()
    with otel_tracer.start_as_current_span("unit_test_parent_span") as parent:
        parent.set_attribute("test.suite", "observability")
        with otel_tracer.start_as_current_span("unit_test_child_span"):
            pass

    spans = in_memory_span_exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert "unit_test_parent_span" in names
    assert "unit_test_child_span" in names


def test_no_hardcoded_api_keys_in_repository() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    forbidden_key_re = re.compile(r"AIza[0-9A-Za-z\-_]{35}")
    for py_file in repo_root.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        assert not forbidden_key_re.search(content), f"Leak found in {py_file}"

    provider = SecretManagerProvider(project_id="")
    token = provider.create_hitl_approval_token("sess-1", "man-1")
    assert len(token) == 24
