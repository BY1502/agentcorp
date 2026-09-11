from pathlib import Path
from typing import Any, Protocol
from uuid import UUID
from pydantic import BaseModel
from .models import AgentState, CheckpointState, ModelConfig, SkillProfile, SkillVersion, TraceEvent, WorkspaceSnapshot
from .policy import PendingApproval, PolicyDecision

class ModelRequest(BaseModel):
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = []
    role: str = ""
    expected_output: str = ""
    metadata: dict[str, Any] = {}
    # Provider-neutral JSON Schema. Provider adapters decide how to transport it.
    response_schema: dict[str, Any] | None = None

class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = {}

class ModelResponse(BaseModel):
    kind: str = "final"
    output: dict[str, Any] = {}
    tool_call: ToolCall | None = None
    usage: dict[str, Any] = {}
    latency_ms: float | None = None

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

class PolicyEvaluatorContract(Protocol):
    def evaluate(self, role: str, tool_call: ToolCall) -> PolicyDecision: ...

class ModelConfigResolver(Protocol):
    def resolve(self, model_id: str | None = None) -> ModelConfig: ...

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

class ApprovalStore(Protocol):
    def save_approval(self, approval: PendingApproval) -> None: ...
    def get_approval(self, approval_id: UUID) -> PendingApproval | None: ...
    def list_approvals(self, run_id: UUID) -> list[PendingApproval]: ...

class AgentRuntime(Protocol):
    def run(self, agent_run_id: UUID, state: AgentState) -> AgentState: ...

class MissionOrchestrator(Protocol):
    def run(self, mission_id: UUID, manifest: Any) -> UUID: ...
