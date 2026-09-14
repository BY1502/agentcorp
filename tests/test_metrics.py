import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.domain.metrics import RunMetrics
from app.domain.models import (
    ExecutionManifest,
    Level,
    MissionRunResult,
    ModelConfig,
    ModelExecutionSnapshot,
    Role,
    TraceEvent,
)
from app.domain.policy import ApprovalStatus, PendingApproval, ToolCallSnapshot
from app.persistence.sqlite import SQLiteStore
from app.services.metrics import RunMetricsService
from app.services.replay import ReplayService
from app.services.store import AppStore


BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def event(run_id, mission_id, sequence, event_type, agent_id=None, payload=None, offset=0):
    return TraceEvent(
        mission_id=mission_id,
        mission_run_id=run_id,
        agent_run_id=agent_id,
        sequence=sequence,
        event_type=event_type,
        timestamp=BASE_TIME + timedelta(milliseconds=offset),
        payload=payload or {},
    )


def fixture(status="PASSED", recovery_count=0):
    mission_id, run_id = uuid4(), uuid4()
    pm_id, developer_id, qa_id = uuid4(), uuid4(), uuid4()
    manifest = ExecutionManifest(
        mission_id=mission_id,
        mission_version="7",
        employee_assignments={role: uuid4() for role in Role},
        model_references={},
        role_levels={role: Level.SENIOR for role in Role},
        skill_versions=(),
        runtime_config={"max_recovery_attempts": 1},
        initial_workspace_snapshot_id=uuid4(),
        model_snapshot=ModelExecutionSnapshot.from_config(
            ModelConfig(
                model_id="frozen-model",
                provider_type="fake",
                model_name="frozen-name",
                base_url="http://user:secret@example.test:1234/v1",
                timeout=30,
                credential_ref="secret-ref",
            )
        ),
    )
    events = [
        event(run_id, mission_id, 1, "mission_started", offset=0),
        event(run_id, mission_id, 2, "agent_started", pm_id, {"recovery_attempt": 0}, 1),
        event(run_id, mission_id, 3, "model_request", pm_id, {"role": "pm"}, 2),
        event(run_id, mission_id, 4, "agent_finished", pm_id, offset=4),
        event(run_id, mission_id, 5, "agent_started", developer_id, {"recovery_attempt": 0}, 5),
        event(run_id, mission_id, 6, "model_request", developer_id, {"role": "developer"}, 6),
        event(run_id, mission_id, 7, "tool_call", developer_id, {"name": "read_file"}, 7),
        event(run_id, mission_id, 8, "tool_result", developer_id, {"tool_name": "read_file", "success": True, "metadata": {}}, 8),
        event(run_id, mission_id, 9, "agent_finished", developer_id, offset=10),
        event(run_id, mission_id, 10, "agent_started", qa_id, {"recovery_attempt": 0}, 11),
        event(run_id, mission_id, 11, "model_request", qa_id, {"role": "qa"}, 12),
        event(run_id, mission_id, 12, "tool_call", qa_id, {"name": "run_test"}, 13),
        event(run_id, mission_id, 13, "tool_result", qa_id, {"tool_name": "run_test", "success": True, "metadata": {"exit_code": 0}}, 14),
        event(run_id, mission_id, 14, "model_request", qa_id, {"role": "qa"}, 15),
        event(run_id, mission_id, 15, "agent_finished", qa_id, offset=16),
        event(run_id, mission_id, 16, "mission_finished", payload={"status": status}, offset=20),
    ]
    result = MissionRunResult(
        mission_run_id=run_id,
        mission_id=mission_id,
        status=status,
        recovery_count=recovery_count,
        execution_manifest=manifest,
        pm_agent_run_id=pm_id,
        developer_agent_run_ids=[developer_id],
        qa_agent_run_ids=[qa_id],
        final_qa_result={"status": "passed" if status == "PASSED" else "failed"},
        changed_files=["app/auth.py"],
        tool_call_count=2,
        event_count=len(events),
        workspace_reference="/tmp/workspace",
        checkpoint_ids=[uuid4(), uuid4()],
    )
    return result, events


def save(storage, result, events, approvals=()):
    storage.save_run(result)
    storage.append_events(events)
    for approval in approvals:
        storage.save_approval(approval)


def approval(run_id, status, created_at, decided_at=None):
    snapshot = ToolCallSnapshot.from_parts("edit_file", Role.DEVELOPER, {"path": "app/auth.py"})
    return PendingApproval(
        run_id=run_id,
        agent_role=Role.DEVELOPER,
        tool_call=snapshot,
        policy_id="filesystem.write",
        reason="write requires approval",
        status=status,
        created_at=created_at,
        decided_at=decided_at,
    )


