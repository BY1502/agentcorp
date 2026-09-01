from uuid import UUID
from app.domain.models import TraceEvent
from typing import Any

SENSITIVE = {"api_key","authorization","access_token","refresh_token","password","secret","credential"}
def sanitize(value: Any):
    if isinstance(value, dict): return {k: "[REDACTED]" if k.lower() in SENSITIVE else sanitize(v) for k,v in value.items()}
    if isinstance(value, list): return [sanitize(v) for v in value]
    if isinstance(value, tuple): return tuple(sanitize(v) for v in value)
    return value

class InMemoryTraceRecorder:
    def __init__(self): self.events: list[TraceEvent] = []
    def record(self, event: TraceEvent) -> TraceEvent:
        expected = len([e for e in self.events if e.mission_run_id == event.mission_run_id]) + 1
        if event.sequence != expected: raise ValueError(f"expected sequence {expected}")
        safe = event.model_copy(update={"payload": sanitize(event.payload), "metadata": sanitize(event.metadata)})
        self.events.append(safe)
        return safe
    def for_run(self, run_id: UUID) -> list[TraceEvent]: return [e for e in self.events if e.mission_run_id == run_id]
