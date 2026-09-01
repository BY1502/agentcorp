import json, shutil
from pathlib import Path
from uuid import UUID, uuid4
from app.domain.models import CheckpointState, WorkspaceSnapshot

class LocalWorkspaceSnapshotManager:
    def __init__(self, root: Path): self.root=root; self.snapshots={}
    def create(self, workspace: Path):
        sid=uuid4(); dest=self.root/"snapshots"/str(sid); dest.parent.mkdir(parents=True,exist_ok=True); shutil.copytree(workspace,dest)
        snap=WorkspaceSnapshot(id=sid,source_workspace=str(workspace),location=str(dest)); self.snapshots[sid]=snap; return snap
    def restore(self,snapshot_id: UUID,destination: Path): shutil.copytree(self.snapshots[snapshot_id].location,destination); return destination

class InMemoryCheckpointManager:
    def __init__(self,snapshots): self.snapshots=snapshots; self.records={}
    def create(self,state):
        checkpoint_id=uuid4(); self.records[checkpoint_id]=state.model_copy(deep=True); return checkpoint_id
    def restore(self,checkpoint_id): return self.records[checkpoint_id].model_copy(deep=True)