def test_metrics_aggregate_persisted_run_data_without_reexecution():
    result, events = fixture()
    storage = AppStore()
    save(storage, result, events)

    metrics = RunMetricsService(storage).get(result.mission_run_id)

    assert metrics == RunMetrics(
        run_id=result.mission_run_id,
        mission_id=result.mission_id,
        status="PASSED",
        started_at=BASE_TIME,
        finished_at=BASE_TIME + timedelta(milliseconds=20),
        duration_ms=20,
        agent_durations_ms={"pm": 3, "developer": 5, "qa": 5},
        model_id="frozen-model",
        provider_type="fake",
        model_name="frozen-name",
        model_call_count=4,
        tool_call_count=2,
        tool_success_count=2,
        tool_failure_count=0,
        tool_calls_by_name={"read_file": 1, "run_test": 1},
        qa_run_test_count=1,
        qa_test_pass_count=1,
        qa_test_fail_count=0,
        recovery_count=0,
        checkpoint_count=2,
        event_count=16,
        workspace_changed_file_count=1,
        integrity_status="valid",
    )


def test_metrics_use_exit_four_as_qa_test_failure_and_keep_recovery_count():
    result, events = fixture(status="FAILED", recovery_count=1)
    events[12] = events[12].model_copy(update={"payload": {"tool_name": "run_test", "success": False, "metadata": {"exit_code": 4}}})
    events[-1] = events[-1].model_copy(update={"payload": {"status": "FAILED"}})
    storage = AppStore()
    save(storage, result, events)

    metrics = RunMetricsService(storage).get(result.mission_run_id)

    assert metrics.qa_run_test_count == 1
    assert metrics.qa_test_pass_count == 0
    assert metrics.qa_test_fail_count == 1
    assert metrics.recovery_count == 1


def test_metrics_count_conflicts_and_required_tool_corrections_from_events():
    result, events = fixture()
    extra = [
        event(result.mission_run_id, result.mission_id, 0, "validation_error", payload={"reason": "qa_evidence_conflict"}),
        event(result.mission_run_id, result.mission_id, 0, "validation_error", payload={"reason": "required_tool_missing"}),
    ]
    events = events[:-1] + extra + events[-1:]
    events = [item.model_copy(update={"sequence": index}) for index, item in enumerate(events, 1)]
    result = result.model_copy(update={"event_count": len(events)})
    storage = AppStore()
    save(storage, result, events)

    metrics = RunMetricsService(storage).get(result.mission_run_id)

    assert metrics.qa_evidence_conflict_count == 1
    assert metrics.required_tool_correction_count == 1


def test_metrics_waiting_approval_is_partial_and_does_not_use_current_time():
    result, events = fixture(status="WAITING_APPROVAL")
    events = events[:-1]
    result = result.model_copy(update={"event_count": len(events)})
    pending = approval(result.mission_run_id, ApprovalStatus.PENDING, BASE_TIME)
    storage = AppStore()
    save(storage, result, events, [pending])

    metrics = RunMetricsService(storage).get(result.mission_run_id)

    assert metrics.status == "WAITING_APPROVAL"
    assert metrics.duration_ms is None
    assert metrics.approval_count == 1
    assert metrics.approval_pending_count == 1
    assert metrics.approval_wait_ms is None


def test_metrics_aggregate_approval_lifecycle_and_wait_time():
    result, events = fixture()
    approvals = [
        approval(result.mission_run_id, ApprovalStatus.APPROVED, BASE_TIME, BASE_TIME + timedelta(milliseconds=30)),
        approval(result.mission_run_id, ApprovalStatus.REJECTED, BASE_TIME, BASE_TIME + timedelta(milliseconds=20)),
        approval(result.mission_run_id, ApprovalStatus.EXPIRED, BASE_TIME, BASE_TIME + timedelta(milliseconds=10)),
    ]
    storage = AppStore()
    save(storage, result, events, approvals)

    metrics = RunMetricsService(storage).get(result.mission_run_id)

    assert metrics.approval_count == 3
    assert metrics.approval_approved_count == 1
    assert metrics.approval_rejected_count == 1
    assert metrics.approval_pending_count == 0
    assert metrics.approval_expired_count == 1
    assert metrics.approval_wait_ms == 60


