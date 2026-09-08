from pathlib import Path
from uuid import uuid4

from app.checkpoints.local import InMemoryCheckpointManager, LocalWorkspaceSnapshotManager
from app.domain.models import ExecutionManifest, Level, ModelConfig, Role
from app.models.fake import FakeModelProvider
from app.models.lmstudio import ProviderError
from app.models.registry import ModelConfigRegistry
from app.runtime.orchestrator import BasicMissionOrchestrator
from app.services.run import RunService
from app.services.store import MissionRecord
from app.skills.filesystem import DeterministicPromptCompiler, FilesystemSkillLoader
from app.tools.filesystem import WorkspaceTools
from app.tracing.recorder import InMemoryTraceRecorder, sanitize


class RecordingFakeProvider(FakeModelProvider):
    def __init__(self, responses):
        super().__init__(responses)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return super().complete(request)


def manifest(max_recovery_attempts=1):
    loader = FilesystemSkillLoader(Path("skills"))
    return ExecutionManifest(
        mission_id=uuid4(),
        mission_version="1",
        employee_assignments={role: uuid4() for role in Role},
        model_references={},
        role_levels={role: Level.SENIOR for role in Role},
        skill_versions=loader.snapshot(["common/tool_usage.md"]),
        runtime_config={"max_recovery_attempts": max_recovery_attempts},
        initial_workspace_snapshot_id=uuid4(),
        model_snapshot=None,
    )


def run(responses, tmp_path, max_recovery_attempts=1, with_checkpoints=False):
    provider = RecordingFakeProvider(responses)
    recorder = InMemoryTraceRecorder()
    run_manifest = manifest(max_recovery_attempts)
    snapshot_manager = LocalWorkspaceSnapshotManager(tmp_path) if with_checkpoints else None
    checkpoint_manager = InMemoryCheckpointManager(snapshot_manager) if snapshot_manager else None
    result = BasicMissionOrchestrator(
        provider,
        DeterministicPromptCompiler(FilesystemSkillLoader(Path("skills"))),
        WorkspaceTools,
        recorder,
        snapshot_manager,
        checkpoint_manager,
    ).run(
        run_manifest.mission_id,
        run_manifest,
        Path("missions/demo_auth_bug/repo"),
        tmp_path / "runs",
    )
    return result, provider, recorder.for_run(result.mission_run_id)


