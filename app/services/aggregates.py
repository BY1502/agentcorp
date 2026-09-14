from statistics import mean, median

from app.domain.aggregates import ModelAggregate, RunAggregate
from app.domain.evaluation import EvaluationRuleStatus
from app.services.evaluation import RunEvaluationService
from app.services.metrics import RunMetricsService


_TERMINAL_STATUSES = {"PASSED", "FAILED", "EXHAUSTED"}
_QA_RULE_ID = "qa_final_test_outcome"


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


class RunAggregateService:
    """Read-only aggregate views over persisted runs."""

    def __init__(self, storage, metrics_service=None, evaluation_service=None):
        self.storage = storage
        self.metrics = metrics_service or RunMetricsService(storage)
        self.evaluation = evaluation_service or RunEvaluationService(storage, self.metrics)

    def _runs(self, model_id=None, provider_type=None, model_name=None, status=None):
        runs = sorted(self.storage.list_runs(), key=lambda item: str(item.mission_run_id))
        return [
            run
            for run in runs
            if (model_id is None or getattr(run.execution_manifest.model_snapshot, "model_id", None) == model_id)
            and (provider_type is None or getattr(run.execution_manifest.model_snapshot, "provider_type", None) == provider_type)
            and (model_name is None or getattr(run.execution_manifest.model_snapshot, "model_name", None) == model_name)
            and (status is None or run.status == status)
        ]

    def _read(self, runs):
        readings = []
        unavailable = []
        for run in runs:
            try:
                readings.append((run, self.metrics.get(run.mission_run_id), self.evaluation.get(run.mission_run_id)))
            except Exception:
                unavailable.append(run)
        return readings, unavailable

    def _aggregate(self, readings, run_count, unavailable, model_key=None):
        metrics = [item[1] for item in readings]
        evaluations = [item[2] for item in readings]
        status_counts = {}
        durations = []
        tool_calls_by_name = {}
        qa_pass = qa_fail = qa_unknown = 0
        evaluation_pass = evaluation_fail = evaluation_incomplete = 0
        for item in metrics:
            status_counts[item.status] = status_counts.get(item.status, 0) + 1
            if item.duration_ms is not None:
                durations.append(item.duration_ms)
            for name, count in item.tool_calls_by_name.items():
                tool_calls_by_name[name] = tool_calls_by_name.get(name, 0) + count
        for report in evaluations:
            if report.status == "PASS":
                evaluation_pass += 1
            elif report.status == "FAIL":
                evaluation_fail += 1
            else:
                evaluation_incomplete += 1
            qa_rule = next((rule for rule in report.rules if rule.rule_id == _QA_RULE_ID), None)
            if qa_rule is None or qa_rule.status == EvaluationRuleStatus.UNKNOWN:
                qa_unknown += 1
            elif qa_rule.status == EvaluationRuleStatus.PASS:
                qa_pass += 1
            elif qa_rule.status == EvaluationRuleStatus.FAIL:
                qa_fail += 1

        terminal = sum(item.status in _TERMINAL_STATUSES for item in metrics)
        passed = status_counts.get("PASSED", 0)
        tool_success = sum(item.tool_success_count for item in metrics)
        tool_failure = sum(item.tool_failure_count for item in metrics)
        qa_samples = qa_pass + qa_fail
        evaluation_samples = evaluation_pass + evaluation_fail + evaluation_incomplete
        values = {
            "run_count": run_count,
            "evaluated_run_count": len(metrics),
            "unavailable_run_count": unavailable,
            "status_counts": dict(sorted(status_counts.items())),
            "terminal_run_count": terminal,
            "non_terminal_run_count": len(metrics) - terminal,
            "passed_count": passed,
            "failed_count": status_counts.get("FAILED", 0),
            "terminal_pass_rate": _rate(passed, terminal),
            "qa_final_test_pass_count": qa_pass,
            "qa_final_test_fail_count": qa_fail,
            "qa_final_test_unknown_count": qa_unknown,
            "qa_final_test_sample_count": qa_samples,
            "qa_final_test_pass_rate": _rate(qa_pass, qa_samples),
            "recovery_run_count": sum(item.recovery_count > 0 for item in metrics),
            "total_recovery_attempts": sum(item.recovery_count for item in metrics),
            "recovered_to_pass_count": sum(item.recovery_count > 0 and item.status == "PASSED" for item in metrics),
            "qa_evidence_conflict_run_count": sum(item.qa_evidence_conflict_count > 0 for item in metrics),
            "tool_call_count": sum(item.tool_call_count for item in metrics),
            "tool_success_count": tool_success,
            "tool_failure_count": tool_failure,
            "tool_calls_by_name": dict(sorted(tool_calls_by_name.items())),
            "tool_failure_rate": _rate(tool_failure, tool_success + tool_failure),
            "approval_run_count": sum(item.approval_count > 0 for item in metrics),
            "approval_count": sum(item.approval_count for item in metrics),
            "approval_pending_count": sum(item.approval_pending_count for item in metrics),
            "approved_count": sum(item.approval_approved_count for item in metrics),
            "rejected_count": sum(item.approval_rejected_count for item in metrics),
            "expired_count": sum(item.approval_expired_count for item in metrics),
            "event_count": sum(item.event_count for item in metrics),
            "checkpoint_count": sum(item.checkpoint_count for item in metrics),
            "model_call_count": sum(item.model_call_count for item in metrics),
            "duration_sample_count": len(durations),
            "duration_avg_ms": mean(durations) if durations else None,
            "duration_median_ms": median(durations) if durations else None,
            "duration_min_ms": min(durations) if durations else None,
            "duration_max_ms": max(durations) if durations else None,
            "evaluation_pass_count": evaluation_pass,
            "evaluation_fail_count": evaluation_fail,
            "evaluation_incomplete_count": evaluation_incomplete,
            "evaluation_sample_count": evaluation_samples,
            "evaluation_pass_rate": _rate(evaluation_pass, evaluation_samples),
        }
        if model_key is not None:
            values.update({
                "model_id": model_key[0],
                "provider_type": model_key[1],
                "model_name": model_key[2],
                "group_key": model_key,
            })
            return ModelAggregate(**values)
        return RunAggregate(**values)

    def aggregate(self, model_id=None, provider_type=None, model_name=None, status=None) -> RunAggregate:
        runs = self._runs(model_id, provider_type, model_name, status)
        readings, unavailable = self._read(runs)
        return self._aggregate(readings, len(runs), len(unavailable))

    def model_aggregates(self, model_id=None, provider_type=None, model_name=None, status=None) -> list[ModelAggregate]:
        runs = self._runs(model_id, provider_type, model_name, status)
        readings, unavailable = self._read(runs)
        grouped = {}
        for reading in readings:
            snapshot = reading[1]
            key = (snapshot.model_id, snapshot.provider_type, snapshot.model_name)
            grouped.setdefault(key, [[], 0])[0].append(reading)
        for run in unavailable:
            snapshot = run.execution_manifest.model_snapshot
            key = (
                snapshot.model_id if snapshot else None,
                snapshot.provider_type if snapshot else None,
                snapshot.model_name if snapshot else None,
            )
            grouped.setdefault(key, [[], 0])[1] += 1
        return [
            self._aggregate(group[0], len(group[0]) + group[1], group[1], key)
            for key, group in sorted(grouped.items(), key=lambda item: tuple(value or "" for value in item[0]))
        ]
