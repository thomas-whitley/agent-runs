"""Structured JSON logs to stdout, for every process.

Deliberately not routed through the telemetry exporter: logs must keep
working in CI, in compose, and on a machine with no exporter configured, and
must never go dark just because tracing is off. Never log a task's input body
or a model's output here or at any call site.
"""

import json
import logging
import sys
from datetime import UTC, datetime

from opentelemetry import trace

_RESERVED_FIELDS = set(vars(logging.makeLogRecord({})))


class JSONFormatter(logging.Formatter):
    """One log line, one JSON object. Trace and span ids come from whatever
    span is current when the line is emitted; extra fields passed to the
    logging call (run id, worker id, step sequence, ...) ride along as is."""

    def format(self, record: logging.LogRecord) -> str:
        # time.strftime, which the base class's formatTime uses, has no %f
        # for microseconds; datetime.isoformat does.
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            payload["trace_id"] = format(span_context.trace_id, "032x")
            payload["span_id"] = format(span_context.span_id, "016x")

        for key, value in vars(record).items():
            if key not in _RESERVED_FIELDS and key not in payload:
                payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Every process calls this once, first thing. Without it, a log call
    below the default WARNING level is silently dropped rather than emitted."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
