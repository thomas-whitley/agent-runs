"""OpenTelemetry, exported to Azure Monitor when there is a connection string.

Locally and in CI there is none, and tracing stays off. Nothing else in the
service knows or cares which it is.
"""

import logging
import os

logger = logging.getLogger("agent_runs.telemetry")

CONNECTION_STRING_VARIABLE = "APPLICATIONINSIGHTS_CONNECTION_STRING"


def configure_telemetry(app=None) -> bool:
    """Turn on tracing if configured. Returns whether it was turned on."""
    connection_string = os.environ.get(CONNECTION_STRING_VARIABLE)
    if not connection_string:
        return False

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        logger.warning("tracing is configured but the exporter is not installed")
        return False

    configure_azure_monitor(connection_string=connection_string)
    if app is not None:
        FastAPIInstrumentor.instrument_app(app)

    logger.info("tracing on, exporting to Azure Monitor")
    return True
