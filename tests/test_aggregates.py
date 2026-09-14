import json
from datetime import timedelta

from app.domain.models import ModelConfig, ModelExecutionSnapshot
from app.domain.policy import ApprovalStatus
from app.persistence.sqlite import SQLiteStore
from app.services.aggregates import RunAggregateService
from app.services.store import AppStore

from test_evaluation import approval_flow, renumber
from test_metrics import BASE_TIME, approval, fixture, save


def variant(result, model_id, provider_type, model_name):
    snapshot = ModelExecutionSnapshot.from_config(ModelConfig(
        model_id=model_id,
        provider_type=provider_type,
        model_name=model_name,
        base_url="http://localhost:1234/v1",
        timeout=30,
    ))
    return result.model_copy(update={
        "execution_manifest": result.execution_manifest.model_copy(update={"model_snapshot": snapshot}),
    })


def timed(result, events, milliseconds):
    events[0] = events[0].model_copy(update={"timestamp": BASE_TIME})
    events[-1] = events[-1].model_copy(update={"timestamp": BASE_TIME + timedelta(milliseconds=milliseconds)})
    return result, events


def aggregate(*records):
    storage = AppStore()
    for result, events, *approvals in records:
        save(storage, result, events, approvals[0] if approvals else ())
    return storage, RunAggregateService(storage)


def test_empty_dataset_has_null_rates():
    report = RunAggregateService(AppStore()).aggregate()

    assert report.run_count == 0
    assert report.terminal_pass_rate is None
    assert report.qa_final_test_pass_rate is None
    assert report.duration_sample_count == 0


def test_mixed_statuses_use_terminal_denominator_and_keep_unknown_qa_outcome_out():
    passed = fixture()
    failed = fixture(status="FAILED", recovery_count=1)
    failed[1][12] = failed[1][12].model_copy(update={"payload": {"tool_name": "run_test", "success": False, "metadata": {"exit_code": 1}}})
    failed[1][-1] = failed[1][-1].model_copy(update={"payload": {"status": "FAILED"}})
    waiting_result, waiting_events = fixture(status="WAITING_APPROVAL")
    waiting_events = waiting_events[:-1]
    waiting_result = waiting_result.model_copy(update={"event_count": len(waiting_events)})
    missing_result, missing_events = fixture()
    missing_result, missing_events = renumber(missing_result, missing_events[:12] + missing_events[13:])
    storage, service = aggregate(passed, failed, (waiting_result, waiting_events), (missing_result, missing_events))

    report = service.aggregate()

    assert report.run_count == 4
    assert report.status_counts == {"FAILED": 1, "PASSED": 2, "WAITING_APPROVAL": 1}
    assert report.terminal_run_count == 3
    assert report.non_terminal_run_count == 1
    assert report.passed_count == 2
    assert report.failed_count == 1
    assert report.terminal_pass_rate == 2 / 3
    assert report.qa_final_test_pass_count == 2
    assert report.qa_final_test_fail_count == 1
    assert report.qa_final_test_unknown_count == 1
    assert report.qa_final_test_sample_count == 3
    assert report.qa_final_test_pass_rate == 2 / 3
    del storage


def test_recovery_and_observability_totals_are_summed_from_metrics():
    direct, direct_events = fixture()
    recovered, recovered_events = fixture(recovery_count=1)
    exhausted, exhausted_events = fixture(status="EXHAUSTED", recovery_count=1)
    exhausted_events[-1] = exhausted_events[-1].model_copy(update={"payload": {"status": "EXHAUSTED"}})
    storage, service = aggregate((direct, direct_events), (recovered, recovered_events), (exhausted, exhausted_events))

    report = service.aggregate()

    assert report.recovery_run_count == 2
    assert report.total_recovery_attempts == 2
    assert report.recovered_to_pass_count == 1
    assert report.tool_call_count == 6
    assert report.tool_success_count == 6
    assert report.tool_failure_count == 0
    assert report.tool_calls_by_name == {"read_file": 3, "run_test": 3}
    assert report.tool_failure_rate == 0.0
    assert report.event_count == 48
    assert report.checkpoint_count == 6
    assert report.model_call_count == 12
    del storage


def test_duration_statistics_exclude_missing_duration():
    records = []
    for milliseconds in (1000, 2000, 4000):
        result, events = timed(*fixture(), milliseconds)
        records.append((result, events))
    waiting_result, waiting_events = fixture(status="WAITING_APPROVAL")
    records.append((waiting_result.model_copy(update={"event_count": len(waiting_events) - 1}), waiting_events[:-1]))
    _, service = aggregate(*records)

    report = service.aggregate()

    assert report.duration_sample_count == 3
    assert report.duration_avg_ms == 7000 / 3
    assert report.duration_median_ms == 2000
    assert report.duration_min_ms == 1000
    assert report.duration_max_ms == 4000


