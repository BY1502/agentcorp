import hashlib
from pathlib import Path
from uuid import UUID

from app.domain.models import ExecutionManifest, TraceEvent
from app.domain.replay import (
    CheckpointInspection,
    ReplayInspection,
    ReplayIntegrity,
    ReplayTimelineItem,
    ToolInspection,
    WorkspaceFileInspection,
    WorkspaceSnapshotInspection,
)
from app.tracing.recorder import sanitize


_SKIP_KEYS = {
    "api_key",
    "authorization",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "credential",
    "credential_ref",
    "reasoning",
    "reasoning_content",
    "raw_request",
    "raw_response",
    "provider_request",
    "provider_response",
}


class RunNotFoundError(LookupError):
    pass


def _safe(value):
    if isinstance(value, dict):
        return {
            key: _safe(item)
            for key, item in value.items()
            if str(key).lower() not in _SKIP_KEYS
        }
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        return sanitize(value)
    return value


def _role_map(events: list[TraceEvent]) -> dict[UUID, str]:
    return {
        event.agent_run_id: str(event.payload["role"])
        for event in events
        if event.agent_run_id and event.event_type == "model_request" and event.payload.get("role")
    }


def _attempt_map(events: list[TraceEvent]) -> dict[UUID, int]:
    return {
        event.agent_run_id: int(event.payload["recovery_attempt"])
        for event in events
        if event.agent_run_id
        and event.event_type == "agent_started"
        and event.payload.get("recovery_attempt") is not None
    }


def _summary(event: TraceEvent, role: str | None) -> str:
    payload = event.payload
    if event.event_type == "tool_call":
        return f"{role or 'agent'} called {payload.get('name', 'unknown tool')}"
    if event.event_type == "tool_result":
        return f"{role or 'agent'} received tool result"
    if event.event_type == "mission_finished":
        return f"mission {payload.get('status', 'finished')}"
    return event.event_type.replace("_", " ")


def _timeline(events: list[TraceEvent]):
    roles = _role_map(events)
    attempts = _attempt_map(events)
    timeline = []
    for event in events:
        payload = event.payload
        metadata = payload.get("metadata") or {}
        role = roles.get(event.agent_run_id)
        timeline.append(
            ReplayTimelineItem(
                sequence=event.sequence,
                event_type=event.event_type,
                agent_run_id=event.agent_run_id,
                role=role,
                tool_name=payload.get("name") or payload.get("tool_name"),
                success=payload.get("success") if event.event_type == "tool_result" else None,
                exit_code=metadata.get("exit_code") if event.event_type == "tool_result" else None,
                recovery_attempt=payload.get("recovery_attempt", attempts.get(event.agent_run_id)),
                status=payload.get("status") if event.event_type == "mission_finished" else None,
                summary=_summary(event, role),
            )
        )
    return timeline, roles, attempts


def _tools(events: list[TraceEvent], roles, attempts):
    pending: dict[UUID | None, list[tuple[TraceEvent, str]]] = {}
    summaries = []
    for event in events:
        payload = event.payload
        agent_id = event.agent_run_id
        if event.event_type == "tool_call":
            name = str(payload.get("name", "unknown"))
            pending.setdefault(agent_id, []).append((event, name))
        elif event.event_type == "tool_result":
            name = str(payload.get("tool_name", ""))
            calls = pending.get(agent_id, [])
            match_index = next((i for i, (_, call_name) in enumerate(calls) if call_name == name), None)
            call = calls.pop(match_index)[0] if match_index is not None else None
            if call is not None:
                metadata = payload.get("metadata") or {}
                summaries.append(
                    ToolInspection(
                        sequence=call.sequence,
                        tool_name=name,
                        agent_run_id=agent_id,
                        role=roles.get(agent_id),
                        recovery_attempt=attempts.get(agent_id),
                        success=payload.get("success"),
                        exit_code=metadata.get("exit_code"),
                    )
                )
    return summaries


def _snapshot_inspection(snapshot_id: UUID, storage, issues: list[str]):
    snapshot = storage.get_workspace_snapshot(snapshot_id)
    if snapshot is None:
        issues.append(f"workspace_snapshot_missing:{snapshot_id}")
        return None
    root = Path(snapshot.location)
    if not root.is_dir():
        issues.append(f"workspace_snapshot_location_missing:{snapshot_id}")
        return WorkspaceSnapshotInspection(snapshot_id=snapshot_id)
    files = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append(
            WorkspaceFileInspection(
                path=str(path.relative_to(root)),
                size=path.stat().st_size,
                sha256=digest,
            )
        )
    return WorkspaceSnapshotInspection(snapshot_id=snapshot_id, files=files)


