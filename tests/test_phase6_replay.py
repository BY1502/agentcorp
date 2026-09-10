import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as main_module
from app.domain.models import ModelConfig
from app.models.factory import ProviderFactory
from app.models.registry import ModelConfigRegistry
from app.persistence.sqlite import SQLiteStore
from app.services.mission import MissionService
from app.services.replay import ReplayService
from app.services.run import RunService, default_fake_responses


def persisted_run(tmp_path, responses=None, registry=None):
    storage = SQLiteStore(tmp_path / "replay.db")
    mission = MissionService(storage).create("Fix auth", "missions/demo_auth_bug/repo")
    service = RunService(
        registry=registry,
        provider_factory=ProviderFactory(fake_responses=responses or default_fake_responses()),
        workspace_root=tmp_path / "workspaces",
        skills_root=Path("skills"),
        storage=storage,
    )
    result = service.start(mission)
    storage.close()
    return tmp_path / "replay.db", result


def test_normal_run_replay_reconstructs_timeline_and_snapshots(tmp_path):
    db, result = persisted_run(tmp_path)
    storage = SQLiteStore(db)
    before = storage.list_events(result.mission_run_id)
    inspection = ReplayService(storage).inspect(result.mission_run_id)
    after = storage.list_events(result.mission_run_id)

    assert inspection.final_status == "PASSED"
    assert [item.sequence for item in inspection.timeline] == [event.sequence for event in before]
    assert inspection.agent_summary["pm"] == {"model_calls": 1, "agent_runs": 1}
    assert inspection.agent_summary["developer"] == {"model_calls": 4, "agent_runs": 1}
    assert inspection.agent_summary["qa"] == {"model_calls": 2, "agent_runs": 1}
    assert [item.tool_name for item in inspection.tool_summary] == ["list_files", "read_file", "edit_file", "run_test"]
    assert inspection.recovery_summary == {"recovery_count": 0, "recovery_started": 0, "exhausted": False}
    assert inspection.integrity.status_consistent is True
    assert inspection.checkpoints
    assert any(item.path == "app/auth.py" for item in inspection.checkpoints[0].workspace_snapshot.files)
    assert before == after
    assert ReplayService(storage).inspect(result.mission_run_id) == inspection
    storage.close()


def test_recovery_success_replay_is_derived_from_persisted_events(tmp_path):
    responses = [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix auth"}}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry < current_time", "new_text": "return expiry == current_time"}},
        {"output": {"status": "completed", "summary": "attempted"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed", "issues": ["incorrect"]}},
        {"kind": "tool", "name": "read_file", "arguments": {"path": "app/auth.py"}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry == current_time", "new_text": "return expiry > current_time"}},
        {"output": {"status": "completed", "summary": "reworked"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "passed", "passed": 2, "failed": 0}},
    ]
    db, result = persisted_run(tmp_path, responses)
    inspection = ReplayService(SQLiteStore(db)).inspect(result.mission_run_id)

    assert inspection.recovery_summary == {"recovery_count": 1, "recovery_started": 1, "exhausted": False}
    assert inspection.agent_summary["developer"] == {"model_calls": 5, "agent_runs": 2}
    assert inspection.agent_summary["qa"] == {"model_calls": 4, "agent_runs": 2}
    assert [item["exit_code"] for item in inspection.evidence_summary["run_tests"]] == [1, 0]
    assert inspection.final_status == "PASSED"


def test_recovery_exhaustion_replay_is_read_only(tmp_path):
    responses = [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}},
        {"output": {"status": "completed", "summary": "attempted"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed"}},
        {"output": {"status": "completed", "summary": "reworked"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed"}},
    ]
    db, result = persisted_run(tmp_path, responses)
    storage = SQLiteStore(db)
    before = storage.list_events(result.mission_run_id)
    inspection = ReplayService(storage).inspect(result.mission_run_id)
    after = storage.list_events(result.mission_run_id)

    assert inspection.final_status == "FAILED"
    assert inspection.recovery_summary["exhausted"] is True
    assert any(item.event_type == "recovery_exhausted" for item in inspection.timeline)
    assert before == after
    storage.close()


def test_evidence_conflict_replay_preserves_phase5_policy(tmp_path):
    responses = [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}},
        {"kind": "tool", "name": "edit_file", "arguments": {"path": "app/auth.py", "old_text": "return expiry < current_time", "new_text": "return expiry > current_time"}},
        {"output": {"status": "completed", "summary": "fixed"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "failed", "issues": ["model disagreement"]}},
    ]
    db, result = persisted_run(tmp_path, responses)
    inspection = ReplayService(SQLiteStore(db)).inspect(result.mission_run_id)

    assert inspection.final_status == "FAILED"
    assert inspection.recovery_summary["recovery_count"] == 0
    assert inspection.evidence_summary["evidence_conflict"] is True
    assert inspection.integrity.status_consistent is True


def test_replay_uses_frozen_manifest_without_registry_or_provider(tmp_path):
    registry = ModelConfigRegistry(
        (ModelConfig(model_id="selected", provider_type="fake", model_name="historical-v1"),),
        default_model_id="selected",
    )
    db, result = persisted_run(tmp_path, registry=registry)
    registry.register(ModelConfig(model_id="selected", provider_type="fake", model_name="current-v2"))
    inspection = ReplayService(SQLiteStore(db)).inspect(result.mission_run_id)

    assert inspection.execution_manifest.model_snapshot.model_name == "historical-v1"
    assert inspection.execution_manifest.model_snapshot.model_id == "selected"


def test_corrupted_historical_status_is_reported_not_repaired(tmp_path):
    db, result = persisted_run(tmp_path)
    connection = sqlite3.connect(db)
    row = connection.execute(
        "SELECT id, event_json FROM events WHERE mission_run_id = ? ORDER BY sequence DESC LIMIT 1",
        (str(result.mission_run_id),),
    ).fetchone()
    event = json.loads(row[1])
    event["payload"]["status"] = "FAILED"
    connection.execute("UPDATE events SET event_json = ? WHERE id = ?", (json.dumps(event), row[0]))
    connection.commit()
    connection.close()

    inspection = ReplayService(SQLiteStore(db)).inspect(result.mission_run_id)
    assert inspection.final_status == "PASSED"
    assert inspection.integrity.status_consistent is False
    assert "mission_status_mismatch" in inspection.consistency_issues


def test_replay_read_model_does_not_expose_raw_payload_or_secrets(tmp_path):
    db, result = persisted_run(tmp_path)
    storage = SQLiteStore(db)
    unsafe_result = result.model_copy(
        update={
            "final_qa_result": {
                "status": "passed",
                "api_key": "api-key-marker",
                "reasoning_content": "reasoning-marker",
                "credential_ref": "credential-marker",
            }
        }
    )
    storage.save_run(unsafe_result)
    inspection = ReplayService(storage).inspect(result.mission_run_id)
    serialized = inspection.model_dump_json()
    assert all(marker not in serialized for marker in ("api-key-marker", "reasoning-marker", "credential-marker"))
    assert "tool_results" not in serialized
    storage.close()


def test_replay_endpoint_has_404_semantics_for_unknown_run():
    response = TestClient(main_module.app).get(f"/runs/{uuid4()}/replay")
    assert response.status_code == 404
