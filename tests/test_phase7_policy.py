from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.domain.contracts import ModelRequest
from app.domain.models import AgentState, Level, ModelConfig, Role
from app.domain.policy import PolicyAction, PolicyEvaluator, ToolCallSnapshot
from app.models.fake import FakeModelProvider
from app.models.factory import ProviderFactory
from app.models.registry import ModelConfigRegistry
from app.persistence.sqlite import SQLiteStore
from app.services.mission import MissionService
from app.services.replay import ReplayService
from app.services.run import ApprovalError, ResumeError, RunService
from app.tracing.recorder import InMemoryTraceRecorder


class SequencedFactory:
    def __init__(self, *providers):
        self.providers = list(providers)
        self.snapshot_calls = 0

    def create(self, config):
        return self.providers.pop(0)

    def create_from_snapshot(self, snapshot):
        self.snapshot_calls += 1
        return self.providers.pop(0)


def run_service(tmp_path, responses, *, mode="policy", rules=None, factory=None):
    storage = SQLiteStore(tmp_path / "policy.db")
    mission = MissionService(storage).create("Fix auth", "missions/demo_auth_bug/repo")
    factory = factory or ProviderFactory(fake_responses=responses)
    registry = ModelConfigRegistry(
        (ModelConfig(model_id="selected", provider_type="fake", model_name="policy-test"),),
        default_model_id="selected",
    )
    service = RunService(
        registry=registry,
        provider_factory=factory,
        workspace_root=tmp_path / "workspaces",
        skills_root=Path("skills"),
        storage=storage,
        approval_mode=mode,
        policy_rules=rules,
    )
    return storage, mission, service


def approval_responses():
    return [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix auth"}}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry < current_time", "new_text": "return expiry > current_time"}},
    ]


def test_policy_evaluator_is_deterministic_and_provider_free():
    evaluator = PolicyEvaluator(mode="policy")
    read = evaluator.evaluate(Role.DEVELOPER, type("Call", (), {"name": "read_file", "arguments": {}})())
    write = evaluator.evaluate(Role.DEVELOPER, type("Call", (), {"name": "edit_file", "arguments": {}})())
    deny = PolicyEvaluator(mode="policy", rules={"edit_file": "deny"}).evaluate(
        Role.DEVELOPER, type("Call", (), {"name": "edit_file", "arguments": {}})()
    )

    assert read.decision == PolicyAction.ALLOW
    assert write.decision == PolicyAction.REQUIRE_APPROVAL
    assert deny.decision == PolicyAction.DENY
    assert write.policy_id == "filesystem.write"


def test_compatibility_mode_keeps_edit_file_automatic(tmp_path):
    from app.services.run import default_fake_responses

    storage, mission, service = run_service(tmp_path, default_fake_responses(), mode="disabled")
    result = service.start(mission, "selected")

    assert result.status == "PASSED"
    assert storage.list_approvals(result.mission_run_id) == []
    storage.close()


def test_policy_allows_read_only_tool(tmp_path):
    responses = [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "inspect"}}},
        {"kind": "tool", "name": "read_file", "arguments": {"path": "app/auth.py"}},
        {"output": {"status": "completed", "summary": "inspected"}},
        {"output": {"status": "pending", "issues": ["no test evidence"]}},
    ]
    storage, mission, service = run_service(tmp_path, responses)
    result = service.start(mission, "selected")
    events = storage.list_events(result.mission_run_id)

    assert result.status == "FAILED"
    assert storage.list_approvals(result.mission_run_id) == []
    assert any(event.event_type == "tool_result" and event.payload["tool_name"] == "read_file" for event in events)
    storage.close()


