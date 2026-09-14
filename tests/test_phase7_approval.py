import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.domain.contracts import ModelRequest
from app.domain.models import ModelConfig, Role
from app.domain.policy import ApprovalStatus, PendingApproval, ToolCallSnapshot
from app.models.fake import FakeModelProvider
from app.models.factory import ProviderFactory
from app.models.lmstudio import ProviderError
from app.models.registry import ModelConfigRegistry
from app.persistence.sqlite import SQLiteStore
from app.services.mission import MissionService
from app.services.replay import ReplayService
from app.services.run import ApprovalError, RunService


class RecordingFake(FakeModelProvider):
    def __init__(self, responses):
        super().__init__(responses)
        self.requests = []

    def complete(self, request: ModelRequest):
        self.requests.append(request)
        return super().complete(request)


class SequenceFactory:
    def __init__(self, *providers):
        self.providers = list(providers)

    def create(self, config):
        return self.providers.pop(0)

    def create_from_snapshot(self, snapshot):
        return self.providers.pop(0)


def make_service(tmp_path, *providers):
    storage = SQLiteStore(tmp_path / "approval.db")
    mission = MissionService(storage).create("Fix auth", "missions/demo_auth_bug/repo")
    config = ModelConfig(model_id="selected", provider_type="fake", model_name="approval-test")
    service = RunService(
        registry=ModelConfigRegistry((config,), "selected"),
        provider_factory=SequenceFactory(*providers),
        workspace_root=tmp_path / "workspaces",
        skills_root=Path("skills"),
        storage=storage,
        approval_mode="policy",
    )
    return storage, mission, service


def waiting_providers(edit_old="return expiry < current_time", edit_new="return expiry > current_time"):
    initial = RecordingFake([
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}},
        {"kind": "tool", "name": "read_file", "arguments": {"path": "app/auth.py"}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": edit_old, "new_text": edit_new}},
    ])
    continuation = RecordingFake([
        {"output": {"status": "completed", "summary": "fixed", "changed_files": ["app/auth.py"]}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "passed", "passed": 2, "failed": 0, "issues": []}},
    ])
    return initial, continuation


def test_approve_continues_same_run_with_exact_snapshot_and_history(tmp_path):
    initial, continuation = waiting_providers()
    storage, mission, service = make_service(tmp_path, initial, continuation)
    pending = service.start(mission, "selected")
    approval = storage.list_approvals(pending.mission_run_id)[0]
    workspace = Path(pending.workspace_reference)

    result = service.approve(approval.approval_id)
    events = storage.list_events(pending.mission_run_id)
    approved_tool = next(event for event in events if event.event_type == "tool_result" and event.payload["tool_name"] == "edit_file")

    assert result["run_id"] == pending.mission_run_id
    assert result["run_status"] == "PASSED"
    assert storage.get_run(pending.mission_run_id).status == "PASSED"
    assert storage.get_approval(approval.approval_id).status == ApprovalStatus.APPROVED
    assert approved_tool.payload["arguments"] == approval.tool_call.arguments
    assert approved_tool.payload["call_id"] == approval.tool_call.call_id
    assert (workspace / "app/auth.py").read_text().endswith("return expiry > current_time\n")
    assert sum(event.event_type == "tool_call" and event.payload.get("name") == "edit_file" for event in events) == 1
    assert events[-1].event_type == "mission_finished"
    assert events.index(next(event for event in events if event.event_type == "approval_approved")) < approved_tool.sequence - 1
    messages = continuation.requests[0].messages
    assistant_index = max(index for index, message in enumerate(messages) if message.get("role") == "assistant" and message.get("tool_calls"))
    tool_index = max(index for index, message in enumerate(messages) if message.get("role") == "tool")
    assert assistant_index < tool_index
    assert messages[assistant_index]["tool_calls"][0]["id"] == approval.tool_call.call_id
    with pytest.raises(ApprovalError):
        service.approve(approval.approval_id)
    storage.close()


