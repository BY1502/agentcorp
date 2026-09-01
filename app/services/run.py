from pathlib import Path
from uuid import uuid4
from app.domain.models import ExecutionManifest, Role, Level
from app.models.fake import FakeModelProvider
from app.skills.filesystem import FilesystemSkillLoader, DeterministicPromptCompiler
from app.tools.filesystem import WorkspaceTools
from app.tracing.recorder import InMemoryTraceRecorder
from app.runtime.orchestrator import BasicMissionOrchestrator
from .store import store

class RunService:
    def start(self, mission):
        provider=FakeModelProvider([{'output':{'mission_summary':'fix','developer_task':{'goal':'fix'}}},{'kind':'tool','name':'list_files','arguments':{'path':'.'}},{'kind':'tool','name':'read_file','arguments':{'path':'app/auth.py'}},{'kind':'tool','name':'edit_file','arguments':{'path':'app/auth.py','old_text':'return expiry < current_time','new_text':'return expiry > current_time'}},{'output':{'status':'completed','summary':'fixed'}},{'kind':'tool','name':'run_test','arguments':{'path':'tests'}},{'output':{'status':'passed','passed':2,'failed':0}}])
        loader=FilesystemSkillLoader(Path('skills')); rec=InMemoryTraceRecorder(); manifest=ExecutionManifest(mission_id=mission.id,mission_version=mission.version,employee_assignments={r:uuid4() for r in Role},model_references={},role_levels={r:Level.SENIOR for r in Role},skill_versions=loader.snapshot(['common/tool_usage.md']),runtime_config={'max_retries':0},initial_workspace_snapshot_id=uuid4())
        result=BasicMissionOrchestrator(provider,DeterministicPromptCompiler(loader),WorkspaceTools,rec).run(mission.id,manifest,Path(mission.fixture),Path('workspaces'))
        store.runs[result.mission_run_id]=result; store.events[result.mission_run_id]=rec.for_run(result.mission_run_id); return result
    def get(self, run_id): return store.runs.get(run_id)
    def events_for(self, run_id): return store.events.get(run_id)
