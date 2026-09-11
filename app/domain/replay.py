from uuid import UUID

from pydantic import BaseModel, Field

from .models import ExecutionManifest


class WorkspaceFileInspection(BaseModel):
    path: str
    size: int
    sha256: str


class WorkspaceSnapshotInspection(BaseModel):
    snapshot_id: UUID
    files: list[WorkspaceFileInspection] = Field(default_factory=list)


class CheckpointInspection(BaseModel):
    checkpoint_id: UUID
    event_sequence: int | None = None
    current_step: str | None = None
    current_agent_run_id: UUID | None = None
    workspace_snapshot_id: UUID | None = None
    workspace_snapshot: WorkspaceSnapshotInspection | None = None


class ReplayTimelineItem(BaseModel):
    sequence: int
    event_type: str
    agent_run_id: UUID | None = None
    role: str | None = None
    tool_name: str | None = None
    success: bool | None = None
    exit_code: int | None = None
    recovery_attempt: int | None = None
    status: str | None = None
    summary: str


class ToolInspection(BaseModel):
    sequence: int
    tool_name: str
    agent_run_id: UUID | None = None
    role: str | None = None
    recovery_attempt: int | None = None
    success: bool | None = None
    exit_code: int | None = None


class ReplayIntegrity(BaseModel):
    event_sequence_valid: bool
    checkpoint_order_valid: bool
    manifest_present: bool
    final_event_present: bool
    status_consistent: bool


class ReplayInspection(BaseModel):
    run_id: UUID
    mission_id: UUID
    final_status: str
    resumed_from_run_id: UUID | None = None
    resumed_from_checkpoint_id: UUID | None = None
    execution_manifest: ExecutionManifest
    timeline: list[ReplayTimelineItem] = Field(default_factory=list)
    agent_summary: dict[str, dict[str, int]] = Field(default_factory=dict)
    tool_summary: list[ToolInspection] = Field(default_factory=list)
    checkpoints: list[CheckpointInspection] = Field(default_factory=list)
    recovery_summary: dict[str, object] = Field(default_factory=dict)
    evidence_summary: dict[str, object] = Field(default_factory=dict)
    final_result: dict[str, object] = Field(default_factory=dict)
    integrity: ReplayIntegrity
    consistency_issues: list[str] = Field(default_factory=list)