def test_restart_approve_and_read_only_api_use_same_run(tmp_path, monkeypatch):
    initial, continuation = waiting_providers()
    storage, mission, service = make_service(tmp_path, initial, continuation)
    pending = service.start(mission, "selected")
    approval = storage.list_approvals(pending.mission_run_id)[0]
    storage.close()
    reopened = SQLiteStore(tmp_path / "approval.db")
    restarted = RunService(
        registry=service.registry,
        provider_factory=service.provider_factory,
        workspace_root=tmp_path / "workspaces",
        skills_root=Path("skills"),
        storage=reopened,
        approval_mode="disabled",
    )
    monkeypatch.setattr(main_module, "runs", restarted)

    response = TestClient(main_module.app).post(f"/approvals/{approval.approval_id}/approve")

    assert response.status_code == 200
    assert response.json()["run_id"] == str(pending.mission_run_id)
    assert reopened.get_run(pending.mission_run_id).status == "PASSED"
    inspection = ReplayService(reopened).inspect(pending.mission_run_id)
    assert inspection.integrity.status_consistent is True
    assert [item.event_type for item in inspection.timeline][-2:] == ["agent_finished", "mission_finished"]
    reopened.close()


def test_reject_is_terminal_without_execution_or_recovery(tmp_path):
    initial, _ = waiting_providers()
    storage, mission, service = make_service(tmp_path, initial)
    pending = service.start(mission, "selected")
    approval = storage.list_approvals(pending.mission_run_id)[0]
    workspace = Path(pending.workspace_reference)
    before = (workspace / "app/auth.py").read_bytes()

    result = service.reject(approval.approval_id, "Change not approved")
    events = storage.list_events(pending.mission_run_id)

    assert result["run_status"] == "FAILED"
    assert storage.get_approval(approval.approval_id).status == ApprovalStatus.REJECTED
    assert storage.get_approval(approval.approval_id).decision_reason == "Change not approved"
    assert (workspace / "app/auth.py").read_bytes() == before
    assert events[-2].event_type == "approval_rejected"
    assert events[-1].event_type == "mission_finished"
    assert events[-1].payload["reason"] == "approval_rejected"
    assert not any(event.event_type == "tool_result" and event.payload.get("tool_name") == "edit_file" for event in events)
    assert not any(event.event_type == "recovery_started" for event in events)
    inspection = ReplayService(storage).inspect(pending.mission_run_id)
    assert [item.event_type for item in inspection.timeline][-2:] == ["approval_rejected", "mission_finished"]
    assert inspection.approvals[0].status == "REJECTED"
    with pytest.raises(ApprovalError):
        service.approve(approval.approval_id)
    with pytest.raises(ApprovalError):
        service.reject(approval.approval_id)
    storage.close()


def test_restart_reject_is_terminal_without_model_call(tmp_path):
    initial, _ = waiting_providers()
    storage, mission, service = make_service(tmp_path, initial)
    pending = service.start(mission, "selected")
    approval = storage.list_approvals(pending.mission_run_id)[0]
    before = (Path(pending.workspace_reference) / "app/auth.py").read_bytes()
    storage.close()
    reopened = SQLiteStore(tmp_path / "approval.db")
    restarted = RunService(
        registry=service.registry,
        provider_factory=ProviderFactory(fake_responses=[]),
        workspace_root=tmp_path / "workspaces",
        skills_root=Path("skills"),
        storage=reopened,
    )

    result = restarted.reject(approval.approval_id)

    assert result["run_id"] == pending.mission_run_id
    assert result["run_status"] == "FAILED"
    assert reopened.get_approval(approval.approval_id).status == ApprovalStatus.REJECTED
    assert (Path(pending.workspace_reference) / "app/auth.py").read_bytes() == before
    reopened.close()


