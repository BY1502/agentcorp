import json
from uuid import uuid4
from pathlib import Path
import pytest
from app.domain.handoffs import QAResult
from app.domain.models import AgentState, Role, SkillProfile
from app.domain.contracts import ModelResponse, ToolCall, ToolResult
from app.models.lmstudio import ProviderError
from app.runtime.agent import BasicAgentRuntime, serialize_tool_result_for_provider
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
    state=AgentState(mission_run_id=run,role=Role.QA,profile=SkillProfile(name='x',skills=('common/tool_usage.md',)),allowed_tools=('run_test',),expected_output='QAResult',required_tool_before_final='run_test')
    BasicAgentRuntime(provider,DeterministicPromptCompiler(FilesystemSkillLoader(Path('skills'))),Tool(),tr,uuid4(),run).run(uuid4(),state)
    assert any(message.get('role')=='assistant' and message.get('tool_calls') for message in provider.requests[1].messages)
    assert any(message.get('tool_call_id')=='call_1' for message in provider.requests[1].messages)
    assert 'status' in provider.requests[1].messages[0]['content'] and 'issues' in provider.requests[1].messages[0]['content']
    evidence = json.loads(next(message['content'] for message in provider.requests[1].messages if message.get('role') == 'tool'))
    assert evidence == {
        'command': 'pytest tests -q -c /dev/null',
        'exit_code': 0,
        'path': 'tests',
        'stderr': '',
        'stdout': 'passed',
        'success': True,
        'tool_name': 'run_test',
    }
    assert provider.requests[0].response_schema is None and provider.requests[1].response_schema is None


@pytest.mark.parametrize(
    ('exit_code', 'stdout', 'stderr', 'expected_success'),
    [
        (0, '1 passed', '', True),
        (0, '1 passed, 1 warning', '', True),
        (1, '1 failed', '', False),
        (0, '1 passed', 'DeprecationWarning...', True),
    ],
)
def test_execution_metadata_is_serialized_for_provider(exit_code, stdout, stderr, expected_success):
    content = serialize_tool_result_for_provider(
        ToolCall(name='run_test', arguments={'path': 'tests/test_example.py'}),
        ToolResult(success=expected_success, output=stdout, error=stderr, metadata={'exit_code': exit_code}),
    )

    evidence = json.loads(content)
    assert evidence['tool_name'] == 'run_test'
    assert evidence['command'] == 'pytest tests/test_example.py -q -c /dev/null'
    assert evidence['path'] == 'tests/test_example.py'
    assert evidence['exit_code'] == exit_code
    assert evidence['success'] is expected_success
    assert evidence['stdout'] == stdout
    assert evidence['stderr'] == stderr


def test_non_execution_tool_output_keeps_existing_message_format():
    result = ToolResult(success=True, output='file contents', metadata={})

    assert serialize_tool_result_for_provider(
        ToolCall(name='read_file', arguments={'path': 'app/auth.py'}), result
    ) == 'file contents'


def test_qa_status_schema_describes_execution_verdict():
    status = QAResult.model_json_schema()['properties']['status']

    assert set(status['enum']) == {'passed', 'failed', 'pending'}
    assert all(term in status['description'] for term in ('run_test', 'exit_code', 'success', 'Warnings'))


def test_qa_status_schema_rejects_lifecycle_labels():
    with pytest.raises(ValueError):
        QAResult(status='completed')


def test_qa_prompt_requires_evidence_and_separates_concerns():
    prompt = DeterministicPromptCompiler(FilesystemSkillLoader(Path('skills'))).compile(
        {},
        SkillProfile(name='qa', skills=('common/tool_usage.md', 'common/handoff.md', 'roles/qa/SKILL.md')),
    )['messages'][0]['content'].lower()

    assert 'run_test' in prompt
    assert 'before returning' in prompt
    assert 'exit_code' in prompt and 'success' in prompt
    assert 'warnings' in prompt and 'issues' in prompt
    assert 'pending' in prompt and 'completed' in prompt


def test_qa_prompt_prefers_exact_developer_test_target():
    prompt = DeterministicPromptCompiler(FilesystemSkillLoader(Path('skills'))).compile(
        {
            'messages': [],
            'handoff': {'developer': {'tests_run': ['tests/test_auth.py']}},
            'expected_output': '',
            'final_output': '',
        },
        SkillProfile(name='qa', skills=('common/tool_usage.md', 'common/handoff.md', 'roles/qa/SKILL.md')),
    )
    text = '\n'.join(message['content'] for message in prompt['messages']).lower()

    assert 'tests_run' in text
    assert 'workspace-relative' in text
    assert 'preserve' in text and 'exactly' in text
    assert 'absolute path' in text
    assert 'do not invent' in text
    assert 'not the final qa' in text
    assert 'tests/test_auth.py' in text


