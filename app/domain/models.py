from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4
from urllib.parse import urlsplit, urlunsplit
from pydantic import BaseModel, ConfigDict, Field
from pydantic import field_validator

def now() -> datetime: return datetime.now(timezone.utc)

class ModelConfig(BaseModel):
    model_id: str
    provider_type: str
    base_url: str = ""
    model_name: str
    enabled: bool = True
    timeout: float = 120
    credential_ref: str | None = None
    @field_validator("timeout")
    @classmethod
    def timeout_positive(cls, value):
        if value <= 0: raise ValueError("timeout must be positive")
        return value

def safe_endpoint_identity(base_url: str) -> str:
    """Keep endpoint identity while removing userinfo and query credentials."""
    if not base_url:
        return ""
    parts = urlsplit(base_url)
    if not parts.scheme or not parts.hostname:
        return base_url.split("?", 1)[0].split("#", 1)[0]
    host = parts.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        port = ""
    return urlunsplit((parts.scheme, f"{host}{port}", parts.path, "", ""))

class ModelExecutionSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    model_id: str
    provider_type: str
    model_name: str
    base_url: str = ""
    timeout: float

    @classmethod
    def from_config(cls, config: ModelConfig) -> "ModelExecutionSnapshot":
        return cls(
            model_id=config.model_id,
            provider_type=config.provider_type,
            model_name=config.model_name,
            base_url=safe_endpoint_identity(config.base_url),
            timeout=config.timeout,
        )

class Role(StrEnum):
    PM = "pm"
    DEVELOPER = "developer"
    QA = "qa"

class Level(StrEnum):
    JUNIOR = "junior"
    SENIOR = "senior"
    LEAD = "lead"

class SkillVersion(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    version: str
    content: str
    checksum: str
    created_at: datetime = Field(default_factory=now)

class SkillProfile(BaseModel):
    name: str
    skills: tuple[str, ...]

class AgentState(BaseModel):
    mission_id: UUID | None = None
    mission_run_id: UUID | None = None
    agent_run_id: UUID | None = None
    role: Role = Role.PM
    level: Level = Level.JUNIOR
    profile: "SkillProfile | None" = None
    step: str = "start"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    handoffs: dict[str, Any] = Field(default_factory=dict)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    finished: bool = False
    allowed_tools: tuple[str, ...] = ()
    expected_output: str | None = None

class WorkspaceSnapshot(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    source_workspace: str
    location: str
    created_at: datetime = Field(default_factory=now)

class CheckpointState(BaseModel):
    mission_run_id: UUID
    current_agent_run_id: UUID | None = None
    current_step: str
    agent_state: AgentState
    handoffs: dict[str, Any] = Field(default_factory=dict)
    skill_versions: tuple[SkillVersion, ...] = ()
    workspace_snapshot_id: UUID

class ExecutionManifest(BaseModel):
    model_config = ConfigDict(frozen=True)
    mission_id: UUID
    mission_version: str
    employee_assignments: dict[Role, UUID]
    model_references: dict[UUID, dict[str, Any]]
    role_levels: dict[Role, Level]
    skill_versions: tuple[SkillVersion, ...]
    runtime_config: dict[str, Any]
    initial_workspace_snapshot_id: UUID
    model_snapshot: ModelExecutionSnapshot | None = None

class TraceEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID = Field(default_factory=uuid4)
    mission_id: UUID
    mission_run_id: UUID
    agent_run_id: UUID | None = None
    sequence: int
    event_type: str
    timestamp: datetime = Field(default_factory=now)
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

class MissionRunResult(BaseModel):
    mission_run_id: UUID
    mission_id: UUID
    status: str
    retry_count: int = 0
    execution_manifest: ExecutionManifest
    pm_agent_run_id: UUID
    developer_agent_run_ids: list[UUID] = Field(default_factory=list)
    qa_agent_run_ids: list[UUID] = Field(default_factory=list)
    final_qa_result: dict[str, Any]
    changed_files: list[str] = Field(default_factory=list)
    tool_call_count: int = 0
    event_count: int = 0
    workspace_reference: str
    checkpoint_ids: list[UUID] = Field(default_factory=list)
