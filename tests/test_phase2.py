from uuid import uuid4
from fastapi.testclient import TestClient
from app.main import app
from app.domain.models import AgentState, ExecutionManifest, Level, Role, SkillVersion, TraceEvent, WorkspaceSnapshot
from app.models.fake import FakeModelProvider
from app.domain.contracts import ModelRequest
from app.runtime.orchestrator import validate_qa_test_evidence
from app.domain.handoffs import QAResult
from app.tracing.recorder import sanitize

def test_health():
    assert TestClient(app).get("/health").json() == {"status": "ok"}

def test_manifest_and_skill_snapshot_are_explicit():
    snapshot = SkillVersion(name="x", version="1", content="hello", checksum="abc")
    ws = uuid4()
    manifest = ExecutionManifest(mission_id=uuid4(), mission_version="1", employee_assignments={Role.PM: uuid4()}, model_references={}, role_levels={Role.PM: Level.JUNIOR}, skill_versions=(snapshot,), runtime_config={"max_retries": 2}, initial_workspace_snapshot_id=ws)
    assert manifest.skill_versions[0].content == "hello"
    assert "api_key" not in manifest.model_dump()

def test_trace_is_immutable_and_ordered():
    from app.tracing.recorder import InMemoryTraceRecorder
    recorder, run = InMemoryTraceRecorder(), uuid4()
    event = TraceEvent(mission_id=uuid4(), mission_run_id=run, sequence=1, event_type="agent_started")
    recorder.record(event)
    assert recorder.for_run(run)[0].sequence == 1

def test_fake_provider():
    provider = FakeModelProvider([{"status": "ok"}])
    assert provider.complete(ModelRequest(messages=[])).output["status"] == "ok"

def test_qa_pass_rejected_without_run_test():
    assert not validate_qa_test_evidence(AgentState(tool_results=[]), QAResult(status="passed"))

def test_qa_pass_rejected_after_failed_run_test():
    state = AgentState(tool_results=[{"success": False, "metadata": {"exit_code": 1}}])
    assert not validate_qa_test_evidence(state, QAResult(status="passed"))

def test_qa_pass_accepted_after_successful_run_test():
    state = AgentState(tool_results=[{"success": True, "metadata": {"exit_code": 0}}])
    assert validate_qa_test_evidence(state, QAResult(status="passed"))

def test_trace_recorder_redacts_secrets():
    from app.tracing.recorder import InMemoryTraceRecorder
    run, mission = uuid4(), uuid4()
    event = TraceEvent(mission_id=mission, mission_run_id=run, sequence=1, event_type="model_request", payload={"api_key":"abc", "nested":{"Authorization":"Bearer token", "items":[{"access_token":"secret"},{"normal":"keep"}]}})
    stored = InMemoryTraceRecorder().record(event)
    assert stored.payload["api_key"] == "[REDACTED]"
    assert stored.payload["nested"]["Authorization"] == "[REDACTED]"
    assert stored.payload["nested"]["items"][1]["normal"] == "keep"

def test_checkpoint_created_after_edit(tmp_path):
    from app.runtime.agent import BasicAgentRuntime
    from app.checkpoints.local import LocalWorkspaceSnapshotManager, InMemoryCheckpointManager
    from app.tools.filesystem import WorkspaceTools
    from app.skills.filesystem import FilesystemSkillLoader, DeterministicPromptCompiler
    root=tmp_path/"workspace"; root.mkdir(); (root/"auth.py").write_text("return expiry < current_time")
    snapshots=LocalWorkspaceSnapshotManager(tmp_path); checkpoints=InMemoryCheckpointManager(snapshots); recorder=__import__('app.tracing.recorder',fromlist=['InMemoryTraceRecorder']).InMemoryTraceRecorder()
    provider=FakeModelProvider([{"kind":"tool","name":"edit_file","arguments":{"path":"auth.py","old_text":"return expiry < current_time","new_text":"return expiry > current_time"}},{"output":{"status":"completed"}}])
    loader=FilesystemSkillLoader(__import__('pathlib').Path("skills")); compiler=DeterministicPromptCompiler(loader); mission,run,agent=uuid4(),uuid4(),uuid4()
    state=AgentState(mission_id=mission,mission_run_id=run,agent_run_id=agent,profile=__import__('app.domain.models',fromlist=['SkillProfile']).SkillProfile(name="x",skills=("common/tool_usage.md",)))
    BasicAgentRuntime(provider,compiler,WorkspaceTools(root),recorder,mission,run,lambda s: checkpoints.create(__import__('app.domain.models',fromlist=['CheckpointState']).CheckpointState(mission_run_id=run,current_agent_run_id=agent,current_step=s.step,agent_state=s,workspace_snapshot_id=snapshots.create(root).id))).run(agent,state)
    assert (root/"auth.py").read_text().endswith("expiry > current_time")
    assert any(e.event_type=="checkpoint_created" for e in recorder.events)

