from pathlib import Path
from shutil import copytree
from uuid import UUID, uuid4
from app.domain.models import AgentState, Role, Level, SkillProfile, MissionRunResult, ExecutionManifest, CheckpointState, TraceEvent
from app.domain.handoffs import PMToDeveloperHandoff, DeveloperToQAHandoff, QAResult
from .agent import BasicAgentRuntime

def validate_qa_test_evidence(agent_state, qa_result):
    runs=[x for x in agent_state.tool_results if x.get("tool_name")=="run_test" or x.get("metadata",{}).get("tool")=="run_test" or x.get("metadata",{}).get("exit_code") is not None]
    latest=runs[-1] if runs else None
    return qa_result.status != "passed" or bool(latest and latest.get("success") is True and latest.get("metadata",{}).get("exit_code")==0)

class BasicMissionOrchestrator:
    def __init__(self, provider, compiler, tools_factory, recorder, snapshot_manager=None, checkpoint_manager=None):
        self.provider=provider; self.compiler=compiler; self.tools_factory=tools_factory; self.recorder=recorder; self.snapshot_manager=snapshot_manager; self.checkpoint_manager=checkpoint_manager
    def run(self, mission_id: UUID, manifest: ExecutionManifest, fixture: Path, workspace_root: Path) -> MissionRunResult:
        run_id=uuid4(); workspace_root.mkdir(parents=True,exist_ok=True); workspace=workspace_root/str(run_id); copytree(fixture,workspace); seq=1
        self.recorder.record(TraceEvent(mission_id=mission_id,mission_run_id=run_id,sequence=seq,event_type="mission_started")); checkpoints=[]; tools=self.tools_factory(workspace); dev_ids=[]; qa_ids=[]; retry=0
        def execute(role, skills, handoff):
            aid=uuid4(); state=AgentState(mission_id=mission_id,mission_run_id=run_id,agent_run_id=aid,role=role,level=Level.SENIOR,profile=SkillProfile(name=role.value,skills=skills),handoffs=handoff)
            def checkpoint(s):
                if self.snapshot_manager and self.checkpoint_manager:
                    snap=self.snapshot_manager.create(workspace); checkpoints.append(self.checkpoint_manager.create(CheckpointState(mission_run_id=run_id,current_agent_run_id=aid,current_step=s.step,agent_state=s,workspace_snapshot_id=snap.id)))
            return aid,BasicAgentRuntime(self.provider,self.compiler,tools,self.recorder,mission_id,run_id,checkpoint).run(aid,state)
        pm_id,pm_state=execute(Role.PM,("common/tool_usage.md","common/handoff.md","roles/pm/SKILL.md"),{})
        pm=PMToDeveloperHandoff(**pm_state.handoffs)
        self.recorder.record(TraceEvent(mission_id=mission_id,mission_run_id=run_id,agent_run_id=pm_id,sequence=len(self.recorder.for_run(run_id))+1,event_type="handoff_created",payload=pm.model_dump()))
        if self.snapshot_manager and self.checkpoint_manager:
            snap=self.snapshot_manager.create(workspace); checkpoints.append(self.checkpoint_manager.create(CheckpointState(mission_run_id=run_id,current_agent_run_id=pm_id,current_step="pm_handoff",agent_state=pm_state,workspace_snapshot_id=snap.id))); self.recorder.record(TraceEvent(mission_id=mission_id,mission_run_id=run_id,agent_run_id=pm_id,sequence=len(self.recorder.for_run(run_id))+1,event_type="checkpoint_created",payload={"reason":"pm_handoff","checkpoint_id":str(checkpoints[-1])}))
        qa=None; feedback=None
        while True:
            dev_id,dev_state=execute(Role.DEVELOPER,("common/tool_usage.md","common/handoff.md","roles/developer/SKILL.md"),{"pm":pm.model_dump(),"qa_feedback":feedback,"retry":retry}); dev_ids.append(dev_id); dev=DeveloperToQAHandoff(**dev_state.handoffs)
            self.recorder.record(TraceEvent(mission_id=mission_id,mission_run_id=run_id,sequence=len(self.recorder.for_run(run_id))+1,event_type="handoff_created",payload=dev.model_dump()))
            if self.snapshot_manager and self.checkpoint_manager:
                snap=self.snapshot_manager.create(workspace); checkpoints.append(self.checkpoint_manager.create(CheckpointState(mission_run_id=run_id,current_agent_run_id=dev_id,current_step="developer_handoff",agent_state=dev_state,workspace_snapshot_id=snap.id))); self.recorder.record(TraceEvent(mission_id=mission_id,mission_run_id=run_id,sequence=len(self.recorder.for_run(run_id))+1,event_type="checkpoint_created",payload={"reason":"developer_handoff","checkpoint_id":str(checkpoints[-1])}))
            qa_id,qa_state=execute(Role.QA,("common/tool_usage.md","common/handoff.md","roles/qa/SKILL.md"),{"developer":dev.model_dump()}); qa_ids.append(qa_id); qa=QAResult(**qa_state.handoffs)
            if validate_qa_test_evidence(qa_state,qa):
                if qa.status=="passed": status="PASSED"; break
            if qa.status=="passed": self.recorder.record(TraceEvent(mission_id=mission_id,mission_run_id=run_id,sequence=len(self.recorder.for_run(run_id))+1,event_type="validation_error",payload={"reason":"QA pass lacks successful run_test evidence"}))
            if retry >= manifest.runtime_config.get("max_retries",0): status="FAILED"; break
            retry+=1; feedback=qa.model_dump()
        events=self.recorder.for_run(run_id); self.recorder.record(TraceEvent(mission_id=mission_id,mission_run_id=run_id,sequence=len(events)+1,event_type="mission_finished",payload={"status":status}))
        changed=[e.payload.get("arguments",{}).get("path") for e in self.recorder.for_run(run_id) if e.event_type=="tool_call" and e.payload.get("name")=="edit_file"]
        return MissionRunResult(mission_run_id=run_id,mission_id=mission_id,status=status,retry_count=retry,execution_manifest=manifest,pm_agent_run_id=pm_id,developer_agent_run_ids=dev_ids,qa_agent_run_ids=qa_ids,final_qa_result=qa.model_dump(),changed_files=[x for x in changed if x],workspace_reference=str(workspace),tool_call_count=sum(e.event_type=="tool_call" for e in self.recorder.for_run(run_id)),event_count=len(self.recorder.for_run(run_id)),checkpoint_ids=checkpoints)
