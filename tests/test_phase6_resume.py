from pathlib import Path

import pytest
from fastapi.testclient import TestClient
import app.main as main_module

from app.domain.models import ModelConfig
from app.models.factory import ProviderFactory
from app.models.fake import FakeModelProvider
from app.models.registry import ModelConfigRegistry
from app.persistence.sqlite import SQLiteStore
from app.services.mission import MissionService
from app.services.replay import ReplayService
from app.services.run import ResumeError, ResumeNotFoundError, RunService


def responses(*items):
    return list(items)


def failed_parent_responses():
    return responses(
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix auth"}}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry < current_time", "new_text": "return expiry > current_time"}},
        {"output": {"status": "completed", "summary": "fixed"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed", "issues": ["model disagreement"]}},
    )


class SequencedFactory:
    def __init__(self, *providers):
        self.providers = list(providers)
        self.snapshot_calls = 0
        self.snapshots = []

    def create(self, config):
        return self.providers.pop(0)

    def create_from_snapshot(self, snapshot):
        self.snapshot_calls += 1
        self.snapshots.append(snapshot)
        return self.providers.pop(0)


class UnavailableFactory(SequencedFactory):
    def create_from_snapshot(self, snapshot):
        self.snapshot_calls += 1
        self.snapshots.append(snapshot)
        raise RuntimeError("historical model unavailable")


def service(tmp_path, factory):
    storage = SQLiteStore(tmp_path / "resume.db")
    mission = MissionService(storage).create("Fix auth", "missions/demo_auth_bug/repo")
    registry = ModelConfigRegistry(
        (ModelConfig(model_id="selected", provider_type="fake", model_name="historical-v1"),),
        default_model_id="selected",
    )
    return (
        storage,
        mission,
        RunService(
            registry=registry,
            provider_factory=factory,
            workspace_root=tmp_path / "workspaces",
            skills_root=Path("skills"),
            storage=storage,
        ),
    )


def checkpoint_for(storage, result, step):
    for checkpoint_id in result.checkpoint_ids:
        checkpoint = storage.get_checkpoint(checkpoint_id)
        if checkpoint.current_step == step and checkpoint.agent_state.finished:
            return checkpoint_id, checkpoint
    raise AssertionError(f"checkpoint not found: {step}")


def test_resume_from_pm_handoff_creates_isolated_run(tmp_path):
    factory = SequencedFactory(
        FakeModelProvider(failed_parent_responses()),
        FakeModelProvider(
            responses(
                {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry < current_time", "new_text": "return expiry > current_time"}},
                {"output": {"status": "completed", "summary": "fixed"}},
                {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
                {"output": {"status": "passed", "passed": 2, "failed": 0}},
            )
        ),
    )
    storage, mission, runs = service(tmp_path, factory)
    parent = runs.start(mission, "selected")
    checkpoint_id, _ = checkpoint_for(storage, parent, "pm_handoff")
    parent_events = storage.list_events(parent.mission_run_id)
    parent_snapshot = storage.get_run(parent.mission_run_id).model_dump_json()
    parent_workspace = Path(parent.workspace_reference)
    parent_files = {path.relative_to(parent_workspace): path.read_bytes() for path in parent_workspace.rglob("*") if path.is_file()}

    resumed = runs.resume(parent.mission_run_id, checkpoint_id)

    assert resumed.status == "PASSED"
    assert resumed.mission_run_id != parent.mission_run_id
    assert resumed.resumed_from_run_id == parent.mission_run_id
    assert resumed.resumed_from_checkpoint_id == checkpoint_id
    assert resumed.execution_manifest == parent.execution_manifest
    assert len(storage.list_events(resumed.mission_run_id)) > 0
    assert storage.get_run(parent.mission_run_id).model_dump_json() == parent_snapshot
    assert storage.list_events(parent.mission_run_id) == parent_events
    assert {path.relative_to(parent_workspace): path.read_bytes() for path in parent_workspace.rglob("*") if path.is_file()} == parent_files
    assert factory.snapshot_calls == 1
    storage.close()


def test_resume_from_developer_handoff_continues_at_qa(tmp_path):
    factory = SequencedFactory(
        FakeModelProvider(failed_parent_responses()),
        FakeModelProvider(
            responses(
                {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
                {"output": {"status": "passed", "passed": 2, "failed": 0}},
            )
        ),
    )
    storage, mission, runs = service(tmp_path, factory)
    parent = runs.start(mission, "selected")
    checkpoint_id, checkpoint = checkpoint_for(storage, parent, "developer")

    resumed = runs.resume(parent.mission_run_id, checkpoint_id)
    events = storage.list_events(resumed.mission_run_id)

    assert resumed.status == "PASSED"
    assert resumed.recovery_count == checkpoint.agent_state.recovery_attempt == 0
    assert not any(event.event_type == "agent_started" and event.payload.get("recovery_attempt") == 0 for event in events if event.agent_run_id == checkpoint.current_agent_run_id)
    assert [event.event_type for event in events].count("model_request") == 2
    storage.close()


def test_resume_from_recovery_handoff_carries_budget_and_state(tmp_path):
    parent_responses = responses(
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix auth"}}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry < current_time", "new_text": "return expiry == current_time"}},
        {"output": {"status": "completed", "summary": "attempted"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed", "issues": ["incorrect"]}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry == current_time", "new_text": "return expiry > current_time"}},
        {"output": {"status": "completed", "summary": "reworked"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed", "issues": ["model disagreement"]}},
    )
    factory = SequencedFactory(
        FakeModelProvider(parent_responses),
        FakeModelProvider(
            responses(
                {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
                {"output": {"status": "passed", "passed": 2, "failed": 0}},
            )
        ),
    )
    storage, mission, runs = service(tmp_path, factory)
    parent = runs.start(mission, "selected")
    checkpoint_id, checkpoint = checkpoint_for(storage, parent, "developer_recovery_1")

    resumed = runs.resume(parent.mission_run_id, checkpoint_id)

    assert resumed.status == "PASSED"
    assert resumed.recovery_count == resumed.retry_count == 1
    assert checkpoint.agent_state.recovery_attempt == 1
    storage.close()


def test_resume_rejects_unsupported_checkpoint_and_does_not_create_provider(tmp_path):
    factory = SequencedFactory(FakeModelProvider(failed_parent_responses()))
    storage, mission, runs = service(tmp_path, factory)
    parent = runs.start(mission, "selected")
    edit_checkpoint, _ = checkpoint_for(storage, parent, "developer")
    edit_checkpoint = next(
        checkpoint_id
        for checkpoint_id in parent.checkpoint_ids
        if storage.get_checkpoint(checkpoint_id).agent_state.finished is False
    )

    with pytest.raises(ResumeError, match="supported handoff"):
        runs.resume(parent.mission_run_id, edit_checkpoint)
    assert factory.snapshot_calls == 0
    assert len(storage.list_events(parent.mission_run_id)) == parent.event_count
    storage.close()


def test_resume_rejects_unknown_run_checkpoint_and_passed_parent(tmp_path):
    factory = SequencedFactory(FakeModelProvider(failed_parent_responses()))
    storage, mission, runs = service(tmp_path, factory)
    parent = runs.start(mission, "selected")
    checkpoint_id, _ = checkpoint_for(storage, parent, "pm_handoff")

    with pytest.raises(ResumeNotFoundError):
        runs.resume("00000000-0000-0000-0000-000000000000", checkpoint_id)
    with pytest.raises(ResumeError, match="does not belong"):
        runs.resume(parent.mission_run_id, "00000000-0000-0000-0000-000000000000")

    passed = parent.model_copy(update={"status": "PASSED"})
    storage.save_run(passed)
    with pytest.raises(ResumeError, match="only failed"):
        runs.resume(parent.mission_run_id, checkpoint_id)
    storage.close()


def test_resume_rejects_checkpoint_from_another_run(tmp_path):
    factory = SequencedFactory(
        FakeModelProvider(failed_parent_responses()),
        FakeModelProvider(failed_parent_responses()),
    )
    storage, mission, runs = service(tmp_path, factory)
    first = runs.start(mission, "selected")
    second = runs.start(mission, "selected")
    checkpoint_id, _ = checkpoint_for(storage, second, "pm_handoff")

    with pytest.raises(ResumeError, match="does not belong"):
        runs.resume(first.mission_run_id, checkpoint_id)
    assert factory.snapshot_calls == 0
    storage.close()


def test_corrupted_checkpoint_fails_before_provider_construction(tmp_path):
    factory = SequencedFactory(FakeModelProvider(failed_parent_responses()))
    storage, mission, runs = service(tmp_path, factory)
    parent = runs.start(mission, "selected")
    checkpoint_id, _ = checkpoint_for(storage, parent, "pm_handoff")
    storage._connection.execute(
        "UPDATE checkpoints SET state_json = ? WHERE checkpoint_id = ?",
        ("{\"corrupted\":true}", str(checkpoint_id)),
    )
    storage._connection.commit()

    with pytest.raises(ResumeError, match="corrupted"):
        runs.resume(parent.mission_run_id, checkpoint_id)
    assert factory.snapshot_calls == 0
    storage.close()


def test_missing_snapshot_and_unavailable_model_fail_before_execution(tmp_path):
    factory = SequencedFactory(FakeModelProvider(failed_parent_responses()))
    storage, mission, runs = service(tmp_path, factory)
    parent = runs.start(mission, "selected")
    checkpoint_id, checkpoint = checkpoint_for(storage, parent, "pm_handoff")
    storage._connection.execute("DELETE FROM workspace_snapshots WHERE id = ?", (str(checkpoint.workspace_snapshot_id),))
    storage._connection.commit()

    with pytest.raises(ResumeError, match="workspace snapshot"):
        runs.resume(parent.mission_run_id, checkpoint_id)
    assert factory.snapshot_calls == 0

    storage.close()

    factory = UnavailableFactory(FakeModelProvider(failed_parent_responses()))
    storage, mission, runs = service(tmp_path / "unavailable", factory)
    parent = runs.start(mission, "selected")
    checkpoint_id, _ = checkpoint_for(storage, parent, "pm_handoff")
    with pytest.raises(ResumeError, match="provider"):
        runs.resume(parent.mission_run_id, checkpoint_id)
    assert factory.snapshot_calls == 1
    assert factory.snapshots[0].model_name == "historical-v1"
    storage.close()


def test_resumed_run_replays_with_lineage_after_storage_restart(tmp_path):
    factory = SequencedFactory(
        FakeModelProvider(failed_parent_responses()),
        FakeModelProvider(
            responses(
                {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
                {"output": {"status": "passed", "passed": 2, "failed": 0}},
            )
        ),
    )
    storage, mission, runs = service(tmp_path, factory)
    parent = runs.start(mission, "selected")
    checkpoint_id, _ = checkpoint_for(storage, parent, "developer")
    storage.close()

    reopened = SQLiteStore(tmp_path / "resume.db")
    restarted = RunService(
        registry=runs.registry,
        provider_factory=factory,
        workspace_root=tmp_path / "workspaces",
        skills_root=Path("skills"),
        storage=reopened,
    )
    resumed = restarted.resume(parent.mission_run_id, checkpoint_id)
    inspection = ReplayService(reopened).inspect(resumed.mission_run_id)

    assert reopened.get_run(resumed.mission_run_id).resumed_from_run_id == parent.mission_run_id
    assert inspection.resumed_from_checkpoint_id == checkpoint_id
    assert inspection.execution_manifest == parent.execution_manifest
    reopened.close()


def test_resume_api_returns_new_run_lineage(tmp_path, monkeypatch):
    factory = SequencedFactory(
        FakeModelProvider(failed_parent_responses()),
        FakeModelProvider(
            responses(
                {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
                {"output": {"status": "passed", "passed": 2, "failed": 0}},
            )
        ),
    )
    storage, mission, runs = service(tmp_path, factory)
    parent = runs.start(mission, "selected")
    checkpoint_id, _ = checkpoint_for(storage, parent, "developer")
    monkeypatch.setattr(main_module, "runs", runs)

    response = TestClient(main_module.app).post(
        f"/runs/{parent.mission_run_id}/resume",
        json={"checkpoint_id": str(checkpoint_id)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] != str(parent.mission_run_id)
    assert body["resumed_from_run_id"] == str(parent.mission_run_id)
    assert body["resumed_from_checkpoint_id"] == str(checkpoint_id)
    storage.close()
