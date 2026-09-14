import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.checkpoints.local import InMemoryCheckpointManager, LocalWorkspaceSnapshotManager
from app.domain.contracts import ModelRequest
from app.domain.models import AgentState, Level, ModelConfig, Role, SkillProfile
from app.domain.policy import ApprovalStatus
from app.models.fake import FakeModelProvider
from app.models.registry import ModelConfigRegistry
from app.persistence.sqlite import SQLiteStore
from app.runtime.agent import BasicAgentRuntime
from app.services.mission import MissionService
from app.services.replay import ReplayService
from app.services.run import ApprovalError, RunService
from app.services.store import AppStore
from app.skills.filesystem import DeterministicPromptCompiler, FilesystemSkillLoader
from app.tools.filesystem import WorkspaceTools
from app.tracing.recorder import InMemoryTraceRecorder


class SequenceFactory:
    def __init__(self, *providers):
        self.providers = list(providers)

    def create(self, config):
        return self.providers.pop(0)

    def create_from_snapshot(self, snapshot):
        return self.providers.pop(0)


def waiting_providers():
    initial = FakeModelProvider([
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry < current_time", "new_text": "return expiry > current_time"}},
    ])
    continuation = FakeModelProvider([
        {"output": {"status": "completed", "summary": "fixed"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "passed", "passed": 2, "failed": 0, "issues": []}},
    ])
    return initial, continuation


def make_service(tmp_path, *providers, **kwargs):
    storage = SQLiteStore(tmp_path / "hardening.db")
    mission = MissionService(storage).create("Fix auth", "missions/demo_auth_bug/repo")
    config = ModelConfig(model_id="selected", provider_type="fake", model_name="hardening")
    service = RunService(
        registry=ModelConfigRegistry((config,), "selected"),
        provider_factory=SequenceFactory(*providers),
        workspace_root=tmp_path / "workspaces",
        skills_root=Path("skills"),
        storage=storage,
        approval_mode="policy",
        **kwargs,
    )
    return storage, mission, service


def test_approval_timestamps_and_replay_audit_summary(tmp_path):
    initial, continuation = waiting_providers()
    storage, mission, service = make_service(tmp_path, initial, continuation)
    waiting = service.start(mission, "selected")
    pending = storage.list_approvals(waiting.mission_run_id)[0]

    assert pending.decided_at is None
    service.approve(pending.approval_id)
    approved = storage.get_approval(pending.approval_id)
    inspection = ReplayService(storage).inspect(waiting.mission_run_id)

    assert approved.status == ApprovalStatus.APPROVED
    assert approved.decided_at is not None
    assert approved.created_at <= approved.decided_at
    assert inspection.approval_summary == {
        "total": 1,
        "pending": 0,
        "approved": 1,
        "rejected": 0,
        "expired": 0,
        "integrity_valid": True,
    }
    assert inspection.integrity.approval_integrity_valid is True
    storage.close()


def test_policy_snapshot_and_approval_version_are_frozen(tmp_path):
    initial, continuation = waiting_providers()
    storage, mission, service = make_service(tmp_path, initial, continuation, policy_version="1")
    waiting = service.start(mission, "selected")
    pending = storage.list_approvals(waiting.mission_run_id)[0]

    service.policy_version = "2"
    service.policy_rules["edit_file"] = "allow"
    service.approve(pending.approval_id)

    final = storage.get_run(waiting.mission_run_id)
    approved = storage.get_approval(pending.approval_id)
    assert final.execution_manifest.policy_snapshot.policy_version == "1"
    assert dict(final.execution_manifest.policy_snapshot.rules) == {}
    assert approved.policy_version == "1"
    storage.close()


def test_stale_workspace_fails_safely_without_tool_execution(tmp_path):
    initial, continuation = waiting_providers()
    storage, mission, service = make_service(tmp_path, initial, continuation)
    waiting = service.start(mission, "selected")
    pending = storage.list_approvals(waiting.mission_run_id)[0]
    Path(waiting.workspace_reference, "app/auth.py").write_text("tampered")

    with pytest.raises(ApprovalError, match="workspace"):
        service.approve(pending.approval_id)

    events = storage.list_events(waiting.mission_run_id)
    assert storage.get_run(waiting.mission_run_id).status == "FAILED"
    assert storage.get_approval(pending.approval_id).status == ApprovalStatus.PENDING
    assert not any(event.event_type == "tool_result" for event in events)
    assert any(event.event_type == "approval_stale" for event in events)
    storage.close()


def test_snapshot_and_checkpoint_ownership_mismatch_is_rejected(tmp_path):
    initial, continuation = waiting_providers()
    storage, mission, service = make_service(tmp_path, initial, continuation)
    waiting = service.start(mission, "selected")
    pending = storage.list_approvals(waiting.mission_run_id)[0]
    checkpoint_id = waiting.checkpoint_ids[-1]
    checkpoint = storage.get_checkpoint(checkpoint_id)
    row = storage._connection.execute(
        "SELECT snapshot_json FROM workspace_snapshots WHERE id = ?", (str(checkpoint.workspace_snapshot_id),)
    ).fetchone()
    snapshot = json.loads(row["snapshot_json"])
    snapshot["mission_run_id"] = str(uuid4())
    storage._connection.execute(
        "UPDATE workspace_snapshots SET snapshot_json = ? WHERE id = ?",
        (json.dumps(snapshot), str(checkpoint.workspace_snapshot_id)),
    )
    storage._connection.commit()

    with pytest.raises(ApprovalError, match="ownership"):
        service.approve(pending.approval_id)
    assert storage.get_approval(pending.approval_id).status == ApprovalStatus.PENDING
    assert not any(event.event_type == "tool_result" for event in storage.list_events(waiting.mission_run_id))

    storage.close()


