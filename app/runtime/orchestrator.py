import hashlib
from pathlib import Path
from shutil import copytree
from uuid import UUID, uuid4

from app.domain.handoffs import (
    DeveloperToQAHandoff,
    FailureEvidence,
    PMToDeveloperHandoff,
    QAResult,
    RecoveryContext,
)
from app.domain.models import (
    AgentState,
    CheckpointState,
    ExecutionManifest,
    Level,
    MissionRunResult,
    Role,
    SkillProfile,
    TraceEvent,
)
from app.domain.policy import PendingApproval, PolicyEvaluator
from app.tracing.recorder import sanitize_text
from .agent import BasicAgentRuntime

EVIDENCE_LIMIT = 2000


def _run_test_results(agent_state):
    return [
        item
        for item in agent_state.tool_results
        if item.get("tool_name") == "run_test"
        or item.get("metadata", {}).get("exit_code") is not None
    ]


def _latest_run_test_result(agent_state):
    results = _run_test_results(agent_state)
    return results[-1] if results else None


def validate_qa_test_evidence(agent_state, qa_result):
    result = _latest_run_test_result(agent_state)
    return bool(
        qa_result.status == "passed"
        and result
        and result.get("success") is True
        and result.get("metadata", {}).get("exit_code") == 0
    )


def classify_qa_outcome(agent_state, qa_result, tests_unchanged=True):
    """Return passed, recoverable_failure, evidence_conflict, or validation_failure."""
    result = _latest_run_test_result(agent_state)
    if not tests_unchanged:
        return "validation_failure", []
    if result and result.get("success") is True and result.get("metadata", {}).get("exit_code") == 0:
        if qa_result.status == "passed" and validate_qa_test_evidence(agent_state, qa_result):
            return "passed", []
        return "evidence_conflict", []
    evidence = _failure_evidence(agent_state)
    if evidence:
        return "recoverable_failure", evidence
    return "validation_failure", []


def _bounded(value: str) -> str:
    value = value or ""
    value = sanitize_text(value)
    return value if len(value) <= EVIDENCE_LIMIT else value[-EVIDENCE_LIMIT:]


def _failed_test_names(output: str) -> list[str]:
    return [line.strip()[:300] for line in output.splitlines() if line.strip().startswith("FAILED")][:20]


def _failure_evidence(agent_state) -> list[FailureEvidence]:
    for result in reversed(_run_test_results(agent_state)):
        exit_code = result.get("metadata", {}).get("exit_code")
        if result.get("success") is True or exit_code in (None, 0):
            continue
        arguments = result.get("arguments", {})
        path = str(arguments.get("path", "tests"))
        stdout = _bounded(str(result.get("output", "")))
        stderr = _bounded(str(result.get("error", "") or ""))
        return [
            FailureEvidence(
                test_command=f"pytest {path} -q -c /dev/null",
                test_path=path,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                failed_tests=_failed_test_names(stdout + "\n" + stderr),
            )
        ]
    return []


def _test_hashes(root: Path) -> dict[str, str]:
    tests = root / "tests"
    if not tests.exists():
        return {}
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tests.rglob("*.py")
        if path.is_file()
    }


def _recovery_budget(runtime_config: dict) -> int:
    value = runtime_config.get("max_recovery_attempts")
    if value is None:
        value = runtime_config.get("max_retries", 0)
    return max(0, int(value))


def _test_result_metadata(agent_state, qa_result):
    result = _latest_run_test_result(agent_state) or {}
    path = str(result.get("arguments", {}).get("path", "tests"))
    return {
        "reason": "qa_evidence_conflict",
        "test_command": f"pytest {path} -q -c /dev/null",
        "test_path": path,
        "test_exit_code": result.get("metadata", {}).get("exit_code"),
        "qa_status": qa_result.status,
    }