def test_model_groups_use_frozen_snapshot_identity_and_deterministic_order():
    first_result, first_events = fixture()
    second_result, second_events = fixture()
    third_result, third_events = fixture()
    first = variant(first_result, "local-qwen", "fake", "qwen3-8b")
    second = variant(second_result, "local-qwen", "fake", "qwen3-14b")
    third = variant(third_result, "local-qwen", "lmstudio", "qwen3-8b")
    _, service = aggregate((first, first_events), (second, second_events), (third, third_events))

    groups = service.model_aggregates()

    assert [item.group_key for item in groups] == [
        ("local-qwen", "fake", "qwen3-14b"),
        ("local-qwen", "fake", "qwen3-8b"),
        ("local-qwen", "lmstudio", "qwen3-8b"),
    ]
    assert all(item.run_count == 1 for item in groups)
    assert all(item.model_id == "local-qwen" for item in groups)
    assert all("base_url" not in item.model_dump_json() for item in groups)


def test_filters_are_exact_and_unknown_status_returns_empty_dataset():
    result, events = fixture()
    result = variant(result, "selected", "fake", "model-a")
    other_result, other_events = fixture()
    other_result = variant(other_result, "selected", "lmstudio", "model-b")
    _, service = aggregate((result, events), (other_result, other_events))

    assert service.aggregate(model_id="selected", provider_type="fake", model_name="model-a").run_count == 1
    assert service.aggregate(provider_type="does-not-exist").run_count == 0
    assert service.model_aggregates(model_name="model-b")[0].provider_type == "lmstudio"
    assert service.aggregate(status="FUTURE_STATUS").run_count == 0


def test_approval_aggregates_are_observational():
    records = []
    for status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED):
        result, events = fixture()
        records.append((result, events, [approval(result.mission_run_id, status, BASE_TIME, BASE_TIME + timedelta(milliseconds=10))]))
    _, service = aggregate(*records)

    report = service.aggregate()

    assert report.approval_run_count == 3
    assert report.approval_count == 3
    assert report.approved_count == 1
    assert report.rejected_count == 1
    assert report.expired_count == 1


def test_conflict_is_counted_per_run_and_corruption_is_not_silently_dropped():
    conflict_result, conflict_events = fixture()
    conflict = conflict_events[0].model_copy(update={"event_type": "validation_error", "payload": {"reason": "qa_evidence_conflict"}})
    conflict_result, conflict_events = renumber(conflict_result, [*conflict_events[:-1], conflict, conflict_events[-1]])
    corrupt_result, corrupt_events = fixture()
    corrupt_events[3] = corrupt_events[3].model_copy(update={"sequence": 40})
    _, service = aggregate((conflict_result, conflict_events), (corrupt_result, corrupt_events))

    report = service.aggregate()

    assert report.qa_evidence_conflict_run_count == 1
    assert report.evaluated_run_count == 2
    assert report.unavailable_run_count == 0
    assert report.evaluation_fail_count == 2


def test_pending_approval_and_resumed_child_are_counted_as_independent_runs():
    pending, pending_events, pending_approval = approval_flow(*fixture(), ApprovalStatus.PENDING)
    child, child_events = fixture()
    child = child.model_copy(update={"resumed_from_run_id": pending.mission_run_id, "resumed_from_checkpoint_id": pending.checkpoint_ids[0]})
    storage, service = aggregate((pending, pending_events, [pending_approval]), (child, child_events))

    report = service.aggregate()

    assert report.run_count == 2
    assert report.approval_count == 1
    assert report.approval_pending_count == 1
    assert report.non_terminal_run_count == 1
    del storage


def test_restart_repeat_and_security_are_stable_without_registry_or_mutation(tmp_path):
    result, events = fixture()
    events[12] = events[12].model_copy(update={"payload": {"tool_name": "run_test", "success": True, "output": "reasoning_content=secret", "metadata": {"exit_code": 0}}})
    database = tmp_path / "aggregate.db"
    storage = SQLiteStore(database)
    save(storage, result, events)
    before = json.dumps({"run": storage.get_run(result.mission_run_id).model_dump(mode="json"), "events": [item.model_dump(mode="json") for item in storage.list_events(result.mission_run_id)]}, sort_keys=True)
    service = RunAggregateService(storage)
    first = service.aggregate()
    second = service.aggregate()
    storage.close()

    restarted = SQLiteStore(database)
    after = json.dumps({"run": restarted.get_run(result.mission_run_id).model_dump(mode="json"), "events": [item.model_dump(mode="json") for item in restarted.list_events(result.mission_run_id)]}, sort_keys=True)
    third = RunAggregateService(restarted).aggregate()
    restarted.close()

    assert first == second == third
    assert before == after
    serialized = first.model_dump_json().lower()
    assert all(term not in serialized for term in ("secret", "reasoning", "stdout", "stderr", "base_url"))
