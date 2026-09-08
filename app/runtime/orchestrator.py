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


def validate_qa_test_evidence(agent_state, qa_result):
    latest = _run_test_results(agent_state)[-1:]
    result = latest[0] if latest else None
    return bool(
        qa_result.status == "passed"
        and result
        and result.get("success") is True
        and result.get("metadata", {}).get("exit_code") == 0
    )


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


class BasicMissionOrchestrator:
    def __init__(self, provider, compiler, tools_factory, recorder, snapshot_manager=None, checkpoint_manager=None):
        self.provider = provider
        self.compiler = compiler
        self.tools_factory = tools_factory
        self.recorder = recorder
        self.snapshot_manager = snapshot_manager
        self.checkpoint_manager = checkpoint_manager

    def run(self, mission_id: UUID, manifest: ExecutionManifest, fixture: Path, workspace_root: Path, mission_context: dict | None = None) -> MissionRunResult:
        run_id = uuid4()
        workspace_root.mkdir(parents=True, exist_ok=True)
        workspace = workspace_root / str(run_id)
        copytree(fixture, workspace)
        initial_test_hashes = _test_hashes(workspace)
        self.recorder.record(TraceEvent(mission_id=mission_id, mission_run_id=run_id, sequence=1, event_type="mission_started"))
        checkpoints = []
        tools = self.tools_factory(workspace)
        dev_ids = []
        qa_ids = []
        recovery_attempt = 0
        budget = _recovery_budget(manifest.runtime_config)

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
                    checkpoints.append(
                        self.checkpoint_manager.create(
                            CheckpointState(
                                mission_run_id=run_id,
                                current_agent_run_id=agent_id,
                                current_step=agent_state.step,
                                agent_state=agent_state,
                                workspace_snapshot_id=snapshot.id,
                            )
                        )
                    )

            return agent_id, BasicAgentRuntime(self.provider, self.compiler, tools, self.recorder, mission_id, run_id, checkpoint).run(agent_id, state)

        def finish(status, final_qa):
            events = self.recorder.for_run(run_id)
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
                if event.event_type == "tool_call" and event.payload.get("name") == "edit_file"
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
            )

        pm_id, pm_state = execute(Role.PM, ("common/tool_usage.md", "common/handoff.md", "roles/pm/SKILL.md"), {})
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

        qa = None
        recovery_context = None
        while True:
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
            if "status" not in qa_state.handoffs:
                return finish("FAILED", QAResult(status="failed", issues=["QA runtime did not produce a result"]))
            qa = QAResult(**qa_state.handoffs)

            if qa.status == "passed":
                if validate_qa_test_evidence(qa_state, qa) and _test_hashes(workspace) == initial_test_hashes:
                    return finish("PASSED", qa)
                self.recorder.record(
                    TraceEvent(
                        mission_id=mission_id,
                        mission_run_id=run_id,
                        sequence=len(self.recorder.for_run(run_id)) + 1,
                        event_type="validation_error",
                        payload={"reason": "QA pass lacks valid evidence or tests changed", "recovery_attempt": recovery_attempt},
                    )
                )
                return finish("FAILED", qa)

            evidence = _failure_evidence(qa_state)
            if not evidence:
                self.recorder.record(
                    TraceEvent(
                        mission_id=mission_id,
                        mission_run_id=run_id,
                        sequence=len(self.recorder.for_run(run_id)) + 1,
                        event_type="validation_error",
                        payload={"reason": "QA failure lacks failed run_test evidence", "recovery_attempt": recovery_attempt},
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
