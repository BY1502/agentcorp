from datetime import datetime, timezone
from uuid import UUID

from app.domain.experiments import Experiment
from app.domain.models import MissionRecord, TraceEvent

__all__ = ["AppStore", "MissionRecord", "store"]

class AppStore:
    def __init__(self):
        self.missions = {}
        self.runs = {}
        self.events = {}
        self.checkpoints = {}
        self.workspace_snapshots = {}
        self.approvals = {}
        self.experiments = {}

    def save_mission(self, mission): self.missions[mission.id] = MissionRecord(mission.title, mission.fixture, mission.id, mission.version)
    def get_mission(self, mission_id): return self.missions.get(mission_id)
    def save_run(self, result): self.runs[result.mission_run_id] = result.model_copy(deep=True)
    def get_run(self, run_id): return self.runs.get(run_id)
    def list_runs(self): return [self.runs[run_id].model_copy(deep=True) for run_id in sorted(self.runs, key=str)]
    def save_experiment(self, experiment: Experiment) -> None:
        existing = self.experiments.get(experiment.experiment_id)
        if existing and existing.status == "SEALED" and existing != experiment:
            raise ValueError("sealed experiment is immutable")
        self.experiments[experiment.experiment_id] = experiment.model_copy(deep=True)
    def get_experiment(self, experiment_id) -> Experiment | None:
        experiment = self.experiments.get(experiment_id)
        return experiment.model_copy(deep=True) if experiment else None
    def append_events(self, events):
        for event in events:
            self.events.setdefault(event.mission_run_id, []).append(event.model_copy(deep=True))
    def list_events(self, run_id): return [event.model_copy(deep=True) for event in sorted(self.events.get(run_id, []), key=lambda event: event.sequence)]
    def finalize_run(self, result, events):
        self.save_run(result)
        self.append_events(events)
    def save_checkpoint(self, checkpoint_id, state): self.checkpoints[checkpoint_id] = state.model_copy(deep=True)
    def get_checkpoint(self, checkpoint_id):
        state = self.checkpoints.get(checkpoint_id)
        return state.model_copy(deep=True) if state else None
    def save_workspace_snapshot(self, snapshot): self.workspace_snapshots[snapshot.id] = snapshot.model_copy(deep=True)
    def get_workspace_snapshot(self, snapshot_id):
        snapshot = self.workspace_snapshots.get(snapshot_id)
        return snapshot.model_copy(deep=True) if snapshot else None
    def save_approval(self, approval):
        if approval.status == "PENDING" and any(item.run_id == approval.run_id and item.status == "PENDING" for item in self.approvals.values()):
            raise ValueError("run already has a pending approval")
        self.approvals[approval.approval_id] = approval.model_copy(deep=True)
    def get_approval(self, approval_id):
        approval = self.approvals.get(approval_id)
        return approval.model_copy(deep=True) if approval else None
    def list_approvals(self, run_id): return sorted((approval.model_copy(deep=True) for approval in self.approvals.values() if approval.run_id == run_id), key=lambda approval: (approval.created_at, approval.approval_id))
    def transition_approval(self, approval_id, expected_status, new_status, decision_reason=None):
        if expected_status != "PENDING" or new_status not in {"APPROVED", "REJECTED", "EXPIRED"}: return None
        approval = self.approvals.get(approval_id)
        if approval is None or approval.status != expected_status: return None
        updated = approval.model_validate({**approval.model_dump(mode="json"), "status": str(new_status), "decision_reason": decision_reason, "decided_at": datetime.now(timezone.utc)})
        self.approvals[approval_id] = updated
        return updated.model_copy(deep=True)

    def transition_approval_with_event(self, approval_id, expected_status, new_status, event: TraceEvent, decision_reason=None):
        previous = self.approvals.get(approval_id)
        if previous is None or event.mission_run_id != previous.run_id:
            return None
        updated = self.transition_approval(approval_id, expected_status, new_status, decision_reason)
        if updated is None:
            return None
        try:
            self.append_events([event])
        except Exception:
            if previous is not None:
                self.approvals[approval_id] = previous
            raise
        return updated

    def close(self): pass
store=AppStore()