def test_metrics_expose_resume_lineage_without_parent_aggregation():
    result, events = fixture()
    parent_id, checkpoint_id = uuid4(), uuid4()
    result = result.model_copy(update={"resumed_from_run_id": parent_id, "resumed_from_checkpoint_id": checkpoint_id})
    storage = AppStore()
    save(storage, result, events)

    metrics = RunMetricsService(storage).get(result.mission_run_id)

    assert metrics.is_resumed_run is True
    assert metrics.resumed_from_run_id == parent_id
    assert metrics.resumed_from_checkpoint_id == checkpoint_id


def test_metrics_are_restart_safe_and_replay_counts_match(tmp_path):
    result, events = fixture()
    database = tmp_path / "metrics.db"
    storage = SQLiteStore(database)
    save(storage, result, events)
    storage.close()

    restarted = SQLiteStore(database)
    metrics = RunMetricsService(restarted).get(result.mission_run_id)
    replay = ReplayService(restarted).inspect(result.mission_run_id)

    assert metrics.event_count == len(restarted.list_events(result.mission_run_id)) == 16
    assert metrics.checkpoint_count == len(replay.checkpoints) == 2
    assert metrics.tool_call_count == len(replay.tool_summary) == 2
    restarted.close()


def test_metrics_read_does_not_call_provider_tools_or_mutate_storage():
    result, events = fixture()
    storage = AppStore()
    save(storage, result, events)
    before = json.dumps(
        {
            "run": storage.get_run(result.mission_run_id).model_dump(mode="json"),
            "events": [item.model_dump(mode="json") for item in storage.list_events(result.mission_run_id)],
        },
        sort_keys=True,
    )

    class ReadOnlyStorage:
        def get_run(self, run_id):
            return storage.get_run(run_id)

        def list_events(self, run_id):
            return storage.list_events(run_id)

        def list_approvals(self, run_id):
            return storage.list_approvals(run_id)

    metrics = RunMetricsService(ReadOnlyStorage()).get(result.mission_run_id)
    after = json.dumps(
        {
            "run": storage.get_run(result.mission_run_id).model_dump(mode="json"),
            "events": [item.model_dump(mode="json") for item in storage.list_events(result.mission_run_id)],
        },
        sort_keys=True,
    )

    assert metrics.model_id == "frozen-model"
    assert before == after


def test_metrics_handle_missing_and_invalid_timestamps_without_fabrication():
    result, events = fixture()
    events[0] = events[0].model_copy(update={"timestamp": "invalid"})
    storage = AppStore()
    save(storage, result, events)
    metrics = RunMetricsService(storage).get(result.mission_run_id)
    assert metrics.started_at is None
    assert metrics.duration_ms is None
    assert metrics.integrity_status == "invalid"

    result2, events2 = fixture()
    events2[-1] = events2[-1].model_copy(update={"timestamp": BASE_TIME - timedelta(milliseconds=1)})
    storage2 = AppStore()
    save(storage2, result2, events2)
    metrics2 = RunMetricsService(storage2).get(result2.mission_run_id)
    assert metrics2.duration_ms is None
    assert metrics2.integrity_status == "invalid"

    result3, events3 = fixture()
    events3[0] = events3[0].model_copy(update={"timestamp": BASE_TIME.replace(tzinfo=None)})
    storage3 = AppStore()
    save(storage3, result3, events3)
    metrics3 = RunMetricsService(storage3).get(result3.mission_run_id)
    assert metrics3.duration_ms is None
    assert metrics3.integrity_status == "invalid"


def test_metrics_count_events_not_sequence_max_and_hide_sensitive_fields():
    result, events = fixture()
    events[3] = events[3].model_copy(update={"sequence": 40})
    storage = AppStore()
    save(storage, result, events)
    metrics = RunMetricsService(storage).get(result.mission_run_id)
    serialized = json.dumps(metrics.model_dump(mode="json")).lower()

    assert metrics.event_count == 16
    assert metrics.integrity_status == "invalid"
    assert all(term not in serialized for term in ("credential_ref", "secret", "password", "authorization", "stdout", "stderr", "reasoning"))


def test_metrics_return_invalid_read_model_when_event_storage_is_unreadable():
    result, events = fixture()

    class BrokenEvents:
        def get_run(self, run_id):
            return result if run_id == result.mission_run_id else None

        def list_events(self, run_id):
            raise ValueError("corrupt event JSON")

        def list_approvals(self, run_id):
            return []

    metrics = RunMetricsService(BrokenEvents()).get(result.mission_run_id)

    assert metrics.duration_ms is None
    assert metrics.event_count == 0
    assert metrics.integrity_status == "invalid"
