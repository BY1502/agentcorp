from uuid import uuid4
from pathlib import Path
from app.domain.models import AgentState, Role, Level, SkillProfile
from app.domain.contracts import ModelResponse
from app.models.fake import FakeModelProvider
from app.runtime.agent import BasicAgentRuntime
from app.skills.filesystem import FilesystemSkillLoader, DeterministicPromptCompiler
from app.tracing.recorder import InMemoryTraceRecorder

def run(response, role, tools):
    run_id=uuid4(); tr=InMemoryTraceRecorder(); state=AgentState(mission_run_id=run_id,role=role,level=Level.SENIOR,profile=SkillProfile(name=role.value,skills=("common/tool_usage.md",)),allowed_tools=tools,expected_output={Role.PM:'PMToDeveloperHandoff',Role.DEVELOPER:'DeveloperToQAHandoff',Role.QA:'QAResult'}[role])
    return BasicAgentRuntime(FakeModelProvider([response]),DeterministicPromptCompiler(FilesystemSkillLoader(Path('skills'))),object(),tr,uuid4(),run_id).run(uuid4(),state),tr

def test_pm_handoff_validation_failure_is_observable():
    state,tr=run({'output':{'wrong':1}},Role.PM,())
    assert any(e.event_type=='validation_error' for e in tr.events) and not state.handoffs

def test_agent_tool_permissions_are_scoped():
    state,tr=run({'kind':'tool','name':'edit_file','arguments':{}},Role.QA,('run_test',))
    assert any(e.event_type=='validation_error' for e in tr.events)

def test_model_response_trace_keeps_observation_without_raw_output():
    state, tr = run({'output': {'mission_summary': 'summary', 'developer_task': {'goal': 'goal'}}}, Role.PM, ())
    event = next(e for e in tr.events if e.event_type == 'model_response')
    assert state.handoffs['mission_summary'] == 'summary'
    assert event.payload['output_keys'] == ['developer_task', 'mission_summary']
    assert 'output' not in event.payload
