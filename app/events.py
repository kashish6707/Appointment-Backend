"""Real-time events. Routes call emit() after committing; Socket.IO subscribes as a sink.

Rooms: "doctor:<doctorId>", "session:<sessionId>", "booking:<bookingId>".
"""

from collections.abc import Callable

Sink = Callable[[str, str, dict], None]
_sinks: list[Sink] = []


def subscribe(sink: Sink) -> Callable[[], None]:
    _sinks.append(sink)
    return lambda: _sinks.remove(sink)


def emit(room: str, event: str, payload: dict) -> None:
    for sink in list(_sinks):
        sink(room, event, payload)
