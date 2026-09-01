from pathlib import Path
from typing import Any, Protocol
from uuid import UUID
from pydantic import BaseModel
from .models import AgentState, CheckpointState, SkillProfile, SkillVersion, TraceEvent, WorkspaceSnapshot

class ModelRequest(BaseModel):
    messages: list[dict[str, Any]]
    response_schema: str | None = None

class ModelResponse(BaseModel):
    output: dict[str, Any]
    usage: dict[str, Any] = {}
    latency_ms: float | None = None

class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = {}

class ToolResult(BaseModel):
    success: bool
    output: str = ""
    error: str | None = None
    metadata: dict[str, Any] = {}

class CompiledPrompt(BaseModel):
    messages: list[dict[str, str]]
    skill_checksums: tuple[str, ...] = ()

class ModelProvider(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse: ...

class SkillLoader(Protocol):
    def load(self, name: str, version: str | None = None) -> SkillVersion: ...
    def snapshot(self, names: list[str]) -> tuple[SkillVersion, ...]: ...

class PromptCompiler(Protocol):
    def compile(self, context: dict[str, Any], profile: SkillProfile) -> CompiledPrompt: ...

class TraceRecorder(Protocol):
    def record(self, event: TraceEvent) -> TraceEvent: ...

class CheckpointManager(Protocol):
    def create(self, state: CheckpointState) -> UUID: ...
    def restore(self, checkpoint_id: UUID) -> CheckpointState: ...

class WorkspaceSnapshotManager(Protocol):
    def create(self, workspace: Path) -> WorkspaceSnapshot: ...
    def restore(self, snapshot_id: UUID, destination: Path) -> Path: ...

class AgentRuntime(Protocol):
    def run(self, agent_run_id: UUID, state: AgentState) -> AgentState: ...

class MissionOrchestrator(Protocol):
    def run(self, mission_id: UUID, manifest: Any) -> UUID: ...
