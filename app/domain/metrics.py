from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class RunMetrics(BaseModel):
    run_id: UUID
    mission_id: UUID
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None
    agent_durations_ms: dict[str, float] = Field(default_factory=dict)
    model_id: str | None = None
    provider_type: str | None = None
    model_name: str | None = None
    model_call_count: int = 0
    tool_call_count: int = 0
    tool_success_count: int = 0
    tool_failure_count: int = 0
    tool_calls_by_name: dict[str, int] = Field(default_factory=dict)
    qa_run_test_count: int = 0
    qa_test_pass_count: int = 0
    qa_test_fail_count: int = 0
    qa_evidence_conflict_count: int = 0
    required_tool_correction_count: int = 0
    recovery_count: int = 0
    approval_count: int = 0
    approval_pending_count: int = 0
    approval_approved_count: int = 0
    approval_rejected_count: int = 0
    approval_expired_count: int = 0
    approval_wait_ms: float | None = None
    checkpoint_count: int = 0
    event_count: int = 0
    resumed_from_run_id: UUID | None = None
    resumed_from_checkpoint_id: UUID | None = None
    is_resumed_run: bool = False
    workspace_changed_file_count: int | None = None
    integrity_status: Literal["valid", "invalid", "unknown"] = "unknown"
