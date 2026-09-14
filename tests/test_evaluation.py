import json
from datetime import timedelta
from uuid import uuid4

from app.domain.models import AgentState, CheckpointState, MissionRunResult, Role
from app.domain.policy import ApprovalStatus
from app.persistence.sqlite import SQLiteStore
from app.services.evaluation import RunEvaluationService
from app.services.metrics import RunMetricsService
from app.services.replay import ReplayService, RunNotFoundError
from app.services.store import AppStore

from test_metrics import BASE_TIME, approval, event, fixture, save


def rule(report, rule_id):
    return next(item for item in report.rules if item.rule_id == rule_id)


def renumber(result, events):
    events = [item.model_copy(update={"sequence": index}) for index, item in enumerate(events, 1)]
    return result.model_copy(update={"event_count": len(events)}), events


def approval_flow(result, events, status):
    item = approval(
        result.mission_run_id,
        status,
        BASE_TIME,
        None if status == ApprovalStatus.PENDING else BASE_TIME + timedelta(milliseconds=30),
    )
    required = {
        "approval_id": str(item.approval_id),
        "policy_id": item.policy_id,
        "policy_version": item.policy_version,
        "tool_name": item.tool_call.tool_name,
        "arguments_digest": item.tool_call.arguments_digest,
        "call_id": item.tool_call.call_id,
    }
    additions = [event(result.mission_run_id, result.mission_id, 0, "approval_required", payload=required)]
    if status != ApprovalStatus.PENDING:
        additions.append(event(result.mission_run_id, result.mission_id, 0, f"approval_{status.value.lower()}", payload=required))
        result = result.model_copy(update={"status": "FAILED", "final_qa_result": {"status": "failed"}})
    else:
        result = result.model_copy(update={"status": "WAITING_APPROVAL", "final_qa_result": {"status": "pending"}})
        events = events[:-1]
    result, events = renumber(result, events + additions)
    if status != ApprovalStatus.PENDING:
        result, events = renumber(result, events + [event(result.mission_run_id, result.mission_id, 0, "mission_finished", payload={"status": "FAILED"})])
    return result, events, item


def test_passed_run_evaluates_with_versioned_rules():
    result, events = fixture()
    storage = AppStore()
    save(storage, result, events)

    report = RunEvaluationService(storage).get(result.mission_run_id)

    assert report.evaluation_version == "agentcorp-eval-v1"
    assert report.status == "PASS"
    assert rule(report, "qa_evidence_present").status == "PASS"
    assert rule(report, "qa_final_test_outcome").status == "PASS"
    assert rule(report, "approval_integrity").status == "NOT_APPLICABLE"


def test_failed_final_test_is_not_hidden_by_terminal_run_status():
    result, events = fixture(status="FAILED", recovery_count=1)
    events[12] = events[12].model_copy(update={"payload": {"tool_name": "run_test", "success": False, "metadata": {"exit_code": 4}}})
    events[-1] = events[-1].model_copy(update={"payload": {"status": "FAILED"}})
    storage = AppStore()
    save(storage, result, events)

    report = RunEvaluationService(storage).get(result.mission_run_id)

    assert report.status == "FAIL"
    assert rule(report, "run_completed").status == "PASS"
    assert rule(report, "qa_final_test_outcome").status == "FAIL"
    assert rule(report, "recovery_budget").status == "PASS"


def test_latest_qa_evidence_wins_over_prior_recovery_failure():
    result, events = fixture(recovery_count=1)
    qa_id = result.qa_agent_run_ids[0]
    prior = event(
        result.mission_run_id,
        result.mission_id,
        0,
        "tool_result",
        qa_id,
        {"tool_name": "run_test", "success": False, "metadata": {"exit_code": 4}},
    )
    storage = AppStore()
    result, events = renumber(result, [*events[:12], prior, *events[12:]])
    save(storage, result, events)

    report = RunEvaluationService(storage).get(result.mission_run_id)

    assert report.status == "PASS"
    assert rule(report, "qa_final_test_outcome").evidence["value"] == 0


