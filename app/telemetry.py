"""OpenTelemetry, exported to Azure Monitor when there is a connection string.

Locally and in CI there is none, and tracing stays off. Nothing else in the
service knows or cares which it is.
"""

import logging
import os

from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger("agent_runs.telemetry")

CONNECTION_STRING_VARIABLE = "APPLICATIONINSIGHTS_CONNECTION_STRING"

# The ambient tracer: a no-op until configure_telemetry turns on a real
# provider, so callers never need to branch on whether tracing is on.
_tracer = trace.get_tracer("agent_runs")


def configure_telemetry(app=None) -> bool:
    """Turn on tracing if configured. Returns whether it was turned on.

    Must be called from create_app(), not from the lifespan. FastAPIInstrumentor
    works by patching build_middleware_stack, but Starlette calls that method
    itself the first time the app receives any ASGI scope, including the
    lifespan startup scope, which happens before our lifespan function's own
    body runs. Instrumenting from inside the lifespan therefore patches a
    method that has already been called, and nothing raises to say so.
    """
    connection_string = os.environ.get(CONNECTION_STRING_VARIABLE)
    if not connection_string:
        return False

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        logger.warning("tracing is configured but the exporter is not installed")
        return False

    # OTEL_SERVICE_NAME, set per role in the Bicep, is what keeps cloud_RoleName
    # from being unknown_service: Resource.create() reads it when no explicit
    # resource is passed.
    configure_azure_monitor(connection_string=connection_string)
    if app is not None:
        FastAPIInstrumentor.instrument_app(app)

    logger.info("tracing on, exporting to Azure Monitor")
    return True


def start_run_trace(tracer: trace.Tracer | None = None) -> str | None:
    """The run's root span, and the traceparent the worker restores from.

    The span itself does not stay open past this call; the API has no more
    work to do in it. What the worker needs is the trace id and span id it
    leaves behind, so its own step spans land as children of this one rather
    than starting a second, unrelated trace. None means tracing is off, and
    is stored as NULL rather than an empty string so the column keeps one
    meaning.
    """
    with (tracer or _tracer).start_as_current_span("run"):
        carrier: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(carrier)
    return carrier.get("traceparent")


def step_span(trace_context: str | None, kind: str, tracer: trace.Tracer | None = None):
    """A child span for one loop step, parented on the run's stored trace context.

    A run created before tracing existed, or with tracing off, has no stored
    context; the step then starts its own trace rather than raising.
    """
    parent = (
        TraceContextTextMapPropagator().extract(carrier={"traceparent": trace_context})
        if trace_context
        else None
    )
    return (tracer or _tracer).start_as_current_span(f"step.{kind}", context=parent)
