# AgentCorp

## 프로젝트 소개

AgentCorp는 LLM을 AI 직원으로 등록하고 PM / Developer / QA 직무에 배치하여 Coding Mission을 수행시키는 local-first AI 회사 시뮬레이션·평가 플랫폼입니다. 실행은 Blackbox Trace로 기록하고 Checkpoint Replay를 통해 추후 Model / Skill / Level 교체 결과를 비교합니다. 현재는 v0.1 백엔드 기반입니다.

## 핵심 아이디어

| 개념 | 의미 |
|---|---|
| Model | 지능을 제공하는 LLM 설정 |
| Employee | Model을 Role과 Level에 배치한 직원 |
| Role / Level | PM·Developer·QA / Junior·Senior·Lead |
| Skill | 재사용 가능한 Markdown 지침 |
| Mission / MissionRun | 업무 / 업무의 한 실행 |
| AgentRun | 한 Employee의 실행 |
| TraceEvent | 불변 실행 관찰 |
| Checkpoint / ForkRun | 복원 상태 / checkpoint에서 파생한 실행 |

## 전체 실행 흐름

```text
Mission → ExecutionManifest → MissionOrchestrator → PM → Developer → QA
                                                        → PASS / FAIL
                                                        → Blackbox Trace
                                                        → Checkpoint Replay
```

QA가 실패하면 설정된 최대 retry 횟수까지 Developer가 재실행됩니다. MissionOrchestrator는 전체 흐름과 완료를, AgentRuntime은 정확히 하나의 AgentRun을 담당합니다.

## 시스템 아키텍처

`domain`은 외부 프레임워크 독립 모델과 계약, `runtime`은 두 상태 머신, `models`는 ModelProvider, `skills`는 Markdown snapshot과 PromptCompiler, `tools`는 workspace 도구, `tracing`은 trace, `checkpoints`는 복원 상태, `persistence`는 향후 저장소, `api`는 FastAPI를 담당합니다.

```text
agentcorp/
├── app/{api,checkpoints,domain,models,runtime,skills,tools,tracing,persistence,services}/
├── skills/{common,levels,roles}/  missions/  workspaces/  tests/
├── ARCHITECTURE.md  CONTRIBUTING.md  pyproject.toml  README.md
```

의존성 방향은 `Mission → ModelConfigRegistry → ModelConfig → ProviderFactory → ModelProvider`와 `Mission → ExecutionManifest → MissionOrchestrator → AgentRuntime → PromptCompiler / ToolExecutor → TraceRecorder → CheckpointManager → WorkspaceSnapshotManager`입니다. Domain은 FastAPI, SQLAlchemy, vendor SDK에 의존하지 않습니다.

## Skill 시스템

```text
skills/{common,levels,roles}/
```

Model은 intelligence, Role은 직무, Level은 seniority behavior, Skill은 reusable instruction입니다. PromptCompiler는 global rules → common skills → role skill/subskills → level skill → mission context → runtime context 순으로 조합합니다. SkillVersion은 name, version, 정확한 content, checksum, created_at을 가진 불변 snapshot이며 Markdown 변경 후에도 과거 실행을 재현하게 합니다.

## Trace / Blackbox

초기 이벤트는 `mission_started`, `agent_started`, `prompt_compiled`, `model_request`, `model_response`, `tool_call`, `tool_result`, `handoff_created`, `checkpoint_created`, `validation_error`, `runtime_error`, `agent_finished`, `mission_finished`입니다. 관찰 가능한 request/response, tool, validation, latency, usage, handoff, error만 저장하며 private chain-of-thought와 secret은 저장하지 않습니다. TraceEvent는 observation이고 Checkpoint는 restorable state입니다.

## Checkpoint / Replay

Checkpoint는 runtime state, handoff state, model assignment, SkillVersion snapshot, WorkspaceSnapshot reference를 복원합니다. WorkspaceSnapshot은 파일시스템 상태를 별도 표현하며 v0.1에서는 복사 디렉터리를 사용할 수 있습니다. 향후 같은 checkpoint에서 다른 model, skill version, level로 ForkRun을 만들 수 있습니다. checkpoint는 모든 trace event마다 만들지 않고 tool result 완료·handoff·ownership 변경 전 같은 안전 경계에서 만듭니다.

## v0.1 범위

포함: PM, Developer, QA, coding/debugging mission 구조, FakeModelProvider, deterministic tests, local workspace, basic trace, checkpoint architecture, FastAPI foundation.

미구현: frontend, interview, HR/promotion, CEO/CTO, LangChain, LangGraph, Redis, Celery, Kubernetes, advanced judge evaluation, full Blackbox UI.