def test_recovery_at_exact_budget_is_not_over_budget():
    result, events = fixture(status="FAILED", recovery_count=1)
    events[12] = events[12].model_copy(update={"payload": {"tool_name": "run_test", "success": False, "metadata": {"exit_code": 1}}})
    events[-1] = events[-1].model_copy(update={"payload": {"status": "FAILED"}})
    storage = AppStore()
    save(storage, result, events)

    report = RunEvaluationService(storage).get(result.mission_run_id)

    assert rule(report, "recovery_budget").status == "PASS"


def test_warning_with_successful_exit_is_pass():
    result, events = fixture()
    events[12] = events[12].model_copy(update={"payload": {"tool_name": "run_test", "success": True, "metadata": {"exit_code": 0, "warning_count": 1}}})
    storage = AppStore()
    save(storage, result, events)

    report = RunEvaluationService(storage).get(result.mission_run_id)

    assert report.status == "PASS"
    assert rule(report, "qa_final_test_outcome").status == "PASS"


def test_evidence_conflict_fails_without_rewriting_qa_status():
    result, events = fixture()
    conflict = event(result.mission_run_id, result.mission_id, 0, "validation_error", payload={"reason": "qa_evidence_conflict"})
    result, events = renumber(result, [*events[:-1], conflict, events[-1]])
    storage = AppStore()
    save(storage, result, events)

    report = RunEvaluationService(storage).get(result.mission_run_id)

    assert report.status == "FAIL"
    assert rule(report, "qa_evidence_consistency").status == "FAIL"


def test_missing_qa_evidence_never_passes():
    result, events = fixture()
    result, events = renumber(result, events[:12] + events[13:])
    storage = AppStore()
    save(storage, result, events)

    report = RunEvaluationService(storage).get(result.mission_run_id)

    assert report.status == "FAIL"
    assert rule(report, "qa_evidence_present").status == "FAIL"
    assert rule(report, "qa_final_test_outcome").status == "UNKNOWN"


def test_approval_lifecycle_rules_cover_pending_and_rejected():
    pending, pending_events, pending_approval = approval_flow(*fixture(), ApprovalStatus.PENDING)
    pending_store = AppStore()
    save(pending_store, pending, pending_events, [pending_approval])
    pending_report = RunEvaluationService(pending_store).get(pending.mission_run_id)

    rejected, rejected_events, rejected_approval = approval_flow(*fixture(), ApprovalStatus.REJECTED)
    rejected_store = AppStore()
    save(rejected_store, rejected, rejected_events, [rejected_approval])
    rejected_report = RunEvaluationService(rejected_store).get(rejected.mission_run_id)

    assert rule(pending_report, "approval_integrity").status == "PASS"
    assert rule(pending_report, "run_completed").status == "FAIL"
    assert rule(rejected_report, "approval_integrity").status == "PASS"


def test_stale_pending_approval_on_terminal_run_fails_integrity():
    result, events, item = approval_flow(*fixture(), ApprovalStatus.PENDING)
    stale = event(result.mission_run_id, result.mission_id, 0, "approval_stale", payload={"approval_id": str(item.approval_id)})
    result = result.model_copy(update={"status": "FAILED", "final_qa_result": {"status": "failed"}})
    result, events = renumber(result, [*events, stale, event(result.mission_run_id, result.mission_id, 0, "mission_finished", payload={"status": "FAILED"})])
    storage = AppStore()
    save(storage, result, events, [item])

    report = RunEvaluationService(storage).get(result.mission_run_id)

    assert rule(report, "approval_integrity").status == "FAIL"


