from typing import Any
from uuid import UUID
from pydantic import BaseModel
from app.domain.models import ModelExecutionSnapshot

class MissionCreate(BaseModel):
    title: str='Demo authentication mission'
    fixture: str='missions/demo_auth_bug/repo'
class RunCreate(BaseModel):
    model_id: str | None = None
class MissionResponse(BaseModel): id: UUID; title: str; version: str; fixture: str
class RunResponse(BaseModel):
    run_id: UUID
    mission_id: UUID
    status: str
    retry_count: int
    changed_files: list[str]
    tool_call_count: int
    event_count: int
    workspace_ref: str
    model_snapshot: ModelExecutionSnapshot | None = None
class EventResponse(BaseModel): sequence: int; event_type: str; mission_run_id: UUID; agent_run_id: UUID|None; timestamp: Any; payload: dict