def test_checkpoint_restore_preserves_workspace(tmp_path):
    from app.runtime.agent import BasicAgentRuntime
    from app.checkpoints.local import LocalWorkspaceSnapshotManager, InMemoryCheckpointManager
    from app.tools.filesystem import WorkspaceTools
    from app.skills.filesystem import FilesystemSkillLoader, DeterministicPromptCompiler
    from app.domain.models import SkillProfile, CheckpointState
    root=tmp_path/"workspace"; root.mkdir(); (root/"auth.py").write_text("return expiry < current_time")
    sm=LocalWorkspaceSnapshotManager(tmp_path); cm=InMemoryCheckpointManager(sm); tr=__import__('app.tracing.recorder',fromlist=['InMemoryTraceRecorder']).InMemoryTraceRecorder()
    provider=FakeModelProvider([{"kind":"tool","name":"edit_file","arguments":{"path":"auth.py","old_text":"return expiry < current_time","new_text":"return expiry > current_time"}},{"output":{"status":"completed"}}])
    compiler=DeterministicPromptCompiler(FilesystemSkillLoader(__import__('pathlib').Path("skills"))); mid,rid,aid=uuid4(),uuid4(),uuid4()
    state=AgentState(mission_id=mid,mission_run_id=rid,agent_run_id=aid,profile=SkillProfile(name="x",skills=("common/tool_usage.md",)))
    BasicAgentRuntime(provider,compiler,WorkspaceTools(root),tr,mid,rid,lambda s: cm.create(CheckpointState(mission_run_id=rid,current_agent_run_id=aid,current_step="edit",agent_state=s,workspace_snapshot_id=sm.create(root).id))).run(aid,state)
    checkpoint=next(iter(cm.records.values())); at_checkpoint=(root/"auth.py").read_text(); (root/"auth.py").write_text("later")
    destination=tmp_path/"restored"; sm.restore(checkpoint.workspace_snapshot_id,destination)
    assert (destination/"auth.py").read_text()==at_checkpoint and at_checkpoint!="later"

def test_pm_handoff_checkpoint_created(tmp_path):
    from app.runtime.orchestrator import BasicMissionOrchestrator
    from app.checkpoints.local import LocalWorkspaceSnapshotManager, InMemoryCheckpointManager
    from app.tools.filesystem import WorkspaceTools
    from app.skills.filesystem import FilesystemSkillLoader, DeterministicPromptCompiler
    mid=uuid4(); loader=FilesystemSkillLoader(__import__('pathlib').Path('skills')); recorder=__import__('app.tracing.recorder',fromlist=['InMemoryTraceRecorder']).InMemoryTraceRecorder(); sm=LocalWorkspaceSnapshotManager(tmp_path); cm=InMemoryCheckpointManager(sm)
    responses=[{'output':{'mission_summary':'x','developer_task':{'goal':'x'}}},{'output':{'status':'completed','summary':'x'}},{'kind':'tool','name':'run_test','arguments':{'path':'tests'}},{'output':{'status':'failed'}}]
    man=ExecutionManifest(mission_id=mid,mission_version='1',employee_assignments={r:uuid4() for r in Role},model_references={},role_levels={r:Level.SENIOR for r in Role},skill_versions=loader.snapshot(['common/tool_usage.md']),runtime_config={'max_retries':0},initial_workspace_snapshot_id=uuid4())
    result=BasicMissionOrchestrator(FakeModelProvider(responses),DeterministicPromptCompiler(loader),WorkspaceTools,recorder,sm,cm).run(mid,man,__import__('pathlib').Path('missions/demo_auth_bug/repo'),tmp_path/'runs')
    events=recorder.for_run(result.mission_run_id); assert result.status=='FAILED'; assert any(e.event_type=='handoff_created' for e in events); assert result.checkpoint_ids; assert result.execution_manifest is man

