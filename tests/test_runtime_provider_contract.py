from uuid import uuid4
from pathlib import Path
import pytest
from app.domain.models import AgentState, Role, SkillProfile
from app.domain.contracts import ModelResponse, ToolCall, ToolResult
from app.models.lmstudio import ProviderError
from app.runtime.agent import BasicAgentRuntime
from app.skills.filesystem import FilesystemSkillLoader, DeterministicPromptCompiler
from app.tracing.recorder import InMemoryTraceRecorder

class CaptureProvider:
    def __init__(self,response): self.response=response; self.requests=[]
    def complete(self,request): self.requests.append(request); return self.response

def test_runtime_passes_tools_and_schema_to_provider():
    provider=CaptureProvider(ModelResponse(output={'done':True})); run=uuid4(); tr=InMemoryTraceRecorder(); state=AgentState(mission_run_id=run,profile=SkillProfile(name='x',skills=('common/tool_usage.md',)),allowed_tools=('read_file',),expected_output='QAResult')
    BasicAgentRuntime(provider,DeterministicPromptCompiler(FilesystemSkillLoader(Path('skills'))),object(),tr,uuid4(),run).run(uuid4(),state)
    assert provider.requests[0].tools[0]['name']=='read_file'
    assert provider.requests[0].tools[0]['parameters']['required']==['path']
    assert provider.requests[0].expected_output==''
    assert provider.requests[0].response_schema is None

def test_runtime_maps_provider_error_to_runtime_error():
    class Broken:
        def complete(self,request): raise ProviderError('timeout_error','timeout')
    run=uuid4(); tr=InMemoryTraceRecorder(); state=AgentState(mission_run_id=run,profile=SkillProfile(name='x',skills=('common/tool_usage.md',)))
    result=BasicAgentRuntime(Broken(),DeterministicPromptCompiler(FilesystemSkillLoader(Path('skills'))),object(),tr,uuid4(),run).run(uuid4(),state)
    assert result.finished and any(e.event_type=='runtime_error' and e.payload['provider_error_type']=='timeout_error' for e in tr.events)

@pytest.mark.parametrize(
    ('role', 'expected_output', 'required_properties'),
    [
        (Role.PM, 'PMToDeveloperHandoff', {'mission_summary', 'developer_task'}),
        (Role.DEVELOPER, 'DeveloperToQAHandoff', {'status', 'summary'}),
        (Role.QA, 'QAResult', {'status', 'passed', 'failed', 'issues'}),
    ],
)
def test_runtime_resolves_json_schema_from_agent_contract(role, expected_output, required_properties):
    provider=CaptureProvider(ModelResponse(output={}))
    run=uuid4(); tr=InMemoryTraceRecorder()
    state=AgentState(
        mission_run_id=run,
        role=role,
        profile=SkillProfile(name=role.value,skills=('common/tool_usage.md',)),
        expected_output=expected_output,
    )
    BasicAgentRuntime(provider,DeterministicPromptCompiler(FilesystemSkillLoader(Path('skills'))),object(),tr,uuid4(),run).run(uuid4(),state)
    schema=provider.requests[0].response_schema
    assert schema['type']=='object'
    assert required_properties <= set(schema['properties'])

def test_runtime_leaves_schema_absent_for_untyped_agent_run():
    provider=CaptureProvider(ModelResponse(output={'done': True})); run=uuid4(); tr=InMemoryTraceRecorder()
    state=AgentState(mission_run_id=run,profile=SkillProfile(name='x',skills=('common/tool_usage.md',)))
    BasicAgentRuntime(provider,DeterministicPromptCompiler(FilesystemSkillLoader(Path('skills'))),object(),tr,uuid4(),run).run(uuid4(),state)
    assert provider.requests[0].response_schema is None

def test_runtime_preserves_assistant_tool_call_before_tool_result():
    class SequentialProvider:
        def __init__(self):
            self.requests=[]
            self.responses=[
                ModelResponse(kind='tool',tool_call=ToolCall(name='run_test',arguments={'path':'tests'})),
                ModelResponse(output={'status':'passed'}),
            ]
        def complete(self,request):
            self.requests.append(request)
            return self.responses.pop(0)
    class Tool:
        def execute(self,call): return ToolResult(success=True,output='passed',metadata={'exit_code':0})
    provider=SequentialProvider(); run=uuid4(); tr=InMemoryTraceRecorder()
    state=AgentState(mission_run_id=run,profile=SkillProfile(name='x',skills=('common/tool_usage.md',)),allowed_tools=('run_test',),expected_output='QAResult')
    BasicAgentRuntime(provider,DeterministicPromptCompiler(FilesystemSkillLoader(Path('skills'))),Tool(),tr,uuid4(),run).run(uuid4(),state)
    assert any(message.get('role')=='assistant' and message.get('tool_calls') for message in provider.requests[1].messages)
    assert any(message.get('tool_call_id')=='call_1' for message in provider.requests[1].messages)
    assert 'status' in provider.requests[1].messages[0]['content'] and 'issues' in provider.requests[1].messages[0]['content']
    assert provider.requests[0].response_schema is None and provider.requests[1].response_schema is None