@pytest.mark.parametrize(
    'targets',
    [
        ['tests/unit/test_auth.py'],
        ['tests/test_auth.py', 'tests/test_session.py'],
    ],
)
def test_qa_prompt_preserves_nested_and_multiple_developer_targets(targets):
    prompt = DeterministicPromptCompiler(FilesystemSkillLoader(Path('skills'))).compile(
        {
            'messages': [],
            'handoff': {'developer': {'tests_run': targets}},
            'expected_output': '',
            'final_output': '',
        },
        SkillProfile(name='qa', skills=('common/tool_usage.md', 'common/handoff.md', 'roles/qa/SKILL.md')),
    )
    text = '\n'.join(message['content'] for message in prompt['messages'])

    for target in targets:
        assert target in text
    if len(targets) > 1:
        assert text.index(targets[0]) < text.index(targets[1])


def test_qa_prompt_keeps_empty_handoff_behavior_without_inventing_target():
    prompt = DeterministicPromptCompiler(FilesystemSkillLoader(Path('skills'))).compile(
        {'messages': [], 'handoff': {}, 'expected_output': '', 'final_output': ''},
        SkillProfile(name='qa', skills=('common/tool_usage.md', 'common/handoff.md', 'roles/qa/SKILL.md')),
    )['messages'][0]['content'].lower()

    assert 'tests_run' in prompt and 'empty or absent' in prompt
    assert 'do not invent' in prompt and 'test target' in prompt


def qa_state():
    return AgentState(
        mission_run_id=uuid4(),
        role=Role.QA,
        profile=SkillProfile(name='qa', skills=('common/tool_usage.md',)),
        allowed_tools=('run_test',),
        expected_output='QAResult',
        required_tool_before_final='run_test',
    )


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return next(self.responses)


class CountingTool:
    def __init__(self, exit_code=0):
        self.calls = 0
        self.exit_code = exit_code

    def execute(self, call):
        self.calls += 1
        return ToolResult(
            success=self.exit_code == 0,
            output='test output',
            error='' if self.exit_code == 0 else 'test failed',
            metadata={'exit_code': self.exit_code},
        )


def run_qa_protocol(responses, tool=None):
    provider = ScriptedProvider(responses)
    recorder = InMemoryTraceRecorder()
    tool = tool or CountingTool()
    state = qa_state()
    result = BasicAgentRuntime(
        provider,
        DeterministicPromptCompiler(FilesystemSkillLoader(Path('skills'))),
        tool,
        recorder,
        uuid4(),
        state.mission_run_id,
    ).run(uuid4(), state)
    return result, provider, tool, recorder


def test_qa_final_first_gets_one_bounded_correction_turn():
    result, provider, tool, recorder = run_qa_protocol(
        [
            ModelResponse(output={'status': 'passed'}),
            ModelResponse(kind='tool', tool_call=ToolCall(name='run_test', arguments={'path': 'tests'})),
            ModelResponse(output={'status': 'passed'}),
        ]
    )

    assert result.finished and result.handoffs['status'] == 'passed'
    assert tool.calls == 1
    assert len(provider.requests) == 3
    assert any(
        'required run_test execution evidence is missing' in message['content']
        for message in provider.requests[1].messages
        if message['role'] == 'user'
    )
    assert any(event.payload.get('reason') == 'required_tool_missing' for event in recorder.events)


def test_qa_repeated_final_exhausts_without_tool_execution():
    result, provider, tool, recorder = run_qa_protocol(
        [ModelResponse(output={'status': 'passed'}), ModelResponse(output={'status': 'passed'})]
    )

    assert result.finished and not result.handoffs
    assert tool.calls == 0
    assert len(provider.requests) == 2
    assert sum(event.payload.get('reason') == 'required_tool_missing' for event in recorder.events) == 2


def test_qa_exit_four_is_evidence_not_a_missing_tool():
    result, _, tool, recorder = run_qa_protocol(
        [
            ModelResponse(kind='tool', tool_call=ToolCall(name='run_test', arguments={'path': 'missing.py'})),
            ModelResponse(output={'status': 'failed'}),
        ],
        CountingTool(exit_code=4),
    )

    assert result.finished and result.handoffs['status'] == 'failed'
    assert tool.calls == 1
    assert not any(event.payload.get('reason') == 'required_tool_missing' for event in recorder.events)
