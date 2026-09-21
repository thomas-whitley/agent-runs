"""Minimal SSE reader, used by the streaming tests."""

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class SseEvent:
    id: int
    event: str
    data: dict


def read_sse(lines: Iterable[str], limit: int | None = None) -> Iterator[SseEvent]:
    """Yield parsed events. Comment lines such as ": keepalive" are skipped."""
    fields: dict[str, str] = {}
    seen = 0

    for line in lines:
        if line.startswith(":"):
            continue
        if line == "":
            if fields:
                yield SseEvent(
                    id=int(fields["id"]),
                    event=fields.get("event", "message"),
                    data=json.loads(fields["data"]),
                )
                fields = {}
                seen += 1
                if limit is not None and seen >= limit:
                    return
            continue
        name, _, value = line.partition(":")
        fields[name] = value[1:] if value.startswith(" ") else value
