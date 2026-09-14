from typing import Any
from uuid import UUID

from app.domain.evaluation import EvaluationRuleResult, EvaluationRuleStatus, RunEvaluation
from app.services.metrics import RunMetricsService
from app.services.replay import RunNotFoundError


_TERMINAL_STATUSES = {"PASSED", "FAILED", "EXHAUSTED"}
_DECISION_EVENTS = {
    "APPROVED": "approval_approved",
    "REJECTED": "approval_rejected",
    "EXPIRED": "approval_expired",
}


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _event_approval_id(event) -> str | None:
    value = event.payload.get("approval_id")
    return str(value) if value is not None else None


def _tool_matches(event, approval) -> bool:
    payload = event.payload
    if payload.get("approval_id") is not None:
        return str(payload.get("approval_id")) == str(approval.approval_id)
    return (
        payload.get("tool_name") == approval.tool_call.tool_name
        and (
            payload.get("call_id") == approval.tool_call.call_id
            or payload.get("arguments_digest") == approval.tool_call.arguments_digest
        )
    )


class RunEvaluationService:
    """Read-only, deterministic rules derived from persisted run records."""

    def __init__(self, storage, metrics_service: RunMetricsService | None = None):
        self.storage = storage
        self.metrics = metrics_service or RunMetricsService(storage)

    def _rule(self, rule_id, status, message, evidence=None):
        return EvaluationRuleResult(
            rule_id=rule_id,
            status=status,
            message=message,
            evidence=evidence or {},
        )

    def _qa_evidence(self, result, events):
        qa_ids = set(result.qa_agent_run_ids)
        if not qa_ids:
            return None
        candidates = [
            event
            for event in events
            if event.agent_run_id in qa_ids
            and event.event_type == "tool_result"
            and event.payload.get("tool_name") == "run_test"
        ]
        return max(candidates, key=lambda event: event.sequence) if candidates else None

    def _approval_integrity(self, result, approvals, events, approvals_readable):
        if not approvals_readable:
            return EvaluationRuleStatus.UNKNOWN, "approval records could not be read", {}
        if not approvals:
            return EvaluationRuleStatus.NOT_APPLICABLE, "no approvals were recorded", {"approval_count": 0}

        approval_ids = [str(item.approval_id) for item in approvals]
        if len(approval_ids) != len(set(approval_ids)):
            return EvaluationRuleStatus.FAIL, "approval identifiers are duplicated", {"approval_count": len(approvals)}
        known_ids = set(approval_ids)
        audit = {}
        for event in events:
            approval_id = _event_approval_id(event)
            if approval_id is not None and event.event_type.startswith("approval_"):
                if approval_id not in known_ids:
                    return EvaluationRuleStatus.FAIL, "approval event references an unknown approval", {}
                audit.setdefault(approval_id, {}).setdefault(event.event_type, []).append(event)

        unknown = False
        final_events = [event for event in events if event.event_type == "mission_finished"]
        final_sequence = final_events[-1].sequence if final_events else None
        for approval in approvals:
            approval_id = str(approval.approval_id)
            status = str(_value(approval.status))
            if approval.run_id != result.mission_run_id or status not in {"PENDING", *(_DECISION_EVENTS.keys())}:
                return EvaluationRuleStatus.FAIL, "approval ownership or status is invalid", {}
            created_at = getattr(approval, "created_at", None)
            decided_at = getattr(approval, "decided_at", None)
            if not created_at or (status != "PENDING" and not decided_at):
                unknown = True
            if created_at and decided_at and decided_at < created_at:
                return EvaluationRuleStatus.FAIL, "approval timestamps are out of order", {}

            record = audit.get(approval_id, {})
            required = record.get("approval_required", [])
            if not required:
                unknown = True
                continue
            if len(required) != 1:
                return EvaluationRuleStatus.FAIL, "approval requirement is duplicated", {}
            required_payload = required[0].payload
            snapshot = {
                "policy_id": approval.policy_id,
                "policy_version": approval.policy_version,
                "tool_name": approval.tool_call.tool_name,
                "arguments_digest": approval.tool_call.arguments_digest,
                "call_id": approval.tool_call.call_id,
            }
            if any(required_payload.get(key) != value for key, value in snapshot.items()):
                return EvaluationRuleStatus.FAIL, "approval snapshot does not match persisted evidence", {}

            decision_type = _DECISION_EVENTS.get(status)
            decisions = record.get(decision_type, []) if decision_type else []
            if status == "PENDING":
                if any(record.get(name) for name in _DECISION_EVENTS.values()):
                    return EvaluationRuleStatus.FAIL, "pending approval has a decision", {}
                if result.status != "WAITING_APPROVAL":
                    return EvaluationRuleStatus.FAIL, "pending approval is attached to a terminal run", {}
                continue
            if not decisions:
                unknown = True
                continue
            if len(decisions) != 1:
                return EvaluationRuleStatus.FAIL, "approval decision is duplicated", {}
            if any(
                record.get(name)
                for name in _DECISION_EVENTS.values()
                if name != decision_type
            ):
                return EvaluationRuleStatus.FAIL, "approval has an unexpected decision", {}
            decision = decisions[0]
            if decision.sequence <= required[0].sequence:
                return EvaluationRuleStatus.FAIL, "approval decision order is invalid", {}
            if any(decision.payload.get(key) != value for key, value in snapshot.items()):
                return EvaluationRuleStatus.FAIL, "approval decision snapshot does not match", {}

            tool_results = [
                event
                for event in events
                if event.event_type == "tool_result" and _tool_matches(event, approval)
            ]
            if status == "APPROVED":
                if len(tool_results) > 1:
                    return EvaluationRuleStatus.FAIL, "approved tool execution is duplicated", {}
                if not tool_results:
                    unknown = True
                elif tool_results[0].sequence <= decision.sequence:
                    return EvaluationRuleStatus.FAIL, "approved tool execution order is invalid", {}
            elif tool_results:
                return EvaluationRuleStatus.FAIL, "rejected or expired approval was executed", {}
            if status in {"REJECTED", "EXPIRED"} and final_sequence is not None and final_sequence <= decision.sequence:
                return EvaluationRuleStatus.FAIL, "terminal event precedes approval decision", {}

        if unknown:
            return EvaluationRuleStatus.UNKNOWN, "approval lifecycle evidence is incomplete", {"approval_count": len(approvals)}
        return EvaluationRuleStatus.PASS, "approval lifecycle evidence is consistent", {"approval_count": len(approvals)}

    def get(self, run_id: UUID) -> RunEvaluation:
        result = self.storage.get_run(run_id)
        if result is None:
            raise RunNotFoundError(run_id)
        metrics = self.metrics.get(run_id)
        try:
            events = self.storage.list_events(run_id)
            events_readable = True
        except Exception:
            events = []
            events_readable = False
        try:
            approvals = self.storage.list_approvals(run_id)
            approvals_readable = True
        except Exception:
            approvals = []
            approvals_readable = False

        rules = []
        run_status = _value(result.status)
        if run_status in _TERMINAL_STATUSES:
            rules.append(self._rule("run_completed", EvaluationRuleStatus.PASS, "run has a terminal status"))
        elif run_status in {"RUNNING", "WAITING_APPROVAL"}:
            rules.append(self._rule("run_completed", EvaluationRuleStatus.FAIL, "run is not complete"))
        else:
            rules.append(self._rule("run_completed", EvaluationRuleStatus.UNKNOWN, "run status is not recognized"))

        qa_context = bool(result.qa_agent_run_ids)
        evidence = self._qa_evidence(result, events) if events_readable else None
        if not qa_context:
            rules.append(self._rule("qa_evidence_present", EvaluationRuleStatus.UNKNOWN, "QA context is unavailable"))
        elif not events_readable:
            rules.append(self._rule("qa_evidence_present", EvaluationRuleStatus.UNKNOWN, "QA evidence could not be read"))
        elif evidence is None:
            rules.append(self._rule("qa_evidence_present", EvaluationRuleStatus.FAIL, "QA has no run_test evidence"))
        else:
            rules.append(self._rule(
                "qa_evidence_present",
                EvaluationRuleStatus.PASS,
                "final QA run_test evidence is present",
                {"event_sequence": evidence.sequence},
            ))

        if evidence is None or not events_readable:
            rules.append(self._rule("qa_final_test_outcome", EvaluationRuleStatus.UNKNOWN, "final run_test outcome is unavailable"))
        else:
            metadata = evidence.payload.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            exit_code = metadata.get("exit_code")
            success = evidence.payload.get("success")
            if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                outcome = EvaluationRuleStatus.UNKNOWN
                message = "final run_test exit code is unavailable"
            elif exit_code == 0 and success is not False:
                outcome = EvaluationRuleStatus.PASS
                message = "final run_test completed successfully"
            else:
                outcome = EvaluationRuleStatus.FAIL
                message = "final run_test completed with failure"
            rules.append(self._rule(
                "qa_final_test_outcome",
                outcome,
                message,
                {"event_sequence": evidence.sequence, "metric": "exit_code", "value": exit_code},
            ))

        if not qa_context or not events_readable:
            rules.append(self._rule("qa_evidence_consistency", EvaluationRuleStatus.UNKNOWN, "QA evidence consistency is unavailable"))
        elif metrics.qa_evidence_conflict_count:
            rules.append(self._rule(
                "qa_evidence_consistency",
                EvaluationRuleStatus.FAIL,
                "persisted QA evidence conflict was recorded",
                {"metric": "qa_evidence_conflict_count", "value": metrics.qa_evidence_conflict_count},
            ))
        else:
            rules.append(self._rule("qa_evidence_consistency", EvaluationRuleStatus.PASS, "no QA evidence conflict was recorded"))

        config = result.execution_manifest.runtime_config
        limit = None
        if isinstance(config, dict):
            limit = config.get("max_recovery_attempts")
            if limit is None:
                limit = config.get("max_retries")
        actual = result.recovery_count
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 0
            or isinstance(actual, bool)
            or not isinstance(actual, int)
            or actual < 0
        ):
            rules.append(self._rule("recovery_budget", EvaluationRuleStatus.UNKNOWN, "recovery budget is unavailable"))
        else:
            rules.append(self._rule(
                "recovery_budget",
                EvaluationRuleStatus.PASS if actual <= limit else EvaluationRuleStatus.FAIL,
                "recovery count is within the configured budget" if actual <= limit else "recovery count exceeds the configured budget",
                {"metric": "recovery_count", "value": actual, "limit": limit},
            ))

        if result.execution_manifest.model_snapshot is None:
            rules.append(self._rule("model_snapshot_present", EvaluationRuleStatus.UNKNOWN, "model snapshot is absent from this historical record"))
        else:
            rules.append(self._rule("model_snapshot_present", EvaluationRuleStatus.PASS, "frozen model snapshot is present"))

        integrity_status = metrics.integrity_status
        integrity_rule_status = {
            "valid": EvaluationRuleStatus.PASS,
            "invalid": EvaluationRuleStatus.FAIL,
            "unknown": EvaluationRuleStatus.UNKNOWN,
        }[integrity_status]
        rules.append(self._rule(
            "historical_integrity",
            integrity_rule_status,
            f"historical integrity is {integrity_status}",
            {"metric": "integrity_status", "value": integrity_status},
        ))

        approval_status, approval_message, approval_evidence = self._approval_integrity(
            result, approvals, events, approvals_readable
        )
        rules.append(self._rule("approval_integrity", approval_status, approval_message, approval_evidence))

        if result.resumed_from_run_id is None and result.resumed_from_checkpoint_id is None:
            rules.append(self._rule("resume_lineage_integrity", EvaluationRuleStatus.NOT_APPLICABLE, "run is not resumed"))
        elif result.resumed_from_run_id is None or result.resumed_from_checkpoint_id is None:
            rules.append(self._rule("resume_lineage_integrity", EvaluationRuleStatus.UNKNOWN, "resume lineage is incomplete"))
        else:
            try:
                parent = self.storage.get_run(result.resumed_from_run_id)
                checkpoint = (
                    self.storage.get_checkpoint(result.resumed_from_checkpoint_id)
                    if hasattr(self.storage, "get_checkpoint")
                    else True
                )
            except Exception:
                parent = None
                checkpoint = None
            if parent is None or checkpoint is None:
                rules.append(self._rule("resume_lineage_integrity", EvaluationRuleStatus.FAIL, "resume parent or checkpoint is missing"))
            elif parent.mission_id != result.mission_id or result.resumed_from_checkpoint_id not in parent.checkpoint_ids:
                rules.append(self._rule("resume_lineage_integrity", EvaluationRuleStatus.FAIL, "resume lineage does not match the parent run"))
            elif checkpoint is not True and checkpoint.mission_run_id != parent.mission_run_id:
                rules.append(self._rule("resume_lineage_integrity", EvaluationRuleStatus.FAIL, "checkpoint ownership does not match the parent run"))
            else:
                rules.append(self._rule(
                    "resume_lineage_integrity",
                    EvaluationRuleStatus.PASS,
                    "resume parent and checkpoint lineage is consistent",
                    {"parent_run_id": str(result.resumed_from_run_id), "checkpoint_id": str(result.resumed_from_checkpoint_id)},
                ))

        failed = sum(rule.status == EvaluationRuleStatus.FAIL for rule in rules)
        unknown = sum(rule.status == EvaluationRuleStatus.UNKNOWN for rule in rules)
        not_applicable = sum(rule.status == EvaluationRuleStatus.NOT_APPLICABLE for rule in rules)
        passed = sum(rule.status == EvaluationRuleStatus.PASS for rule in rules)
        overall = "FAIL" if failed else "INCOMPLETE" if unknown else "PASS"
        return RunEvaluation(
            run_id=result.mission_run_id,
            status=overall,
            rules=tuple(rules),
            summary=f"{passed} passed, {failed} failed, {unknown} incomplete, {not_applicable} not applicable.",
        )