def test_approval_api_reject_does_not_accept_executable_arguments(tmp_path, monkeypatch):
    initial, _ = waiting_providers()
    storage, mission, service = make_service(tmp_path, initial)
    pending = service.start(mission, "selected")
    approval = storage.list_approvals(pending.mission_run_id)[0]
    monkeypatch.setattr(main_module, "runs", service)

    response = TestClient(main_module.app).post(
        f"/approvals/{approval.approval_id}/reject",
        json={"reason": "not now", "arguments": {"path": "different.py"}},
    )

    assert response.status_code == 200
    assert response.json()["approval_status"] == "REJECTED"
    assert storage.get_approval(approval.approval_id).tool_call.arguments["path"] == "app/auth.py"
    storage.close()


def test_double_decision_and_sqlite_race_have_one_winner(tmp_path):
    initial, _ = waiting_providers()
    storage, mission, service = make_service(tmp_path, initial)
    pending = service.start(mission, "selected")
    approval = storage.list_approvals(pending.mission_run_id)[0]
    results = []
    barrier = threading.Barrier(2)
    stores = [storage, SQLiteStore(tmp_path / "approval.db")]

    def transition(store):
        barrier.wait()
        results.append(store.transition_approval(approval.approval_id, ApprovalStatus.PENDING, ApprovalStatus.APPROVED))

    threads = [threading.Thread(target=transition, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(result is not None for result in results) == 1
    assert storage.get_approval(approval.approval_id).status == ApprovalStatus.APPROVED
    assert storage.transition_approval(approval.approval_id, ApprovalStatus.PENDING, ApprovalStatus.REJECTED) is None
    stores[1].close()
    storage.close()


def test_approved_tool_failure_stays_approved_and_runs_once(tmp_path):
    initial, continuation = waiting_providers(edit_old="does not exist")
    continuation.responses = iter([
        {"output": {"status": "completed", "summary": "attempted"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed", "issues": ["still broken"]}},
        {"output": {"status": "completed", "summary": "not fixed"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed", "issues": ["still broken"]}},
    ])
    storage, mission, service = make_service(tmp_path, initial, continuation)
    pending = service.start(mission, "selected")
    approval = storage.list_approvals(pending.mission_run_id)[0]

    service.approve(approval.approval_id)
    events = storage.list_events(pending.mission_run_id)

    edit_results = [event for event in events if event.event_type == "tool_result" and event.payload.get("tool_name") == "edit_file"]
    assert len(edit_results) == 1
    assert edit_results[0].payload["success"] is False
    assert storage.get_approval(approval.approval_id).status == ApprovalStatus.APPROVED
    assert storage.get_run(pending.mission_run_id).status == "FAILED"
    storage.close()


def test_sequential_approvals_create_one_pending_at_a_time(tmp_path):
    initial, _ = waiting_providers()
    continuation = RecordingFake([
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry > current_time", "new_text": "return expiry > current_time  "}},
    ])
    second = RecordingFake([
        {"output": {"status": "completed", "summary": "fixed", "changed_files": ["app/auth.py"]}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "passed", "passed": 2, "failed": 0, "issues": []}},
    ])
    storage, mission, service = make_service(tmp_path, initial, continuation, second)
    first_run = service.start(mission, "selected")
    first = storage.list_approvals(first_run.mission_run_id)[0]
    service.approve(first.approval_id)
    approvals = storage.list_approvals(first_run.mission_run_id)

    assert first_run.mission_run_id == storage.get_run(first_run.mission_run_id).mission_run_id
    assert [approval.status for approval in approvals] == [ApprovalStatus.APPROVED, ApprovalStatus.PENDING]
    assert len([approval for approval in approvals if approval.status == ApprovalStatus.PENDING]) == 1
    second_result = service.approve(approvals[1].approval_id)
    assert second_result["run_status"] == "PASSED"
    assert [approval.status for approval in storage.list_approvals(first_run.mission_run_id)] == [ApprovalStatus.APPROVED, ApprovalStatus.APPROVED]
    storage.close()


def test_recovery_approval_continues_without_new_recovery_attempt(tmp_path):
    initial = RecordingFake([
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}},
        {"output": {"status": "completed", "summary": "attempted"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed", "issues": ["broken"]}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry < current_time", "new_text": "return expiry > current_time"}},
    ])
    continuation = RecordingFake([
        {"output": {"status": "completed", "summary": "recovered", "changed_files": ["app/auth.py"]}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "passed", "passed": 2, "failed": 0, "issues": []}},
    ])
    storage, mission, service = make_service(tmp_path, initial, continuation)
    pending = service.start(mission, "selected")
    approval = storage.list_approvals(pending.mission_run_id)[0]
    assert pending.recovery_count == 1
    service.approve(approval.approval_id)

    result = storage.get_run(pending.mission_run_id)
    assert result.status == "PASSED"
    assert result.recovery_count == 1
    assert not any(event.event_type == "recovery_started" and event.payload.get("recovery_attempt") == 2 for event in storage.list_events(pending.mission_run_id))
    storage.close()


def test_resumed_run_approval_keeps_lineage_and_same_resumed_id(tmp_path):
    class ParentProvider(RecordingFake):
        def complete(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return FakeModelProvider.complete(self, request)
            raise ProviderError("timeout_error", "timeout")

    parent_provider = ParentProvider([{ "output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}} }])
    _, resume_continuation = waiting_providers()
    resume_initial = RecordingFake([
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry < current_time", "new_text": "return expiry > current_time"}},
    ])
    storage, mission, service = make_service(tmp_path, parent_provider, resume_initial, resume_continuation)
    parent = service.start(mission, "selected")
    pm_checkpoint = next(
        checkpoint_id for checkpoint_id in parent.checkpoint_ids
        if storage.get_checkpoint(checkpoint_id).current_step == "pm_handoff"
    )

    resumed = service.resume(parent.mission_run_id, pm_checkpoint)
    approval = storage.list_approvals(resumed.mission_run_id)[0]
    service.approve(approval.approval_id)

    final = storage.get_run(resumed.mission_run_id)
    assert final.status == "PASSED"
    assert final.mission_run_id == resumed.mission_run_id
    assert final.resumed_from_run_id == parent.mission_run_id
    assert final.resumed_from_checkpoint_id == pm_checkpoint
    assert storage.get_run(parent.mission_run_id).status == "FAILED"
    storage.close()


def test_approval_snapshot_and_workspace_tamper_fail_without_execution(tmp_path):
    initial, continuation = waiting_providers()
    storage, mission, service = make_service(tmp_path, initial, continuation)
    pending = service.start(mission, "selected")
    approval = storage.list_approvals(pending.mission_run_id)[0]
    row = storage._connection.execute("SELECT approval_json FROM approvals WHERE approval_id = ?", (str(approval.approval_id),)).fetchone()
    payload = json.loads(row["approval_json"])
    payload["tool_call"]["arguments_digest"] = "0" * 64
    storage._connection.execute("UPDATE approvals SET approval_json = ? WHERE approval_id = ?", (json.dumps(payload), str(approval.approval_id)))
    storage._connection.commit()
    with pytest.raises(ApprovalError, match="snapshot"):
        service.approve(approval.approval_id)
    assert storage.get_run(pending.mission_run_id).status == "WAITING_APPROVAL"
    storage.close()

    initial, continuation = waiting_providers()
    storage, mission, service = make_service(tmp_path / "workspace-tamper", initial, continuation)
    pending = service.start(mission, "selected")
    approval = storage.list_approvals(pending.mission_run_id)[0]
    Path(pending.workspace_reference, "app/auth.py").write_text("tampered")
    with pytest.raises(ApprovalError, match="workspace"):
        service.approve(approval.approval_id)
    assert storage.get_approval(approval.approval_id).status == ApprovalStatus.PENDING
    storage.close()