class BasicMissionOrchestrator:
    def __init__(self, provider, compiler, tools_factory, recorder, snapshot_manager=None, checkpoint_manager=None, approval_store=None, policy_evaluator=None):
        self.provider = provider
        self.compiler = compiler
        self.tools_factory = tools_factory
        self.recorder = recorder
        self.snapshot_manager = snapshot_manager
        self.checkpoint_manager = checkpoint_manager
        self.approval_store = approval_store
        self.policy_evaluator = policy_evaluator

    def run(
        self,
        mission_id: UUID,
        manifest: ExecutionManifest,
        fixture: Path,
        workspace_root: Path,
        mission_context: dict | None = None,
        *,
        run_id: UUID | None = None,
        workspace: Path | None = None,
        resume_checkpoint: CheckpointState | None = None,
        resume_stage: str | None = None,
        resumed_from_run_id: UUID | None = None,
        resumed_from_checkpoint_id: UUID | None = None,
        parent_pm_agent_run_id: UUID | None = None,
    ) -> MissionRunResult:
        run_id = run_id or uuid4()
        workspace_root.mkdir(parents=True, exist_ok=True)
        workspace = workspace or workspace_root / str(run_id)
        if resume_checkpoint is None:
            copytree(fixture, workspace)
        initial_test_hashes = _test_hashes(workspace)
        self.recorder.record(
            TraceEvent(
                mission_id=mission_id,
                mission_run_id=run_id,
                sequence=1,
                event_type="mission_started",
                payload={
                    key: str(value)
                    for key, value in {
                        "resumed_from_run_id": resumed_from_run_id,
                        "resumed_from_checkpoint_id": resumed_from_checkpoint_id,
                    }.items()
                    if value is not None
                },
            )
        )
        checkpoints = []
        tools = self.tools_factory(workspace)
        pm_id = parent_pm_agent_run_id
        dev_ids = []
        qa_ids = []
        recovery_attempt = resume_checkpoint.agent_state.recovery_attempt if resume_checkpoint else 0
        budget = _recovery_budget(manifest.runtime_config)
        policy_evaluator = self.policy_evaluator or PolicyEvaluator(
            mode=(manifest.policy_snapshot.mode if manifest.policy_snapshot else manifest.runtime_config.get("approval_mode", "disabled")),
            policy_version=(manifest.policy_snapshot.policy_version if manifest.policy_snapshot else manifest.runtime_config.get("policy_version", "1")),
            rules=manifest.runtime_config.get("policy_rules"),
        )

        def approval_handler(approval: PendingApproval, state: AgentState):
            if self.approval_store:
                for existing in self.approval_store.list_approvals(run_id):
                    if existing.status == "PENDING" and existing.tool_call.tool_name == approval.tool_call.tool_name and existing.tool_call.arguments_digest == approval.tool_call.arguments_digest:
                        return existing
                self.approval_store.save_approval(approval)
            return approval

        def execute(role, skills, handoff, attempt=0):
            permissions = {
                Role.PM: (),
                Role.DEVELOPER: ("list_files", "read_file", "search_code", "edit_file", "run_test"),
                Role.QA: ("run_test",),
            }
            expected = {Role.PM: "PMToDeveloperHandoff", Role.DEVELOPER: "DeveloperToQAHandoff", Role.QA: "QAResult"}
            agent_id = uuid4()
            state = AgentState(
                mission_id=mission_id,
                mission_run_id=run_id,
                agent_run_id=agent_id,
                role=role,
                level=Level.SENIOR,
                profile=SkillProfile(name=role.value, skills=skills),
                step=f"{role.value}_recovery_{attempt}" if role == Role.DEVELOPER and attempt else role.value,
                messages=[{"role": "user", "content": str(mission_context)}] if mission_context else [],
                handoffs=handoff,
                allowed_tools=permissions[role],
                expected_output=expected[role],
                recovery_attempt=attempt,
            )

            def checkpoint(agent_state):
                if self.snapshot_manager and self.checkpoint_manager:
                    snapshot = self.snapshot_manager.create(workspace)
                    checkpoint_id = self.checkpoint_manager.create(
                            CheckpointState(
                                mission_run_id=run_id,
                                current_agent_run_id=agent_id,
                                current_step=agent_state.step,
                                agent_state=agent_state,
                                handoffs=agent_state.handoffs,
                                skill_versions=manifest.skill_versions,
                                workspace_snapshot_id=snapshot.id,
                            )
                        )
                    checkpoints.append(checkpoint_id)
                    return checkpoint_id
                return None

            return agent_id, BasicAgentRuntime(self.provider, self.compiler, tools, self.recorder, mission_id, run_id, checkpoint, policy_evaluator, approval_handler).run(agent_id, state)

        def finish(status, final_qa, emit_finished=True):
            events = self.recorder.for_run(run_id)
            if emit_finished:
                self.recorder.record(
                    TraceEvent(
                        mission_id=mission_id,
                        mission_run_id=run_id,
                        sequence=len(events) + 1,
                        event_type="mission_finished",
                        payload={"status": status, "recovery_count": recovery_attempt},
                    )
                )
            events = self.recorder.for_run(run_id)
            changed = [
                event.payload.get("arguments", {}).get("path")
                for event in events
                if event.event_type == "tool_result" and event.payload.get("tool_name") == "edit_file"
            ]
            return MissionRunResult(
                mission_run_id=run_id,
                mission_id=mission_id,
                status=status,
                retry_count=recovery_attempt,
                recovery_count=recovery_attempt,
                execution_manifest=manifest,
                pm_agent_run_id=pm_id,
                developer_agent_run_ids=dev_ids,
                qa_agent_run_ids=qa_ids,
                final_qa_result=final_qa.model_dump(),
                changed_files=[path for path in changed if path],
                workspace_reference=str(workspace),
                tool_call_count=sum(event.event_type == "tool_call" for event in events),
                event_count=len(events),
                checkpoint_ids=checkpoints,
                resumed_from_run_id=resumed_from_run_id,
                resumed_from_checkpoint_id=resumed_from_checkpoint_id,
            )

        resumed_developer = None
        recovery_context = None
        if resume_checkpoint is None:
            pm_id, pm_state = execute(Role.PM, ("common/tool_usage.md", "common/handoff.md", "roles/pm/SKILL.md"), {})
            if pm_state.waiting_approval:
                return finish("WAITING_APPROVAL", QAResult(status="pending"), False)
            if not {"mission_summary", "developer_task"} <= pm_state.handoffs.keys():
                return finish("FAILED", QAResult(status="failed", issues=["PM runtime did not produce a handoff"]))
            pm = PMToDeveloperHandoff(**pm_state.handoffs)
            self.recorder.record(
                TraceEvent(
                    mission_id=mission_id,
                    mission_run_id=run_id,
                    agent_run_id=pm_id,
                    sequence=len(self.recorder.for_run(run_id)) + 1,
                    event_type="handoff_created",
                    payload=pm.model_dump(),
                )
            )
            if self.snapshot_manager and self.checkpoint_manager:
                snapshot = self.snapshot_manager.create(workspace)
                checkpoints.append(
                    self.checkpoint_manager.create(
                        CheckpointState(
                            mission_run_id=run_id,
                            current_agent_run_id=pm_id,
                            current_step="pm_handoff",
                            agent_state=pm_state,
                            handoffs=pm_state.handoffs,
                            skill_versions=manifest.skill_versions,
                            workspace_snapshot_id=snapshot.id,
                        )
                    )
                )
                self.recorder.record(
                    TraceEvent(
                        mission_id=mission_id,
                        mission_run_id=run_id,
                        agent_run_id=pm_id,
                        sequence=len(self.recorder.for_run(run_id)) + 1,
                        event_type="checkpoint_created",
                        payload={"reason": "pm_handoff", "checkpoint_id": str(checkpoints[-1])},
                    )
                )
        elif resume_stage == "developer":
            pm_id = pm_id or resume_checkpoint.current_agent_run_id
            pm = PMToDeveloperHandoff(**resume_checkpoint.agent_state.handoffs)
        elif resume_stage == "qa":
            pm = PMToDeveloperHandoff(**resume_checkpoint.agent_state.handoffs["pm"])
            resumed_developer = DeveloperToQAHandoff(**resume_checkpoint.agent_state.handoffs)
            saved_recovery = resume_checkpoint.agent_state.handoffs.get("recovery_context")
            if saved_recovery:
                recovery_context = RecoveryContext.model_validate(saved_recovery)
        else:
            return finish("FAILED", QAResult(status="failed", issues=["unsupported resume stage"]))

        qa = None
        while True:
            if resumed_developer is not None:
                dev = resumed_developer
                resumed_developer = None
            else:
                developer_handoff = {
                    "pm": pm.model_dump(),
                    "recovery_context": recovery_context.model_dump() if recovery_context else None,
                    "recovery_attempt": recovery_attempt,
                }
                dev_id, dev_state = execute(
                    Role.DEVELOPER,
                    ("common/tool_usage.md", "common/handoff.md", "roles/developer/SKILL.md"),
                    developer_handoff,
                    recovery_attempt,
                )
                dev_ids.append(dev_id)
                if dev_state.waiting_approval:
                    return finish("WAITING_APPROVAL", QAResult(status="pending"), False)
                if not {"status", "summary"} <= dev_state.handoffs.keys():
                    return finish("FAILED", QAResult(status="failed", issues=["Developer runtime did not produce a handoff"]))
                dev = DeveloperToQAHandoff(**dev_state.handoffs)
                self.recorder.record(
                    TraceEvent(
                        mission_id=mission_id,
                        mission_run_id=run_id,
                        sequence=len(self.recorder.for_run(run_id)) + 1,
                        event_type="handoff_created",
                        payload={"recovery_attempt": recovery_attempt, **dev.model_dump()},
                    )
                )
                if self.snapshot_manager and self.checkpoint_manager:
                    snapshot = self.snapshot_manager.create(workspace)
                    checkpoints.append(
                        self.checkpoint_manager.create(
                            CheckpointState(
                                mission_run_id=run_id,
                                current_agent_run_id=dev_id,
                                current_step=dev_state.step,
                                agent_state=dev_state,
                                handoffs=dev_state.handoffs,
                                skill_versions=manifest.skill_versions,
                                workspace_snapshot_id=snapshot.id,
                            )
                        )
                    )
                    self.recorder.record(
                        TraceEvent(
                            mission_id=mission_id,
                            mission_run_id=run_id,
                            sequence=len(self.recorder.for_run(run_id)) + 1,
                            event_type="checkpoint_created",
                            payload={"reason": "developer_handoff", "checkpoint_id": str(checkpoints[-1]), "recovery_attempt": recovery_attempt},
                        )
                    )

            qa_id, qa_state = execute(
                Role.QA,
                ("common/tool_usage.md", "common/handoff.md", "roles/qa/SKILL.md"),
                {"developer": dev.model_dump(), "recovery_attempt": recovery_attempt},
                recovery_attempt,
            )
            qa_ids.append(qa_id)
            if qa_state.waiting_approval:
                return finish("WAITING_APPROVAL", QAResult(status="pending"), False)
            if "status" not in qa_state.handoffs:
                return finish("FAILED", QAResult(status="failed", issues=["QA runtime did not produce a result"]))
            qa = QAResult(**qa_state.handoffs)

            decision, evidence = classify_qa_outcome(qa_state, qa, _test_hashes(workspace) == initial_test_hashes)
            if decision == "passed":
                return finish("PASSED", qa)
            if decision == "evidence_conflict":
                self.recorder.record(
                    TraceEvent(
                        mission_id=mission_id,
                        mission_run_id=run_id,
                        sequence=len(self.recorder.for_run(run_id)) + 1,
                        event_type="validation_error",
                        payload=_test_result_metadata(qa_state, qa),
                    )
                )
                return finish("FAILED", qa)
            if decision == "validation_failure":
                self.recorder.record(
                    TraceEvent(
                        mission_id=mission_id,
                        mission_run_id=run_id,
                        sequence=len(self.recorder.for_run(run_id)) + 1,
                        event_type="validation_error",
                        payload={"reason": "QA result lacks valid evidence or tests changed", "recovery_attempt": recovery_attempt},
                    )
                )
                return finish("FAILED", qa)
            if recovery_attempt >= budget:
                self.recorder.record(
                    TraceEvent(
                        mission_id=mission_id,
                        mission_run_id=run_id,
                        sequence=len(self.recorder.for_run(run_id)) + 1,
                        event_type="recovery_exhausted",
                        payload={"recovery_attempt": recovery_attempt, "max_recovery_attempts": budget, "trigger": "qa_failure"},
                    )
                )
                return finish("FAILED", qa)

            recovery_attempt += 1
            failure_summary = "; ".join(qa.issues) or f"{evidence[0].test_path} exited with code {evidence[0].exit_code}"
            recovery_context = RecoveryContext(
                recovery_attempt=recovery_attempt,
                previous_developer_handoff=dev,
                qa_result=qa,
                failure_evidence=evidence,
                failure_summary=failure_summary[:EVIDENCE_LIMIT],
            )
            self.recorder.record(
                TraceEvent(
                    mission_id=mission_id,
                    mission_run_id=run_id,
                    sequence=len(self.recorder.for_run(run_id)) + 1,
                    event_type="recovery_started",
                    payload={
                        "recovery_attempt": recovery_attempt,
                        "trigger": "qa_failure",
                        "failure_summary": recovery_context.failure_summary,
                        "failure_evidence": [
                            {
                                "test_command": item.test_command,
                                "test_path": item.test_path,
                                "exit_code": item.exit_code,
                                "failed_tests": item.failed_tests,
                            }
                            for item in evidence
                        ],
                    },
                )
            )
