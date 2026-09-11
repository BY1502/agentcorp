# AgentCorp v0.1 Architecture

## 1. Goal and scope

AgentCorp is a local-first, single-process platform for comparing LLM-backed employees on deterministic coding missions. v0.1 supports three roles (PM, Developer, QA), one coding/debugging mission type, a bounded retry loop, observable traces, explicit checkpoints, and deterministic fake-model execution. The system is intentionally small: one FastAPI process, SQLite by default, filesystem skills, and mission-scoped workspaces.

The core execution path is:

```text
Mission -> MissionRun -> PM AgentRun -> Developer AgentRun -> QA AgentRun
                                      ^                 |
                                      +--- retry -------+
```

`MissionOrchestrator` owns PM → Developer → QA orchestration, retry policy, ownership changes, and mission completion. `AgentRuntime` executes exactly one AgentRun: prompt compilation → model call → tool execution → state update → validated structured final output. The orchestrator may invoke AgentRuntime multiple times. Agents communicate only through validated Pydantic handoffs. No private chain-of-thought is persisted.

## 2. Proposed repository layout

```text
agentcorp/
├── app/
│   ├── main.py                 # FastAPI application and route wiring
│   ├── config.py               # Environment-backed settings
│   ├── api/                    # Thin HTTP handlers and request/response DTOs
│   ├── domain/                 # Provider-neutral entities, enums, and state models
│   ├── runtime/                # AgentRuntime and MissionOrchestrator state machines
│   ├── models/                 # ModelProvider implementations and fake provider
│   ├── skills/                 # Skill loading, checksums, snapshots, prompt compiler
│   ├── tools/                  # Tool contracts, registry, and sandboxed tools
│   ├── tracing/                # Immutable trace records and recorder interface
│   ├── checkpoints/            # Serializable checkpoint state and local snapshots
│   ├── persistence/            # repository protocols and SQLite adapter
│   └── services/               # Small application use cases
├── skills/                     # Markdown source of truth for v0.1 skills
├── missions/                   # Mission definitions and deterministic fixtures
├── workspaces/                 # Runtime-created, mission-scoped copies
├── tests/                      # Unit and vertical-slice tests
├── scripts/                    # Small local developer utilities
├── pyproject.toml
├── .env.example
├── ARCHITECTURE.md
└── README.md
```

`app/domain` must not import FastAPI, SQLAlchemy, or a vendor SDK. API and persistence code depend inward on domain contracts. Concrete adapters are injected at the application boundary.

## 3. Domain model

All identifiers are UUIDs. Domain records use Pydantic models or frozen dataclasses where practical.

- **ModelConfig**: provider-neutral model configuration (`model_id`, `provider_type`, `base_url`, `model_name`, `enabled`, `timeout`, and optional `credential_ref`). It identifies intelligence/configuration, not a worker. Credential values and API keys are resolved only at runtime and are never part of reproducible records.
- **ModelExecutionSnapshot**: immutable, serializable safe snapshot resolved once at run start. It contains `model_id`, `provider_type`, `model_name`, sanitized endpoint identity, and `timeout`; it excludes `credential_ref` and all secret values.
- **Role**: PM, Developer, or QA.
- **Level**: Junior, Senior, or Lead.
- **SkillVersion**: immutable snapshot containing skill name, version, exact Markdown content, SHA-256 checksum, and creation time. A run stores the complete snapshots used, not only filesystem paths.
- **Employee**: assignment of one Model to one Role and Level, plus selected skill identities. It is the runtime worker configuration.
- **SkillProfile**: ordered collection of skills assigned to an employee/role. It is a domain/config abstraction, not a separate persistence aggregate in v0.1.
- **Mission**: user-facing work specification, acceptance criteria, and maximum retries.
- **ExecutionManifest**: immutable run-start snapshot containing mission identity/version, employee assignments, model identity/configuration references, role/level assignments, exact SkillVersion snapshots, runtime configuration (including max retries), the initial workspace snapshot reference, and the resolved `ModelExecutionSnapshot`. The model snapshot is frozen and contains no secrets.
- **MissionRun**: one complete execution attempt, including its frozen ExecutionManifest and status.
- **AgentRun**: one employee's execution inside a MissionRun, with role, level, model identity, status, and compiled skill snapshot references.
- **TraceEvent**: immutable ordered observable event. Fields: id, mission ID, run ID, optional agent-run ID, sequence, event type, timestamp, payload, and metadata.
- **CheckpointState**: explicit serializable continuation state: mission/run status, current agent and step, validated messages/handoffs, required tool results, assignment metadata, and exact skill versions. It has a `workspace_snapshot_id` reference, not filesystem contents.
- **WorkspaceSnapshot**: independent filesystem state representation. In v0.1 it may be a copied directory. `WorkspaceSnapshotManager` owns creation and restoration.
- **Checkpoint**: restorable record combining checkpoint metadata, CheckpointState, and its WorkspaceSnapshot reference.
- **ForkRun**: a new MissionRun derived from a checkpoint, recording replacement model, level, or skills and its parent checkpoint.
- **PolicyExecutionSnapshot**: immutable policy mode/version frozen in the ExecutionManifest. It contains no credentials or secrets.
- **PolicyDecision**: deterministic ALLOW, DENY, or REQUIRE_APPROVAL result for a proposed tool call. Permission checks remain a separate earlier gate.
- **PendingApproval**: safe, persisted inspection record containing a bounded `ToolCallSnapshot`, argument digest, policy identity, and reason. It is not an approval action.

