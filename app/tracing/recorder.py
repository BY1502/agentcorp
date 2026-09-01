from uuid import UUID
from app.domain.models import TraceEvent

class InMemoryTraceRecorder:
    def __init__(self): self.events: list[TraceEvent] = []
    def record(self, event: TraceEvent) -> TraceEvent:
        expected = len([e for e in self.events if e.mission_run_id == event.mission_run_id]) + 1
        if event.sequence != expected: raise ValueError(f"expected sequence {expected}")
        self.events.append(event)
        return event
    def for_run(self, run_id: UUID) -> list[TraceEvent]: return [e for e in self.events if e.mission_run_id == run_id]
