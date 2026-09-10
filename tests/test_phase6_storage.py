from pathlib import Path
from uuid import uuid4

from app.checkpoints.local import InMemoryCheckpointManager, LocalWorkspaceSnapshotManager
from app.domain.models import TraceEvent
from app.models.factory import ProviderFactory
from app.persistence.sqlite import SQLiteStore
from app.services.mission import MissionService
from app.services.run import RunService, default_fake_responses
from app.services.store import MissionRecord


def sqlite_run(tmp_path):
    db = tmp_path / "agentcorp.db"
    storage = SQLiteStore(db)
    mission = MissionService(storage).create("Fix auth", "missions/demo_auth_bug/repo")
    service = RunService(
        provider_factory=ProviderFactory(fake_responses=default_fake_responses()),
        workspace_root=tmp_path / "workspaces",
        skills_root=Path("skills"),
        storage=storage,
    )
    return db, storage, mission, service.start(mission)


def test_mission_run_events_and_checkpoint_survive_new_storage_instances(tmp_path):
    db, storage, mission, result = sqlite_run(tmp_path)
    events_before = storage.list_events(result.mission_run_id)
    checkpoint_id = result.checkpoint_ids[-1]
    storage.close()

    reopened = SQLiteStore(db)
    assert reopened.get_mission(mission.id).title == mission.title
    restored = reopened.get_run(result.mission_run_id)
    assert restored is not None
    assert restored.status == result.status == "PASSED"
    assert restored.execution_manifest.model_snapshot == result.execution_manifest.model_snapshot
    assert restored.recovery_count == result.recovery_count
    assert reopened.list_events(result.mission_run_id) == events_before

    snapshots = LocalWorkspaceSnapshotManager(tmp_path / "workspaces", reopened)
    checkpoints = InMemoryCheckpointManager(snapshots, reopened)
    state = checkpoints.restore(checkpoint_id)
    assert state.mission_run_id == result.mission_run_id
    destination = tmp_path / "restored"
    snapshots.restore(state.workspace_snapshot_id, destination)
    assert (destination / "app" / "auth.py").exists()
    reopened.close()


def test_historical_manifest_does_not_resolve_current_registry(tmp_path):
    from app.domain.models import ModelConfig
    from app.models.registry import ModelConfigRegistry

    db = tmp_path / "history.db"
    storage = SQLiteStore(db)
    registry = ModelConfigRegistry(
        (ModelConfig(model_id="selected", provider_type="fake", model_name="historical-v1"),),
        default_model_id="selected",
    )
    service = RunService(
        registry=registry,
        provider_factory=ProviderFactory(fake_responses=default_fake_responses()),
        workspace_root=tmp_path / "workspaces",
        skills_root=Path("skills"),
        storage=storage,
    )
    mission = MissionRecord("Fix auth", "missions/demo_auth_bug/repo")
    storage.save_mission(mission)
    result = service.start(mission, "selected")
    registry.register(ModelConfig(model_id="selected", provider_type="fake", model_name="current-v2"))
    storage.close()

    reopened = SQLiteStore(db)
    historical = reopened.get_run(result.mission_run_id)
    assert historical.execution_manifest.model_snapshot.model_name == "historical-v1"
    reopened.close()


def test_events_are_append_only_and_read_in_sequence_order(tmp_path):
    storage = SQLiteStore(tmp_path / "events.db")
    run_id = uuid4()
    events = [
        TraceEvent(mission_id=uuid4(), mission_run_id=run_id, sequence=1, event_type="first"),
        TraceEvent(mission_id=uuid4(), mission_run_id=run_id, sequence=2, event_type="second"),
    ]
    storage.append_events(events)
    storage.append_events([TraceEvent(mission_id=uuid4(), mission_run_id=run_id, sequence=3, event_type="third")])
    assert [event.event_type for event in storage.list_events(run_id)] == ["first", "second", "third"]
    storage.close()


def test_persistent_json_does_not_contain_secret_or_reasoning_markers(tmp_path):
    db, storage, _, result = sqlite_run(tmp_path)
    manifest = result.execution_manifest.model_copy(
        update={
            "model_references": {
                uuid4(): {
                    "api_key": "api-key-marker",
                    "credential_ref": "credential-marker",
                    "reasoning_content": "reasoning-marker",
                }
            }
        }
    )
    unsafe = result.model_copy(update={"execution_manifest": manifest})
    event = TraceEvent(
        mission_id=result.mission_id,
        mission_run_id=result.mission_run_id,
        sequence=result.event_count + 1,
        event_type="provider_observation",
        payload={"api_key": "api-key-marker", "reasoning_content": "reasoning-marker"},
    )
    storage.finalize_run(unsafe, [event])
    storage.close()
    raw = db.read_bytes()
    for marker in (b"api-key-marker", b"credential-marker", b"reasoning-marker"):
        assert marker not in raw


def test_recovery_exhaustion_metadata_survives_restart(tmp_path):
    storage = SQLiteStore(tmp_path / "recovery.db")
    mission = MissionService(storage).create("Fix auth", "missions/demo_auth_bug/repo")
    responses = [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}},
        {"output": {"status": "completed", "summary": "attempted"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed"}},
        {"output": {"status": "completed", "summary": "reworked"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed"}},
    ]
    service = RunService(
        provider_factory=ProviderFactory(fake_responses=responses),
        workspace_root=tmp_path / "workspaces",
        skills_root=Path("skills"),
        storage=storage,
    )
    result = service.start(mission)
    storage.close()

    reopened = SQLiteStore(tmp_path / "recovery.db")
    historical = reopened.get_run(result.mission_run_id)
    events = reopened.list_events(result.mission_run_id)
    assert historical.recovery_count == historical.retry_count == 1
    assert historical.status == "FAILED"
    assert any(event.event_type == "recovery_started" for event in events)
    assert any(event.event_type == "recovery_exhausted" for event in events)
    reopened.close()


def test_qa_evidence_conflict_metadata_survives_restart(tmp_path):
    storage = SQLiteStore(tmp_path / "conflict.db")
    mission = MissionService(storage).create("Fix auth", "missions/demo_auth_bug/repo")
    responses = [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry < current_time", "new_text": "return expiry > current_time"}},
        {"output": {"status": "completed", "summary": "fixed"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed", "issues": ["model disagreement"]}},
    ]
    service = RunService(
        provider_factory=ProviderFactory(fake_responses=responses),
        workspace_root=tmp_path / "workspaces",
        skills_root=Path("skills"),
        storage=storage,
    )
    result = service.start(mission)
    storage.close()

    reopened = SQLiteStore(tmp_path / "conflict.db")
    historical = reopened.get_run(result.mission_run_id)
    events = reopened.list_events(result.mission_run_id)
    conflict = next(event for event in events if event.event_type == "validation_error")
    assert historical.status == "FAILED"
    assert historical.recovery_count == 0
    assert conflict.payload["reason"] == "qa_evidence_conflict"
    reopened.close()