def _checkpoints(result, events, storage, issues):
    checkpoint_events = {
        str(event.payload.get("checkpoint_id")): event.sequence
        for event in events
        if event.event_type == "checkpoint_created" and event.payload.get("checkpoint_id")
    }
    records = []
    for checkpoint_id in result.checkpoint_ids:
        state = storage.get_checkpoint(checkpoint_id)
        if state is None:
            issues.append(f"checkpoint_missing:{checkpoint_id}")
            records.append(CheckpointInspection(checkpoint_id=checkpoint_id))
            continue
        records.append(
            CheckpointInspection(
                checkpoint_id=checkpoint_id,
                event_sequence=checkpoint_events.get(str(checkpoint_id)),
                current_step=state.current_step,
                current_agent_run_id=state.current_agent_run_id,
                workspace_snapshot_id=state.workspace_snapshot_id,
                workspace_snapshot=_snapshot_inspection(state.workspace_snapshot_id, storage, issues),
            )
        )
    sequences = [record.event_sequence for record in records if record.event_sequence is not None]
    valid = len(sequences) == len(set(sequences)) and sequences == sorted(sequences)
    if len(sequences) != len(records):
        issues.append("checkpoint_event_missing")
    if not valid:
        issues.append("checkpoint_order_invalid")
    return records, valid


class ReplayService:
    """Read-only reconstruction over persisted historical data."""

    def __init__(self, storage):
        self.storage = storage

    def inspect(self, run_id: UUID) -> ReplayInspection:
        result = self.storage.get_run(run_id)
        if result is None:
            raise RunNotFoundError(run_id)
        events = self.storage.list_events(run_id)
        issues: list[str] = []
        sequences = [event.sequence for event in events]
        if len(sequences) != len(set(sequences)):
            issues.append("event_sequence_duplicate")
        if sequences != sorted(sequences):
            issues.append("event_sequence_out_of_order")
        if any(right - left != 1 for left, right in zip(sequences, sequences[1:])):
            issues.append("event_sequence_gap")
        if result.event_count != len(events):
            issues.append("event_count_mismatch")

        timeline, roles, attempts = _timeline(events)
        tools = _tools(events, roles, attempts)
        checkpoints, checkpoint_order_valid = _checkpoints(result, events, self.storage, issues)
        final_events = [event for event in events if event.event_type == "mission_finished"]
        final_event = final_events[-1] if final_events else None
        if final_event is None:
            issues.append("mission_finished_missing")
        if events and events[-1].event_type != "mission_finished":
            issues.append("mission_finished_not_last")
        status_consistent = bool(final_event and final_event.payload.get("status") == result.status)
        if final_event and not status_consistent:
            issues.append("mission_status_mismatch")

        agent_summary: dict[str, dict[str, int]] = {}
        for event in events:
            if event.event_type != "model_request":
                continue
            role = str(event.payload.get("role", "unknown"))
            summary = agent_summary.setdefault(role, {"model_calls": 0, "agent_runs": 0})
            summary["model_calls"] += 1
        for role in set(roles.values()):
            agent_summary.setdefault(role, {"model_calls": 0, "agent_runs": 0})["agent_runs"] = len(
                {agent_id for agent_id, agent_role in roles.items() if agent_role == role}
            )

        recovery_started = sum(event.event_type == "recovery_started" for event in events)
        recovery_exhausted = any(event.event_type == "recovery_exhausted" for event in events)
        evidence_conflict = any(
            event.event_type == "validation_error" and event.payload.get("reason") == "qa_evidence_conflict"
            for event in events
        )
        evidence = [
            {
                "sequence": item.sequence,
                "exit_code": item.exit_code,
                "success": item.success,
            }
            for item in tools
            if item.tool_name == "run_test"
        ]
        manifest = ExecutionManifest.model_validate(_safe(result.execution_manifest.model_dump(mode="json")))
        return ReplayInspection(
            run_id=result.mission_run_id,
            mission_id=result.mission_id,
            final_status=result.status,
            execution_manifest=manifest,
            timeline=timeline,
            agent_summary=agent_summary,
            tool_summary=tools,
            checkpoints=checkpoints,
            recovery_summary={
                "recovery_count": result.recovery_count,
                "recovery_started": recovery_started,
                "exhausted": recovery_exhausted,
            },
            evidence_summary={"run_tests": evidence, "evidence_conflict": evidence_conflict},
            final_result=_safe(result.final_qa_result),
            integrity=ReplayIntegrity(
                event_sequence_valid=not any(
                    issue in issues
                    for issue in ("event_sequence_duplicate", "event_sequence_out_of_order")
                ),
                checkpoint_order_valid=checkpoint_order_valid,
                manifest_present=result.execution_manifest is not None,
                final_event_present=final_event is not None,
                status_consistent=status_consistent,
            ),
            consistency_issues=issues,
        )