def test_developer_handoff_checkpoint_created(tmp_path):
    from app.runtime.orchestrator import BasicMissionOrchestrator
    from app.checkpoints.local import LocalWorkspaceSnapshotManager, InMemoryCheckpointManager
    from app.tools.filesystem import WorkspaceTools
    from app.skills.filesystem import FilesystemSkillLoader, DeterministicPromptCompiler
    mid=uuid4(); loader=FilesystemSkillLoader(__import__('pathlib').Path('skills')); recorder=__import__('app.tracing.recorder',fromlist=['InMemoryTraceRecorder']).InMemoryTraceRecorder(); sm=LocalWorkspaceSnapshotManager(tmp_path); cm=InMemoryCheckpointManager(sm)
    responses=[{'output':{'mission_summary':'x','developer_task':{'goal':'x'}}},{'output':{'status':'completed','summary':'x','changed_files':['app/auth.py']}},{'kind':'tool','name':'run_test','arguments':{'path':'tests'}},{'output':{'status':'failed'}}]
    man=ExecutionManifest(mission_id=mid,mission_version='1',employee_assignments={r:uuid4() for r in Role},model_references={},role_levels={r:Level.SENIOR for r in Role},skill_versions=loader.snapshot(['common/tool_usage.md']),runtime_config={'max_retries':0},initial_workspace_snapshot_id=uuid4())
    result=BasicMissionOrchestrator(FakeModelProvider(responses),DeterministicPromptCompiler(loader),WorkspaceTools,recorder,sm,cm).run(mid,man,__import__('pathlib').Path('missions/demo_auth_bug/repo'),tmp_path/'runs')
    events=recorder.for_run(result.mission_run_id); handoffs=[i for i,e in enumerate(events) if e.event_type=='handoff_created']; checkpoints=[i for i,e in enumerate(events) if e.event_type=='checkpoint_created']
    assert len(handoffs)>=2 and any(c>handoffs[-1] for c in checkpoints)

def test_demo_auth_mission_passes_end_to_end(tmp_path):
    from pathlib import Path
    from app.runtime.orchestrator import BasicMissionOrchestrator
    from app.checkpoints.local import LocalWorkspaceSnapshotManager, InMemoryCheckpointManager
    from app.tools.filesystem import WorkspaceTools
    from app.skills.filesystem import FilesystemSkillLoader, DeterministicPromptCompiler
    source=Path('missions/demo_auth_bug/repo'); assert not WorkspaceTools(source).run_test('tests').success
    loader=FilesystemSkillLoader(Path('skills')); mid=uuid4(); rec=__import__('app.tracing.recorder',fromlist=['InMemoryTraceRecorder']).InMemoryTraceRecorder(); sm=LocalWorkspaceSnapshotManager(tmp_path); cm=InMemoryCheckpointManager(sm)
    responses=[{'output':{'mission_summary':'x','developer_task':{'goal':'fix auth'}}},{'kind':'tool','name':'list_files','arguments':{'path':'.'}},{'kind':'tool','name':'read_file','arguments':{'path':'app/auth.py'}},{'kind':'tool','name':'edit_file','arguments':{'path':'app/auth.py','old_text':'return expiry < current_time','new_text':'return expiry > current_time'}},{'output':{'status':'completed','summary':'fixed','changed_files':['app/auth.py']}},{'kind':'tool','name':'run_test','arguments':{'path':'tests'}},{'output':{'status':'passed','passed':2,'failed':0}}]
    man=ExecutionManifest(mission_id=mid,mission_version='1',employee_assignments={r:uuid4() for r in Role},model_references={},role_levels={r:Level.SENIOR for r in Role},skill_versions=loader.snapshot(['common/tool_usage.md']),runtime_config={'max_retries':0},initial_workspace_snapshot_id=uuid4())
    result=BasicMissionOrchestrator(FakeModelProvider(responses),DeterministicPromptCompiler(loader),WorkspaceTools,rec,sm,cm).run(mid,man,source,tmp_path/'runs'); events=rec.for_run(result.mission_run_id)
    assert result.status=='PASSED' and result.retry_count==0 and result.changed_files==['app/auth.py']; assert 'expiry > current_time' in (Path(result.workspace_reference)/'app/auth.py').read_text(); assert 'expiry < current_time' in (source/'app/auth.py').read_text(); assert events[0].event_type=='mission_started' and events[-1].event_type=='mission_finished'; assert result.event_count==len(events)

