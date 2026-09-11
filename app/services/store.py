from uuid import UUID

from app.domain.models import MissionRecord

__all__ = ["AppStore", "MissionRecord", "store"]

class AppStore:
    def __init__(self):
        self.missions = {}
        self.runs = {}
        self.events = {}
        self.checkpoints = {}
        self.workspace_snapshots = {}
        self.approvals = {}

    def save_mission(self, mission): self.missions[mission.id] = MissionRecord(mission.title, mission.fixture, mission.id, mission.version)
    def get_mission(self, mission_id): return self.missions.get(mission_id)
    def save_run(self, result): self.runs[result.mission_run_id] = result.model_copy(deep=True)
    def get_run(self, run_id): return self.runs.get(run_id)
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
    def save_approval(self, approval): self.approvals[approval.approval_id] = approval.model_copy(deep=True)
    def get_approval(self, approval_id):
        approval = self.approvals.get(approval_id)
        return approval.model_copy(deep=True) if approval else None
    def list_approvals(self, run_id): return [approval.model_copy(deep=True) for approval in self.approvals.values() if approval.run_id == run_id]

    def close(self): pass
store=AppStore()