def test_invalid_policy_configuration_is_rejected(tmp_path):
    initial, _ = waiting_providers()
    storage, mission, service = make_service(tmp_path, initial)
    with pytest.raises(ValueError, match="approval mode"):
        service.start(mission, "selected", approval_mode="unknown")
    service.approval_ttl_seconds = -1
    with pytest.raises(ValueError, match="ttl"):
        service.start(mission, "selected")
    service.approval_ttl_seconds = None
    service.policy_version = "v2"
    with pytest.raises(ValueError, match="policy_version"):
        service.start(mission, "selected")
    storage.close()


def test_expired_approval_is_terminal_and_never_executes(tmp_path):
    initial, continuation = waiting_providers()
    storage, mission, service = make_service(tmp_path, initial, continuation, approval_ttl_seconds=0)
    waiting = service.start(mission, "selected")
    pending = storage.list_approvals(waiting.mission_run_id)[0]

    with pytest.raises(ApprovalError, match="decided"):
        service.approve(pending.approval_id)

    assert storage.get_approval(pending.approval_id).status == ApprovalStatus.EXPIRED
    assert storage.get_approval(pending.approval_id).decided_at is not None
    assert storage.get_run(waiting.mission_run_id).status == "FAILED"
    assert any(event.event_type == "approval_expired" for event in storage.list_events(waiting.mission_run_id))
    assert not any(event.event_type == "tool_result" for event in storage.list_events(waiting.mission_run_id))
    storage.close()


def test_policy_evaluator_failure_is_fail_closed():
    class BrokenPolicy:
        mode = "policy"
        policy_version = "1"

        def evaluate(self, role, tool_call):
            raise RuntimeError("broken evaluator")

    mission_id, run_id, agent_id = uuid4(), uuid4(), uuid4()
    state = AgentState(
        mission_id=mission_id,
        mission_run_id=run_id,
        agent_run_id=agent_id,
        role=Role.DEVELOPER,
        level=Level.SENIOR,
        profile=SkillProfile(name="developer", skills=("common/tool_usage.md",)),
        allowed_tools=("edit_file",),
        expected_output="DeveloperToQAHandoff",
    )
    recorder = InMemoryTraceRecorder()
    runtime = BasicAgentRuntime(
        FakeModelProvider([{"kind": "tool", "name": "edit_file", "arguments": {"path": "x", "old_text": "a", "new_text": "b"}}]),
        DeterministicPromptCompiler(FilesystemSkillLoader(Path("skills"))),
        WorkspaceTools(Path(".")),
        recorder,
        mission_id,
        run_id,
        policy_evaluator=BrokenPolicy(),
    )

    runtime.run(agent_id, state)
    assert not any(event.event_type == "tool_result" for event in recorder.events)
    assert any(event.payload.get("category") == "policy_evaluator_error" for event in recorder.events)


def test_approval_persistence_failure_is_fail_closed(tmp_path):
    class BrokenStore(AppStore):
        def save_approval(self, approval):
            raise OSError("storage unavailable")

    storage = BrokenStore()
    mission = MissionService(storage).create("Fix auth", "missions/demo_auth_bug/repo")
    config = ModelConfig(model_id="selected", provider_type="fake", model_name="hardening")
    service = RunService(
        registry=ModelConfigRegistry((config,), "selected"),
        provider_factory=SequenceFactory(*waiting_providers()[:1]),
        workspace_root=tmp_path / "workspaces",
        skills_root=Path("skills"),
        storage=storage,
        approval_mode="policy",
    )

    result = service.start(mission, "selected")
    events = storage.list_events(result.mission_run_id)
    assert result.status == "FAILED"
    assert any(event.event_type == "runtime_error" and event.payload.get("category") == "approval_persistence" for event in events)
    assert not any(event.event_type == "tool_result" for event in events)


def test_replay_flags_duplicate_approved_execution_evidence(tmp_path):
    initial, continuation = waiting_providers()
    storage, mission, service = make_service(tmp_path, initial, continuation)
    waiting = service.start(mission, "selected")
    pending = storage.list_approvals(waiting.mission_run_id)[0]
    service.approve(pending.approval_id)
    events = storage.list_events(waiting.mission_run_id)
    tool_result = next(event for event in events if event.event_type == "tool_result" and event.payload.get("approval_id"))
    duplicate = tool_result.model_copy(update={"id": uuid4(), "sequence": len(events) + 1})
    storage.append_events([duplicate])

    inspection = ReplayService(storage).inspect(waiting.mission_run_id)
    assert any(issue.startswith("approval_duplicate_tool_result") for issue in inspection.consistency_issues)
    assert inspection.integrity.approval_integrity_valid is False
    storage.close()