def test_demo_auth_mission_retries_and_passes(tmp_path):
    from pathlib import Path
    from app.runtime.orchestrator import BasicMissionOrchestrator
    from app.tools.filesystem import WorkspaceTools
    from app.skills.filesystem import FilesystemSkillLoader, DeterministicPromptCompiler
    source=Path('missions/demo_auth_bug/repo'); loader=FilesystemSkillLoader(Path('skills')); rec=__import__('app.tracing.recorder',fromlist=['InMemoryTraceRecorder']).InMemoryTraceRecorder(); mid=uuid4()
    R=[{'output':{'mission_summary':'x','developer_task':{'goal':'fix'}}},{'output':{'status':'completed','summary':'not fixed'}},{'kind':'tool','name':'run_test','arguments':{'path':'tests'}},{'output':{'status':'failed','failed':2}},{'kind':'tool','name':'edit_file','arguments':{'path':'app/auth.py','old_text':'return expiry < current_time','new_text':'return expiry > current_time'}},{'output':{'status':'completed','summary':'fixed','changed_files':['app/auth.py']}},{'kind':'tool','name':'run_test','arguments':{'path':'tests'}},{'output':{'status':'passed','passed':2,'failed':0}}]
    man=ExecutionManifest(mission_id=mid,mission_version='1',employee_assignments={r:uuid4() for r in Role},model_references={},role_levels={r:Level.SENIOR for r in Role},skill_versions=loader.snapshot(['common/tool_usage.md']),runtime_config={'max_retries':1},initial_workspace_snapshot_id=uuid4())
    result=BasicMissionOrchestrator(FakeModelProvider(R),DeterministicPromptCompiler(loader),WorkspaceTools,rec).run(mid,man,source,tmp_path/'runs')
    assert result.status=='PASSED' and result.retry_count==1 and len(result.developer_agent_run_ids)==2 and len(result.qa_agent_run_ids)==2

def test_demo_auth_mission_fails_after_retry_exhaustion(tmp_path):
    from pathlib import Path
    from app.runtime.orchestrator import BasicMissionOrchestrator
    from app.tools.filesystem import WorkspaceTools
    from app.skills.filesystem import FilesystemSkillLoader, DeterministicPromptCompiler
    source=Path('missions/demo_auth_bug/repo'); loader=FilesystemSkillLoader(Path('skills')); rec=__import__('app.tracing.recorder',fromlist=['InMemoryTraceRecorder']).InMemoryTraceRecorder(); mid=uuid4()
    R=[{'output':{'mission_summary':'x','developer_task':{'goal':'fix'}}},{'output':{'status':'completed','summary':'no'}},{'kind':'tool','name':'run_test','arguments':{'path':'tests'}},{'output':{'status':'failed'}},{'output':{'status':'completed','summary':'no'}},{'kind':'tool','name':'run_test','arguments':{'path':'tests'}},{'output':{'status':'failed'}}]
    man=ExecutionManifest(mission_id=mid,mission_version='1',employee_assignments={r:uuid4() for r in Role},model_references={},role_levels={r:Level.SENIOR for r in Role},skill_versions=loader.snapshot(['common/tool_usage.md']),runtime_config={'max_retries':1},initial_workspace_snapshot_id=uuid4())
    result=BasicMissionOrchestrator(FakeModelProvider(R),DeterministicPromptCompiler(loader),WorkspaceTools,rec).run(mid,man,source,tmp_path/'runs')
    assert result.status=='FAILED' and result.retry_count==1 and len(result.developer_agent_run_ids)==2 and len(result.qa_agent_run_ids)==2

def test_execution_manifest_keeps_skill_snapshot(tmp_path):
    from pathlib import Path
    skill=tmp_path/'SKILL.md'; original='original instruction'; skill.write_text(original)
    loaded=__import__('app.skills.filesystem',fromlist=['FilesystemSkillLoader']).FilesystemSkillLoader(tmp_path).load('SKILL.md')
    manifest=ExecutionManifest(mission_id=uuid4(),mission_version='1',employee_assignments={},model_references={},role_levels={},skill_versions=(loaded,),runtime_config={'max_retries':1},initial_workspace_snapshot_id=uuid4())
    skill.write_text('changed instruction'); fresh=__import__('app.skills.filesystem',fromlist=['FilesystemSkillLoader']).FilesystemSkillLoader(tmp_path).load('SKILL.md')
    assert manifest.skill_versions[0].content==original and manifest.skill_versions[0].checksum!=fresh.checksum
    assert 'api_key' not in str(manifest.model_dump()) and 'credential' not in str(manifest.model_dump()).lower()
