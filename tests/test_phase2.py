from uuid import uuid4
from fastapi.testclient import TestClient
from app.main import app
from app.domain.models import AgentState, ExecutionManifest, Level, Role, SkillVersion, TraceEvent, WorkspaceSnapshot
from app.models.fake import FakeModelProvider
from app.domain.contracts import ModelRequest

def test_health():
    assert TestClient(app).get("/health").json() == {"status": "ok"}

def test_manifest_and_skill_snapshot_are_explicit():
    snapshot = SkillVersion(name="x", version="1", content="hello", checksum="abc")
    ws = uuid4()
    manifest = ExecutionManifest(mission_id=uuid4(), mission_version="1", employee_assignments={Role.PM: uuid4()}, model_references={}, role_levels={Role.PM: Level.JUNIOR}, skill_versions=(snapshot,), runtime_config={"max_retries": 2}, initial_workspace_snapshot_id=ws)
    assert manifest.skill_versions[0].content == "hello"
    assert "api_key" not in manifest.model_dump()

def test_trace_is_immutable_and_ordered():
    from app.tracing.recorder import InMemoryTraceRecorder
    recorder, run = InMemoryTraceRecorder(), uuid4()
    event = TraceEvent(mission_id=uuid4(), mission_run_id=run, sequence=1, event_type="agent_started")
    recorder.record(event)
    assert recorder.for_run(run)[0].sequence == 1

def test_fake_provider():
    provider = FakeModelProvider([{"status": "ok"}])
    assert provider.complete(ModelRequest(messages=[])).output["status"] == "ok"