Persistence may initially omit a standalone `roles`/`levels` table because these are closed v0.1 enums. They remain domain concepts and can become reference data when made configurable.

## 4. Key interfaces

The following protocols define seams without forcing infrastructure into the domain:

- `ModelProvider.complete(request) -> ModelResponse`: chat-completion adapter. The domain sees messages, structured-output request, response, usage, and latency—not an OpenAI SDK type.
- `SkillLoader.load(name, version?) -> SkillVersion` and `snapshot(names)`: reads Markdown and returns immutable content/checksum records.
- `PromptCompiler.compile(context, skill_versions) -> CompiledPrompt`: deterministic composition of global rules, common skills, role skill/subskills, level skill, mission context, and runtime context.
- `ToolRegistry.get(name)` / `list()`: resolves approved tool specifications.
- `ToolExecutor.execute(call, workspace) -> ToolResult`: executes only registered tools in a validated mission workspace. `ToolResult` contains success, output, error, and metadata.
- `TraceRecorder.record(event) -> TraceEvent`: assigns a monotonic per-run sequence and persists/collects immutable events.
- `CheckpointManager.create(state, workspace_snapshot_id) -> Checkpoint` and `restore(id) -> CheckpointState`: serializes explicit Pydantic state and checkpoints only at configured safe boundaries.
- `WorkspaceSnapshotManager.create(workspace) -> WorkspaceSnapshot` and `restore(snapshot_id, destination)`: owns independent filesystem snapshots; copied directories are sufficient for v0.1.
- `ModelConfigRegistry.resolve(model_id?) -> ModelConfig`: resolves the requested model or configured default, rejecting unknown and disabled models without fallback. `ProviderFactory.create(config) -> ModelProvider` builds the selected provider once for the run.
- `AgentRuntime.run(agent_run, state) -> AgentResult`: executes exactly one AgentRun and emits observable events for prompt compilation, model request/response, tool calls/results, validation, state updates, and finalization.
- `MissionOrchestrator.run(mission, manifest) -> MissionResult`: invokes AgentRuntime for PM, Developer, and QA, applies bounded retries, creates handoff/ownership-boundary checkpoints, and completes the MissionRun.
- `PolicyEvaluator.evaluate(role, tool_call) -> PolicyDecision`: provider-free deterministic policy evaluation after tool permission validation.
- `ApprovalStore.save/get/list`: persists safe pending approvals without executing or approving them.

The application resolves `model_id` to `ModelConfig`, creates one provider through `ProviderFactory`, and freezes the safe model snapshot into `ExecutionManifest` before orchestration. PM, Developer, and QA therefore share the provider selected for that run; the registry is not consulted again during the run. SQLAlchemy repositories, filesystem skill loading, HTTP model adapters, and the fake provider implement the contracts. Dependency direction:

```text
Mission -> ExecutionManifest -> MissionOrchestrator -> AgentRuntime
   |                                                        |
   |                                                        +--> ModelProvider
   |                                                        +--> PromptCompiler
   |                                                        +--> ToolExecutor
   |                                                        +--> TraceRecorder -> CheckpointManager -> WorkspaceSnapshotManager
   |
   +--> model_id -> ModelConfigRegistry -> ModelConfig -> ProviderFactory -> ModelProvider
```

## 5. Runtime and boundary rules

The runtime is a small state machine, not a framework graph. Every observable execution step emits a trace event, but filesystem checkpoints are not created after every event. Checkpoint policy is explicit and configurable; v0.1 safe boundaries are after completed tool results, after structured handoffs, and before changing ownership between agents.

Permission validation answers whether an agent may request a registered tool; policy evaluation then decides whether that permitted request is automatic, denied, or paused for approval. The default `approval_mode=disabled` preserves automatic tool execution. In `policy` mode, read-only tools are allowed, `edit_file` requires approval, and deterministic rules may deny a tool. A REQUIRE_APPROVAL decision persists a PendingApproval and checkpoint, emits `approval_required`, leaves the run in nonterminal `WAITING_APPROVAL`, and does not execute the tool or emit `mission_finished`. Step 1 exposes read-only approval inspection only; approve/reject continuation is deferred.

`TraceEvent` is an immutable observation. `Checkpoint` is restorable state. They are related by IDs and sequence context, but are not equivalent.

PM output becomes a validated `PMToDeveloperHandoff`; Developer output becomes `DeveloperToQAHandoff`; QA output becomes `QAResult`. Invalid structured output emits `validation_error` and follows an explicit failure policy.

Tool paths are resolved beneath the assigned workspace root, then checked with `resolve()` and `relative_to(root)`. Tests and commands execute with the workspace as their working directory. `run_command` is postponed unless a narrowly allowlisted implementation is needed.

