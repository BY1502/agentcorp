from uuid import uuid4
from pathlib import Path
from app.domain.models import AgentState, SkillProfile
from app.domain.contracts import ModelResponse
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
    assert provider.requests[0].tools==[{'name':'read_file'}] and provider.requests[0].expected_output=='QAResult'

def test_runtime_maps_provider_error_to_runtime_error():
    class Broken:
        def complete(self,request): raise ProviderError('timeout_error','timeout')
    run=uuid4(); tr=InMemoryTraceRecorder(); state=AgentState(mission_run_id=run,profile=SkillProfile(name='x',skills=('common/tool_usage.md',)))
    result=BasicAgentRuntime(Broken(),DeterministicPromptCompiler(FilesystemSkillLoader(Path('skills'))),object(),tr,uuid4(),run).run(uuid4(),state)
    assert result.finished and any(e.event_type=='runtime_error' and e.payload['provider_error_type']=='timeout_error' for e in tr.events)