def test_approval_tamper_and_rejected_execution_fail():
    result, events, item = approval_flow(*fixture(), ApprovalStatus.REJECTED)
    tampered = events[-2].model_copy(update={"payload": {**events[-2].payload, "call_id": "tampered"}})
    events[-2] = tampered
    tool = event(
        result.mission_run_id,
        result.mission_id,
        0,
        "tool_result",
        payload={"approval_id": str(item.approval_id), "tool_name": "edit_file", "success": True, "metadata": {}},
    )
    result, events = renumber(result, [*events[:-1], tool, events[-1]])
    storage = AppStore()
    save(storage, result, events, [item])

    report = RunEvaluationService(storage).get(result.mission_run_id)

    assert rule(report, "approval_integrity").status == "FAIL"


def test_corrupt_history_is_failed_and_evaluation_is_read_only():
    result, events = fixture()
    events[3] = events[3].model_copy(update={"sequence": 40})
    storage = AppStore()
    save(storage, result, events)
    before = json.dumps(storage.get_run(result.mission_run_id).model_dump(mode="json"), sort_keys=True)

    service = RunEvaluationService(storage)
    first = service.get(result.mission_run_id)
    second = service.get(result.mission_run_id)
    after = json.dumps(storage.get_run(result.mission_run_id).model_dump(mode="json"), sort_keys=True)

    assert first == second
    assert first.status == "FAIL"
    assert rule(first, "historical_integrity").status == "FAIL"
    assert before == after


def test_resume_lineage_is_checked_without_parent_aggregation():
    parent, parent_events = fixture()
    checkpoint_id = uuid4()
    parent = parent.model_copy(update={"checkpoint_ids": [checkpoint_id]})
    child, child_events = fixture()
    child = child.model_copy(update={
        "mission_id": parent.mission_id,
        "resumed_from_run_id": parent.mission_run_id,
        "resumed_from_checkpoint_id": checkpoint_id,
    })
    child_events = [item.model_copy(update={"mission_id": parent.mission_id}) for item in child_events]
    checkpoint = CheckpointState(
        mission_run_id=parent.mission_run_id,
        current_agent_run_id=parent.pm_agent_run_id,
        current_step="pm",
        agent_state=AgentState(mission_run_id=parent.mission_run_id, agent_run_id=parent.pm_agent_run_id, role=Role.PM),
        workspace_snapshot_id=uuid4(),
    )
    storage = AppStore()
    save(storage, parent, parent_events)
    save(storage, child, child_events)
    storage.save_checkpoint(checkpoint_id, checkpoint)

    report = RunEvaluationService(storage).get(child.mission_run_id)

    assert rule(report, "resume_lineage_integrity").status == "PASS"
    assert report.rules[0].evidence == {}


def test_restart_and_metrics_cross_check_without_provider_or_tool_calls(tmp_path):
    result, events = fixture()
    database = tmp_path / "evaluation.db"
    storage = SQLiteStore(database)
    save(storage, result, events)
    first = RunEvaluationService(storage).get(result.mission_run_id)
    storage.close()

    restarted = SQLiteStore(database)
    second = RunEvaluationService(restarted).get(result.mission_run_id)
    metrics = RunMetricsService(restarted).get(result.mission_run_id)
    replay = ReplayService(restarted).inspect(result.mission_run_id)
    restarted.close()

    assert first == second
    assert rule(second, "historical_integrity").evidence["value"] == metrics.integrity_status
    assert rule(second, "qa_final_test_outcome").evidence["value"] == replay.evidence_summary["run_tests"][-1]["exit_code"]


def test_model_snapshot_absence_is_unknown_for_historical_compatibility():
    result, events = fixture()
    manifest = result.execution_manifest.model_copy(update={"model_snapshot": None})
    result = result.model_copy(update={"execution_manifest": manifest})
    storage = AppStore()
    save(storage, result, events)

    report = RunEvaluationService(storage).get(result.mission_run_id)

    assert rule(report, "model_snapshot_present").status == "UNKNOWN"
    assert "credential_ref" not in report.model_dump_json()


def test_unknown_run_raises_existing_not_found_error():
    try:
        RunEvaluationService(AppStore()).get(uuid4())
    except RunNotFoundError:
        pass
    else:
        raise AssertionError("missing run should raise RunNotFoundError")