## 6. Persistence strategy

The application boundary is `Runtime/Service -> repository protocol -> persistent adapter`. v0.1 uses the standard-library SQLite adapter at `AGENTCORP_STORAGE_PATH` (default `data/agentcorp.db`); the in-memory adapter remains available for deterministic unit tests. SQLite is not imported by domain or runtime code.

The adapter stores missions, run results, append-only trace events, checkpoint state, workspace-snapshot metadata, and pending approvals. UUIDs and datetimes use explicit JSON/primitive serialization; `PRAGMA user_version` records schema version 1. Run finalization writes the result and its events in one short transaction, while checkpoint, workspace, and approval metadata are committed at their safe boundaries.

Trace payloads, manifests, and checkpoint state are JSON-serializable only. No arbitrary Python object, API key, credential value, credential reference, raw provider request/response, or hidden reasoning is stored. Model records use a credential reference or runtime-resolved secret only before persistence. Historical run reads use the stored `ExecutionManifest` and `ModelExecutionSnapshot`; they never re-resolve mutable model configuration. Trace records are append-only.

Historical replay is a read-only inspection of persisted Run, Manifest, Events, Checkpoints, and WorkspaceSnapshots. `ReplayService` reconstructs a sequence-based timeline and safe summaries without creating a provider, calling a model, executing tools, restoring or mutating a workspace, or appending events/checkpoints. Replay is not Resume: Resume creates a new Run from a supported failed-run handoff checkpoint, restores the checkpoint snapshot into a new isolated workspace, preserves the parent manifest/model identity, and never mutates the parent. Rerun starts from the beginning.

## 7. v0.1 API surface

- `GET /health`
- `POST /models`, `GET /models`
- `POST /missions`, `GET /missions/{id}`
- `POST /missions/{id}/runs`
- `GET /runs/{id}`, `GET /runs/{id}/events`, `GET /runs/{id}/replay`
- `GET /runs/{id}/approvals`, `GET /approvals/{id}`
- `POST /runs/{id}/resume`

`POST /missions/{id}/runs` accepts an optional JSON body `{ "model_id": "..." }`. When omitted, the configured default model is selected. Unknown models return 404 and disabled models return 409; neither path silently falls back. The response exposes only the safe immutable model snapshot. Handlers remain thin and call services. Run creation initially executes synchronously to keep behavior easy to observe; background execution, streaming, and authentication are outside the first slice.

`POST /runs/{id}/resume` accepts a checkpoint ID and only resumes FAILED/EXHAUSTED runs at supported handoff boundaries: PM handoff resumes at Developer and Developer handoff resumes at QA. It reconstructs the provider from the persisted model snapshot and compiles from immutable checkpoint SkillVersion snapshots; it does not consult the mutable model registry or current Markdown files.

`WAITING_APPROVAL` is intentionally not accepted by the generic resume endpoint. Step 1 can inspect its persisted pending approval and replay timeline, while same-run approval continuation is reserved for the next policy step.

## 8. Testing strategy

Tests cover deterministic skill loading/checksums, prompt ordering, traversal rejection, tool results and events, trace sequencing, handoff validation, checkpoint round trips, and the intentionally broken demo fixture. A `FakeModelProvider` drives the complete PM → Developer → QA path without network access. The fixture's pytest test initially fails for the known authentication bug; the mission run is expected to fix the implementation without changing tests, after which the suite passes.

## 9. Deferred from v0.1

- Interview workflows, HR/promotion logic, CEO/CTO roles, and a company UI.
- Frontend and full Blackbox visualization.
- Automatic model/skill evaluation, judge models, statistical comparisons, and leaderboard features.
- Full checkpoint branching UX; v0.1 provides serializable state and a local copy-based fork seam.
- PostgreSQL deployment, migrations beyond basic setup, multi-process workers, queues, Redis, Celery, Docker Compose, Kubernetes, and microservices.
- Arbitrary shell execution, unrestricted tools, browser/network tools, and long-running async orchestration.
- Production authentication, authorization, secrets management, multi-user tenancy, and remote workspace storage.
- Token-budget optimization, prompt caching, advanced model routing, and vendor-specific features.

These are deliberately postponed because they do not prove the core observability/replayability loop and would make the first implementation harder to reason about.

## 10. Incremental implementation order

1. Create package/config/API skeleton and domain contracts.
2. Implement Markdown skill snapshots and deterministic prompt compilation.
3. Implement sandboxed tools and append-only in-memory/SQLite trace recording.
4. Implement explicit agent state, handoff schemas, fake provider, and checkpoint round trips.
5. Add the deterministic broken repository and synchronous vertical mission run.
6. Add repository protocols, SQLite persistence, and foundational historical-read endpoints.
7. Add the OpenAI-compatible HTTP adapter with configurable `base_url`, only after the fake flow and tests pass.

## 11. Approved scope boundary

The original v0.1 limits remain unchanged: no frontend, interview system, HR/promotion, CEO/CTO features, LangChain, LangGraph, Redis, Celery, Docker orchestration, or advanced evaluation. PHASE 2 defines seams for future capabilities without implementing them.
