"""Tracing is configured from the environment, and its absence is not an error."""

import re

from app.telemetry import configure_telemetry, start_run_trace, step_span

TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


def test_without_a_connection_string_tracing_is_off(monkeypatch):
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)

    assert configure_telemetry(app=None) is False


def test_an_empty_connection_string_counts_as_off(monkeypatch):
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "")

    assert configure_telemetry(app=None) is False


def test_the_app_starts_with_tracing_off(start_server, monkeypatch):
    """The local stack has no Azure Monitor, and must not care."""
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    import httpx2

    base_url = start_server()

    assert httpx2.get(f"{base_url}/health").status_code == 200


def test_instrumenting_at_construction_puts_otel_middleware_in_the_stack(monkeypatch):
    """The bug that survived before: instrumenting from the lifespan patches a
    method Starlette has already called, and nothing raises to say so. Azure
    Monitor's own setup is stubbed out; only the middleware placement is real."""
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=00000000-0000-0000-0000-000000000000",
    )
    monkeypatch.setattr("azure.monitor.opentelemetry.configure_azure_monitor", lambda **_: None)
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

    from app.main import create_app

    app = create_app()

    stack = app.build_middleware_stack()

    assert isinstance(stack.app, OpenTelemetryMiddleware)


def test_start_run_trace_returns_none_when_tracing_is_off():
    """The ambient tracer is a no-op until configure_telemetry turns one on."""
    assert start_run_trace() is None


def test_start_run_trace_returns_a_valid_traceparent(in_memory_tracer):
    trace_context = start_run_trace(tracer=in_memory_tracer)

    assert TRACEPARENT_RE.match(trace_context)


def test_step_span_is_a_child_of_the_stored_trace_context(in_memory_tracer, span_exporter):
    trace_context = start_run_trace(tracer=in_memory_tracer)

    with step_span(trace_context, "plan", tracer=in_memory_tracer):
        pass

    spans = span_exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "run")
    step = next(s for s in spans if s.name == "step.plan")

    assert step.context.trace_id == root.context.trace_id
    assert step.parent.span_id == root.context.span_id