def test_recovery_success_uses_failure_evidence_and_same_workspace(tmp_path):
    responses = [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix auth"}}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry < current_time", "new_text": "return expiry == current_time"}},
        {"output": {"status": "completed", "summary": "attempted"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed", "issues": ["expiry behavior is incorrect"]}},
        {"kind": "tool", "name": "read_file", "arguments": {"path": "app/auth.py"}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry == current_time", "new_text": "return expiry > current_time"}},
        {"output": {"status": "completed", "summary": "reworked"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "passed", "passed": 2, "failed": 0}},
    ]
    result, provider, events = run(responses, tmp_path)

    assert result.status == "PASSED"
    assert result.recovery_count == 1
    assert result.retry_count == 1
    assert len(result.developer_agent_run_ids) == 2
    assert len(result.qa_agent_run_ids) == 2
    agent_counts = {
        role: len({request.metadata["agent_run_id"] for request in provider.requests if request.role == role})
        for role in ("pm", "developer", "qa")
    }
    assert agent_counts == {"pm": 1, "developer": 2, "qa": 2}
    assert "return expiry > current_time" in (Path(result.workspace_reference) / "app/auth.py").read_text()
    assert provider.requests[5].messages[-1]["content"].find("recovery_attempt") >= 0
    assert "exit_code" in provider.requests[5].messages[-1]["content"]
    assert [event.event_type for event in events].count("recovery_started") == 1
    assert not any(event.event_type == "recovery_exhausted" for event in events)


def test_recovery_exhaustion_is_bounded(tmp_path):
    responses = [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}},
        {"output": {"status": "completed", "summary": "attempted"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed"}},
        {"output": {"status": "completed", "summary": "reworked"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed"}},
    ]
    result, provider, events = run(responses, tmp_path, max_recovery_attempts=1)

    assert result.status == "FAILED"
    assert result.recovery_count == 1
    assert len(result.developer_agent_run_ids) == 2
    assert len(result.qa_agent_run_ids) == 2
    assert len(provider.requests) == 7
    exhausted = next(event for event in events if event.event_type == "recovery_exhausted")
    assert exhausted.payload["recovery_attempt"] == 1
    assert exhausted.payload["max_recovery_attempts"] == 1


def test_runtime_failure_does_not_start_qa_recovery(tmp_path):
    class BrokenDeveloperProvider:
        def __init__(self):
            self.calls = 0

        def complete(self, request):
            self.calls += 1
            if self.calls == 1:
                return FakeModelProvider([{"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}}]).complete(request)
            raise ProviderError("timeout_error", "timeout")

    recorder = InMemoryTraceRecorder()
    provider = BrokenDeveloperProvider()
    run_manifest = manifest(1)
    result = BasicMissionOrchestrator(
        provider,
        DeterministicPromptCompiler(FilesystemSkillLoader(Path("skills"))),
        WorkspaceTools,
        recorder,
    ).run(run_manifest.mission_id, run_manifest, Path("missions/demo_auth_bug/repo"), tmp_path / "runs")
    events = recorder.for_run(result.mission_run_id)

    assert result.status == "FAILED"
    assert result.recovery_count == 0
    assert not any(event.event_type == "recovery_started" for event in events)
    assert any(event.event_type == "runtime_error" for event in events)


def test_recovery_events_keep_evidence_bounded_and_non_reasoning(tmp_path):
    responses = [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}},
        {"output": {"status": "completed", "summary": "attempted"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed"}},
        {"output": {"status": "completed", "summary": "reworked"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed"}},
    ]
    result, _, events = run(responses, tmp_path, max_recovery_attempts=1)
    recovery = next(event for event in events if event.event_type == "recovery_started")
    serialized = str(recovery.payload)

    assert "stdout" not in recovery.payload["failure_evidence"][0]
    assert "stderr" not in recovery.payload["failure_evidence"][0]
    assert "reasoning_content" not in serialized
    assert result.final_qa_result["status"] == "failed"


def test_recovery_evidence_redacts_secret_text():
    stored = sanitize({"stdout": "api_key=hidden authorization=Bearer-token"})

    assert "hidden" not in stored["stdout"]
    assert "Bearer-token" not in stored["stdout"]


def test_recovery_checkpoint_policy_tracks_safe_boundaries(tmp_path):
    responses = [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry < current_time", "new_text": "return expiry == current_time"}},
        {"output": {"status": "completed", "summary": "attempted"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed"}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry == current_time", "new_text": "return expiry > current_time"}},
        {"output": {"status": "completed", "summary": "reworked"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "passed", "passed": 2, "failed": 0}},
    ]
    result, _, events = run(responses, tmp_path, with_checkpoints=True)

    assert result.status == "PASSED"
    assert len(result.checkpoint_ids) == 5
    assert [event.payload.get("reason") for event in events if event.event_type == "checkpoint_created"] == [
        "pm_handoff",
        "successful_edit_file",
        "developer_handoff",
        "successful_edit_file",
        "developer_handoff",
    ]


def test_recovery_does_not_accept_test_deletion_as_success(tmp_path):
    test_source = (Path("missions/demo_auth_bug/repo") / "tests/test_auth.py").read_text()
    responses = [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "tests/test_auth.py", "old_text": test_source, "new_text": ""}},
        {"output": {"status": "completed", "summary": "changed"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "passed", "passed": 0, "failed": 0}},
    ]
    result, _, events = run(responses, tmp_path)

    assert result.status == "FAILED"
    assert result.recovery_count == 0
    assert any(event.event_type == "validation_error" for event in events)


def test_qa_failed_without_failed_test_evidence_does_not_recover(tmp_path):
    responses = [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry < current_time", "new_text": "return expiry > current_time"}},
        {"output": {"status": "completed", "summary": "attempted"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed", "issues": ["model disagreement"]}},
    ]
    result, _, events = run(responses, tmp_path)

    assert result.status == "FAILED"
    assert result.recovery_count == 0
    assert not any(event.event_type == "recovery_started" for event in events)
    assert any(event.event_type == "validation_error" for event in events)


def test_recovery_keeps_one_frozen_provider_identity(tmp_path):
    responses = [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}},
        {"output": {"status": "completed", "summary": "attempted"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed"}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry < current_time", "new_text": "return expiry > current_time"}},
        {"output": {"status": "completed", "summary": "reworked"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "passed", "passed": 2, "failed": 0}},
    ]
    registry = ModelConfigRegistry(
        [ModelConfig(model_id="selected", provider_type="fake", model_name="fake-v1")],
        default_model_id="selected",
    )

    class MutatingProvider(RecordingFakeProvider):
        def complete(self, request):
            response = super().complete(request)
            if len(self.requests) == 4:
                registry.register(ModelConfig(model_id="selected", provider_type="fake", model_name="fake-v2"))
            return response

    provider = MutatingProvider(responses)

    class Factory:
        def __init__(self):
            self.calls = 0

        def create(self, config):
            self.calls += 1
            return provider

    factory = Factory()
    service = RunService(registry=registry, provider_factory=factory, workspace_root=tmp_path / "workspaces", skills_root=Path("skills"))
    result = service.start(MissionRecord("Fix auth expiry", "missions/demo_auth_bug/repo"), "selected")

    assert result.status == "PASSED"
    assert factory.calls == 1
    assert result.execution_manifest.model_snapshot.model_name == "fake-v1"
    assert registry.resolve("selected").model_name == "fake-v2"