def test_edit_file_requires_approval_without_mutating_workspace(tmp_path):
    storage, mission, service = run_service(tmp_path, approval_responses())
    result = service.start(mission, "selected")
    events = storage.list_events(result.mission_run_id)
    approvals = storage.list_approvals(result.mission_run_id)
    workspace = Path(result.workspace_reference)

    assert result.status == "WAITING_APPROVAL"
    assert result.changed_files == []
    assert (workspace / "app/auth.py").read_text() == "def is_token_valid(expiry, current_time):\n    return expiry < current_time\n"
    assert len(approvals) == 1
    assert approvals[0].agent_role == Role.DEVELOPER
    assert approvals[0].tool_call.tool_name == "edit_file"
    assert approvals[0].tool_call.arguments["path"] == "app/auth.py"
    assert approvals[0].tool_call.arguments_digest == ToolCallSnapshot.from_parts(
        "edit_file", Role.DEVELOPER, approvals[0].tool_call.arguments
    ).arguments_digest
    assert any(event.event_type == "approval_required" for event in events)
    assert not any(event.event_type == "tool_result" for event in events)
    assert not any(event.event_type == "mission_finished" for event in events)
    storage.close()


def test_waiting_approval_persists_replays_and_cannot_use_generic_resume(tmp_path, monkeypatch):
    storage, mission, service = run_service(tmp_path, approval_responses())
    result = service.start(mission, "selected")
    approval = storage.list_approvals(result.mission_run_id)[0]
    checkpoint_id = result.checkpoint_ids[-1]
    storage.close()

    reopened = SQLiteStore(tmp_path / "policy.db")
    restarted = RunService(
        registry=service.registry,
        provider_factory=ProviderFactory(fake_responses=[]),
        workspace_root=tmp_path / "workspaces",
        skills_root=Path("skills"),
        storage=reopened,
    )
    assert restarted.get(result.mission_run_id).status == "WAITING_APPROVAL"
    assert restarted.approval(approval.approval_id).status == "PENDING"
    inspection = ReplayService(reopened).inspect(result.mission_run_id)
    assert inspection.approvals[0].approval_id == approval.approval_id
    assert inspection.integrity.status_consistent is True
    assert inspection.integrity.final_event_present is False
    with pytest.raises(ResumeError):
        restarted.resume(result.mission_run_id, checkpoint_id)

    monkeypatch.setattr(main_module, "runs", restarted)
    client = TestClient(main_module.app)
    assert client.get(f"/runs/{result.mission_run_id}/approvals").status_code == 200
    assert client.get(f"/approvals/{approval.approval_id}").status_code == 200
    assert client.post(f"/runs/{result.mission_run_id}/resume", json={"checkpoint_id": str(checkpoint_id)}).status_code == 409
    reopened.close()


def test_deny_stops_before_tool_executor(tmp_path):
    responses = [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "test"}}},
        {"output": {"status": "completed", "summary": "ready"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
    ]
    storage, mission, service = run_service(tmp_path, responses, rules={"run_test": "deny"})
    result = service.start(mission, "selected")
    events = storage.list_events(result.mission_run_id)

    assert result.status == "FAILED"
    assert not any(event.event_type == "tool_result" for event in events)
    denied = next(event for event in events if event.event_type == "validation_error")
    assert denied.payload["reason"] == "policy_denied"
    assert storage.list_approvals(result.mission_run_id) == []
    storage.close()


def test_permission_validation_runs_before_policy():
    class SpyPolicy:
        mode = "policy"
        calls = 0

        def evaluate(self, role, tool_call):
            self.calls += 1
            raise AssertionError("policy must not run after permission failure")

    from app.runtime.agent import BasicAgentRuntime
    from app.skills.filesystem import DeterministicPromptCompiler, FilesystemSkillLoader
    from app.tools.filesystem import WorkspaceTools
    from uuid import uuid4

    recorder = InMemoryTraceRecorder()
    provider = FakeModelProvider([{"kind": "tool", "name": "edit_file", "arguments": {}}])
    state = AgentState(
        mission_id=uuid4(), mission_run_id=uuid4(), agent_run_id=uuid4(), role=Role.QA,
        level=Level.SENIOR, expected_output="QAResult", allowed_tools=("run_test",),
    )
    state.profile = __import__("app.domain.models", fromlist=["SkillProfile"]).SkillProfile(name="qa", skills=("common/tool_usage.md",))
    runtime = BasicAgentRuntime(provider, DeterministicPromptCompiler(FilesystemSkillLoader(Path("skills"))), WorkspaceTools(Path(".")), recorder, state.mission_id, state.mission_run_id, policy_evaluator=SpyPolicy())
    runtime.run(state.agent_run_id, state)

    assert runtime.policy_evaluator.calls == 0
    assert not any(event.event_type == "tool_result" for event in recorder.events)


def test_recovery_developer_also_hits_policy_without_spending_extra_budget(tmp_path):
    responses = [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}},
        {"output": {"status": "completed", "summary": "attempted"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed", "issues": ["broken"]}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry < current_time", "new_text": "return expiry > current_time"}},
    ]
    storage, mission, service = run_service(tmp_path, responses)
    result = service.start(mission, "selected")
    pending = storage.list_approvals(result.mission_run_id)[0]
    checkpoint = storage.get_checkpoint(result.checkpoint_ids[-1])

    assert result.status == "WAITING_APPROVAL"
    assert result.recovery_count == 1
    assert checkpoint.agent_state.recovery_attempt == 1
    assert pending.agent_role == Role.DEVELOPER
    assert checkpoint.agent_state.pending_approval_id == pending.approval_id
    storage.close()


