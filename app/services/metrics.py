from datetime import datetime
from uuid import UUID

from app.domain.metrics import RunMetrics
from app.services.replay import RunNotFoundError


def _timestamp(value) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _status(value) -> str:
    return getattr(value, "value", str(value))


def _elapsed_ms(start: datetime, end: datetime) -> float | None:
    try:
        return round((end - start).total_seconds() * 1000, 2)
    except (TypeError, ValueError):
        return None


class RunMetricsService:
    """Read-only metrics derived from one persisted run and its records."""

    def __init__(self, storage):
        self.storage = storage

    def get(self, run_id: UUID) -> RunMetrics:
        result = self.storage.get_run(run_id)
        if result is None:
            raise RunNotFoundError(run_id)
        event_read_error = False
        try:
            events = self.storage.list_events(run_id)
        except Exception:
            events = []
            event_read_error = True
        approval_read_error = False
        try:
            approvals = self.storage.list_approvals(run_id)
        except Exception:
            approvals = []
            approval_read_error = True

        integrity_issues = []
        if event_read_error:
            integrity_issues.append("event_data_invalid")
        if approval_read_error:
            integrity_issues.append("approval_data_invalid")
        sequences = [event.sequence for event in events]
        if (
            len(sequences) != len(set(sequences))
            or sequences != sorted(sequences)
            or any(right - left != 1 for left, right in zip(sequences, sequences[1:]))
            or result.event_count != len(events)
        ):
            integrity_issues.append("event_integrity")

        started_event = next((event for event in events if event.event_type == "mission_started"), None)
        finished_events = [event for event in events if event.event_type == "mission_finished"]
        finished_event = finished_events[-1] if finished_events else None
        started_at = _timestamp(getattr(started_event, "timestamp", None)) if started_event else None
        finished_at = _timestamp(getattr(finished_event, "timestamp", None)) if finished_event else None
        if started_event and started_at is None:
            integrity_issues.append("started_timestamp_invalid")
        if finished_event and finished_at is None:
            integrity_issues.append("finished_timestamp_invalid")

        duration_ms = None
        if started_at and finished_at:
            delta = _elapsed_ms(started_at, finished_at)
            if delta is None:
                integrity_issues.append("timestamp_incompatible")
            elif delta < 0:
                integrity_issues.append("timestamp_order")
            else:
                duration_ms = delta
        if result.status != "WAITING_APPROVAL" and finished_event is None:
            integrity_issues.append("mission_finished_missing")
        if finished_event and finished_event.payload.get("status") != result.status:
            integrity_issues.append("mission_status_mismatch")

        role_by_agent = {
            event.agent_run_id: str(event.payload.get("role"))
            for event in events
            if event.event_type == "model_request"
            and event.agent_run_id
            and event.payload.get("role")
        }
        agent_starts = {}
        agent_durations: dict[str, float] = {}
        for event in events:
            if not event.agent_run_id:
                continue
            if event.event_type == "agent_started":
                timestamp = _timestamp(getattr(event, "timestamp", None))
                if timestamp:
                    agent_starts[event.agent_run_id] = timestamp
            elif event.event_type == "agent_finished" and event.agent_run_id in agent_starts:
                timestamp = _timestamp(getattr(event, "timestamp", None))
                started = agent_starts.pop(event.agent_run_id)
                if timestamp:
                    delta = _elapsed_ms(started, timestamp)
                    if delta is not None and delta >= 0 and role_by_agent.get(event.agent_run_id):
                        role = role_by_agent[event.agent_run_id]
                        agent_durations[role] = round(agent_durations.get(role, 0) + delta, 2)

        model_snapshot = result.execution_manifest.model_snapshot
        tool_calls = [event for event in events if event.event_type == "tool_call"]
        tool_results = [event for event in events if event.event_type == "tool_result"]
        tool_calls_by_name: dict[str, int] = {}
        for event in tool_calls:
            name = str(event.payload.get("name", "unknown"))
            tool_calls_by_name[name] = tool_calls_by_name.get(name, 0) + 1
        tool_success_count = sum(event.payload.get("success") is True for event in tool_results)
        tool_failure_count = sum(event.payload.get("success") is False for event in tool_results)

        qa_ids = set(result.qa_agent_run_ids)
        qa_results = [event for event in tool_results if event.agent_run_id in qa_ids]
        qa_run_tests = [event for event in qa_results if event.payload.get("tool_name") == "run_test"]
        qa_pass_count = sum(
            (event.payload.get("metadata") or {}).get("exit_code") == 0
            for event in qa_run_tests
        )
        qa_fail_count = sum(
            isinstance((event.payload.get("metadata") or {}).get("exit_code"), int)
            and (event.payload.get("metadata") or {}).get("exit_code") != 0
            for event in qa_run_tests
        )

        approval_waits = []
        approval_counts = {"PENDING": 0, "APPROVED": 0, "REJECTED": 0, "EXPIRED": 0}
        for approval in approvals:
            status = _status(approval.status)
            if status in approval_counts:
                approval_counts[status] += 1
            created_at = _timestamp(getattr(approval, "created_at", None))
            decided_at = _timestamp(getattr(approval, "decided_at", None))
            if status != "PENDING" and created_at and decided_at:
                delta = _elapsed_ms(created_at, decided_at)
                if delta is not None and delta >= 0:
                    approval_waits.append(delta)
                else:
                    integrity_issues.append("approval_timestamp_invalid")

        integrity_status = "invalid" if integrity_issues else "unknown"
        if not integrity_issues and started_event and started_at:
            integrity_status = "valid"

        return RunMetrics(
            run_id=result.mission_run_id,
            mission_id=result.mission_id,
            status=result.status,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            agent_durations_ms=agent_durations,
            model_id=model_snapshot.model_id if model_snapshot else None,
            provider_type=model_snapshot.provider_type if model_snapshot else None,
            model_name=model_snapshot.model_name if model_snapshot else None,
            model_call_count=sum(event.event_type == "model_request" for event in events),
            tool_call_count=len(tool_calls),
            tool_success_count=tool_success_count,
            tool_failure_count=tool_failure_count,
            tool_calls_by_name=tool_calls_by_name,
            qa_run_test_count=len(qa_run_tests),
            qa_test_pass_count=qa_pass_count,
            qa_test_fail_count=qa_fail_count,
            qa_evidence_conflict_count=sum(
                event.event_type == "validation_error"
                and event.payload.get("reason") == "qa_evidence_conflict"
                for event in events
            ),
            required_tool_correction_count=sum(
                event.event_type == "validation_error"
                and event.payload.get("reason") == "required_tool_missing"
                for event in events
            ),
            recovery_count=result.recovery_count,
            approval_count=len(approvals),
            approval_pending_count=approval_counts["PENDING"],
            approval_approved_count=approval_counts["APPROVED"],
            approval_rejected_count=approval_counts["REJECTED"],
            approval_expired_count=approval_counts["EXPIRED"],
            approval_wait_ms=round(sum(approval_waits), 2) if approval_waits else None,
            checkpoint_count=len(result.checkpoint_ids),
            event_count=len(events),
            resumed_from_run_id=result.resumed_from_run_id,
            resumed_from_checkpoint_id=result.resumed_from_checkpoint_id,
            is_resumed_run=result.resumed_from_run_id is not None,
            workspace_changed_file_count=len(result.changed_files),
            integrity_status=integrity_status,
        )