## 설치 및 실행

Python 3.12+가 필요합니다.

```bash
git clone https://github.com/BY1502/agentcorp.git
cd agentcorp
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
cp .env.example .env
pytest -q
uvicorn app.main:app --reload
```

현재 API는 `GET /health`, mission 생성·조회, `POST /missions/{id}/runs`, run 조회·event 조회를 제공합니다. run 생성 body에 선택적 `model_id`를 전달할 수 있으며 생략하면 설정된 default model을 사용합니다.

## 테스트

현재 테스트는 health/API lifecycle, model selection 및 safe snapshot, ExecutionManifest와 SkillVersion snapshot, TraceEvent ordering, FakeModelProvider, skill/path traversal/prompt/checkpoint/handoff 계약을 검증합니다.

## 개발 로드맵

- **v0.1 진행 중**: 핵심 계약, skill snapshot, trace foundation, FakeModelProvider, FastAPI foundation
- **v0.2 예정**: coding mission과 local adapter 안정화
- **v0.3 예정**: Blackbox viewer와 checkpoint replay
- **v0.4 예정**: AI interview / model audition
- **v0.5 예정**: employee evaluation / skill benchmarking
- **Future 예정**: promotion, 다중 역할, model-vs-model 실험

## 설계 원칙

Observable, Replayable, Reproducible, Comparable, Structured, Safe Local Execution, Model Agnostic, Simple First를 지킵니다. 즉 실행을 기록하고, 명시 상태로 재실행하며, snapshot으로 재현·비교하고, schema 통신과 workspace 격리 및 단순한 provider-neutral 구조를 유지합니다.

## 보안 / 주의사항

API key와 secret은 커밋하지 않습니다. 도구는 mission workspace 안에서만 동작해야 하며 arbitrary shell execution은 기본 활성화하지 않습니다. 현재 local research/development 용도입니다.

## PHASE 3 실행

현재 데모는 `PM → Developer → QA` 순서로 동작합니다. Developer가 workspace를 검사하고 `edit_file`로 인증 fixture를 수정한 뒤 QA가 자신의 `run_test`로 pytest를 실행합니다. QA가 실패하면 bounded retry 범위에서 Developer와 QA가 다시 실행됩니다.

실행 경로는 `PromptCompiler → ModelRequest → FakeModelProvider → ModelResponse(tool_call) → ToolRegistry → ToolResult → 다음 ModelRequest → structured output`입니다. AgentRun과 도구 실행은 TraceRecorder에 기록되고 edit/handoff safe boundary에서 checkpoint와 WorkspaceSnapshot을 생성합니다.

```bash
source .venv/bin/activate
pytest -q
uvicorn app.main:app --reload
```

API는 mission 생성·조회, 동기 run 실행, run 조회, event 조회를 제공합니다. 현재 저장소는 in-memory이며 FakeModelProvider와 LMStudioProvider를 제공합니다. 실제 기본 mission 실행, frontend, Blackbox UI, arbitrary fork/replay UI, interview, HR/promotion, distributed execution은 아직 없습니다.

## PHASE 4 Step 4 검증

Provider-neutral JSON Schema를 사용하는 `ModelConfig → ProviderFactory → LMStudioProvider → AgentRuntime` 경로를 Qwen3 8B GGUF / llama.cpp와 실제 LM Studio에서 검증했습니다. PMToDeveloperHandoff의 JSON parse 및 Pydantic validation까지 통과했습니다.

Qwen3.8 27B MLX는 structured-output 요청에서 reasoning-only 응답과 빈 `message.content`를 반환하는 compatibility limitation이 확인되었습니다. AgentCorp에서는 reasoning fallback이나 JSON repair workaround를 추가하지 않고, 해당 모델/runtime 조합의 별도 호환성 문제로 취급합니다.

## PHASE 4 Step 5 model selection

`POST /missions/{id}/runs`는 선택적인 `{ "model_id": "..." }`를 받아 `ModelConfigRegistry → ProviderFactory → ModelProvider`를 통해 provider를 한 번 선택합니다. 모델 설정은 `ModelConfig`로 관리하고, 실행 시작 시 `ModelExecutionSnapshot`을 `ExecutionManifest`에 고정합니다. snapshot에는 model/provider identity, 안전한 endpoint identity, timeout만 저장하며 credential reference와 secret 값은 저장하지 않습니다. 따라서 해당 run의 PM·Developer·QA는 동일한 provider snapshot을 사용하고, 이후 registry 설정 변경은 과거 run 기록을 바꾸지 않습니다.
