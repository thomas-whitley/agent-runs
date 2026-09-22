"""JSON to stdout, with trace and span ids carried from whatever span is current."""

import json
import logging
from datetime import datetime

from app.logging_setup import configure_logging


def test_a_log_line_is_one_json_object_with_the_core_fields(capsys):
    configure_logging()
    logger = logging.getLogger("agent_runs.test")

    logger.info("hello", extra={"run_id": "abc123"})

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["level"] == "INFO"
    assert payload["logger"] == "agent_runs.test"
    assert payload["message"] == "hello"
    assert payload["run_id"] == "abc123"
    datetime.fromisoformat(payload["timestamp"])  # raises if not a real timestamp


def test_a_log_line_carries_no_trace_or_span_id_outside_a_span(capsys):
    configure_logging()
    logger = logging.getLogger("agent_runs.test")

    logger.info("no span here")

    payload = json.loads(capsys.readouterr().out.strip())
    assert "trace_id" not in payload
    assert "span_id" not in payload


def test_a_log_line_inside_a_span_carries_its_trace_and_span_ids(capsys):
    from opentelemetry.sdk.trace import TracerProvider

    configure_logging()
    logger = logging.getLogger("agent_runs.test")
    tracer = TracerProvider().get_tracer("test")

    with tracer.start_as_current_span("work") as span:
        logger.info("inside a span")
        expected_trace_id = format(span.get_span_context().trace_id, "032x")
        expected_span_id = format(span.get_span_context().span_id, "016x")

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["trace_id"] == expected_trace_id
    assert payload["span_id"] == expected_span_id
