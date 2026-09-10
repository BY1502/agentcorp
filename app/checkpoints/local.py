import json, shutil
from pathlib import Path
from uuid import UUID, uuid4
from app.domain.models import CheckpointState, WorkspaceSnapshot

class LocalWorkspaceSnapshotManager:
    def __init__(self, root: Path, snapshot_store=None):
        self.root = root
        self.snapshots = {}
        self.snapshot_store = snapshot_store
    def create(self, workspace: Path):
        sid=uuid4(); dest=self.root/"snapshots"/str(sid); dest.parent.mkdir(parents=True,exist_ok=True); shutil.copytree(workspace,dest)
        snap=WorkspaceSnapshot(id=sid,source_workspace=str(workspace),location=str(dest)); self.snapshots[sid]=snap
        if self.snapshot_store:
            self.snapshot_store.save_workspace_snapshot(snap)
        return snap
    def restore(self,snapshot_id: UUID,destination: Path):
        snapshot = self.snapshots.get(snapshot_id)
        if snapshot is None and self.snapshot_store:
            snapshot = self.snapshot_store.get_workspace_snapshot(snapshot_id)
        if snapshot is None:
            raise KeyError(snapshot_id)
        shutil.copytree(snapshot.location,destination); return destination

class InMemoryCheckpointManager:
    def __init__(self, snapshots, checkpoint_store=None):
        self.snapshots = snapshots
        self.records = {}
        self.checkpoint_store = checkpoint_store
    def create(self,state):
        checkpoint_id=uuid4(); self.records[checkpoint_id]=state.model_copy(deep=True)
        if self.checkpoint_store:
            self.checkpoint_store.save_checkpoint(checkpoint_id, state)
        return checkpoint_id
    def restore(self,checkpoint_id):
        state = self.records.get(checkpoint_id)
        if state is None and self.checkpoint_store:
            state = self.checkpoint_store.get_checkpoint(checkpoint_id)
        if state is None:
            raise KeyError(checkpoint_id)
        return state.model_copy(deep=True)