def test_resumed_run_uses_frozen_policy_snapshot(tmp_path):
    parent_provider = FakeModelProvider([
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}},
        {"output": {"status": "completed", "summary": "ready"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
    ])
    resumed_provider = FakeModelProvider([
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
    ])
    factory = SequencedFactory(parent_provider, resumed_provider)
    storage, mission, service = run_service(tmp_path, [], rules={"run_test": "deny"}, factory=factory)
    parent = service.start(mission, "selected")
    dev_checkpoint = next(
        checkpoint_id for checkpoint_id in parent.checkpoint_ids
        if storage.get_checkpoint(checkpoint_id).agent_state.role == Role.DEVELOPER
        and storage.get_checkpoint(checkpoint_id).agent_state.finished
    )
    service.approval_mode = "disabled"

    resumed = service.resume(parent.mission_run_id, dev_checkpoint)
    events = storage.list_events(resumed.mission_run_id)

    assert resumed.status == "FAILED"
    assert resumed.resumed_from_run_id == parent.mission_run_id
    assert any(event.event_type == "validation_error" and event.payload["reason"] == "policy_denied" for event in events)
    assert factory.snapshot_calls == 1
    storage.close()


def test_secret_arguments_are_denied_and_never_persisted(tmp_path):
    evaluator = PolicyEvaluator(mode="policy")
    call = type("Call", (), {"name": "edit_file", "arguments": {"path": "x", "new_text": "api_key=marker"}})()
    assert evaluator.evaluate(Role.DEVELOPER, call).decision == PolicyAction.DENY
    with pytest.raises(ValueError):
        ToolCallSnapshot.from_parts("edit_file", Role.DEVELOPER, {"secret": "marker"})

    storage, mission, service = run_service(tmp_path, [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "x", "new_text": "api_key=marker"}},
    ])
    result = service.start(mission, "selected")
    storage.close()

    assert result.status == "FAILED"
    assert b"marker" not in (tmp_path / "policy.db").read_bytes()


def test_approval_read_reports_corruption_explicitly(tmp_path):
    storage, mission, service = run_service(tmp_path, approval_responses())
    result = service.start(mission, "selected")
    approval = storage.list_approvals(result.mission_run_id)[0]
    storage._connection.execute("UPDATE approvals SET approval_json = ? WHERE approval_id = ?", ("{bad", str(approval.approval_id)))
    storage._connection.commit()

    with pytest.raises(ApprovalError, match="corrupted"):
        service.approval(approval.approval_id)
    storage.close()
