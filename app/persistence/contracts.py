from typing import Protocol
from uuid import UUID

from app.domain.models import (
    CheckpointState,
    MissionRecord,
    MissionRunResult,
    TraceEvent,
    WorkspaceSnapshot,
)


class MissionRepository(Protocol):
    def save_mission(self, mission: MissionRecord) -> None: ...
    def get_mission(self, mission_id: UUID) -> MissionRecord | None: ...


class RunRepository(Protocol):
    def save_run(self, result: MissionRunResult) -> None: ...
    def get_run(self, run_id: UUID) -> MissionRunResult | None: ...


class EventStore(Protocol):
    def append_events(self, events: list[TraceEvent]) -> None: ...
    def list_events(self, run_id: UUID) -> list[TraceEvent]: ...


class CheckpointStore(Protocol):
    def save_checkpoint(self, checkpoint_id: UUID, state: CheckpointState) -> None: ...
    def get_checkpoint(self, checkpoint_id: UUID) -> CheckpointState | None: ...


class WorkspaceSnapshotStore(Protocol):
    def save_workspace_snapshot(self, snapshot: WorkspaceSnapshot) -> None: ...
    def get_workspace_snapshot(self, snapshot_id: UUID) -> WorkspaceSnapshot | None: ...
