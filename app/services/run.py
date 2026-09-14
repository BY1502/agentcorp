import hashlib
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.config import settings
from app.checkpoints.local import InMemoryCheckpointManager, LocalWorkspaceSnapshotManager
from app.domain.models import (
    ExecutionManifest,
    Level,
    ModelConfig,
    ModelExecutionSnapshot,
    PolicyExecutionSnapshot,
    Role,
    TraceEvent,
)
from app.domain.policy import ApprovalStatus, PendingApproval, PolicyEvaluator, ToolCallSnapshot
from app.models.factory import ProviderFactory
from app.models.lmstudio import LMStudioProvider
from app.models.registry import ModelConfigRegistry
from app.runtime.orchestrator import BasicMissionOrchestrator
from app.skills.filesystem import DeterministicPromptCompiler, FilesystemSkillLoader, SnapshotSkillLoader
from app.tools.filesystem import WorkspaceTools
from app.tracing.recorder import InMemoryTraceRecorder, sanitize_text
from .store import store


class ResumeError(ValueError):
    status_code = 409


class ResumeNotFoundError(ResumeError):
    status_code = 404


class ApprovalError(ValueError):
    status_code = 409


class ApprovalNotFoundError(ApprovalError):
    status_code = 404


class ApprovalStaleError(ApprovalError):
    def __init__(self, message, context):
        super().__init__(message)
        self.context = context


def _workspace_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _mission_context(mission) -> dict[str, str]:
    return {
        "task": mission.title,
        "fixture": mission.fixture,
        "workspace_rule": "The fixture is already the current workspace. Tool paths must be relative to its root; do not prefix them with the fixture path.",
        "completion_rule": "Developer must inspect the file, apply the requested edit with edit_file, and run tests before reporting completion. QA must run_test on the workspace tests before passing.",
        "recovery_rule": "During recovery, use the exact failed test command and workspace-relative test path from RecoveryContext; do not guess or rewrite the path.",
    }


def default_fake_responses() -> list[dict]:
    return [
        {"output": {"mission_summary": "fix", "developer_task": {"goal": "fix"}}},
        {"kind": "tool", "name": "list_files", "arguments": {"path": "."}},
        {"kind": "tool", "name": "read_file", "arguments": {"path": "app/auth.py"}},
        {
            "kind": "tool",
            "name": "edit_file",
            "arguments": {
                "path": "app/auth.py",
                "old_text": "return expiry < current_time",
                "new_text": "return expiry > current_time",
            },
        },
        {"output": {"status": "completed", "summary": "fixed"}},
        {"kind": "tool", "name": "run_test", "arguments": {"path": "tests"}},
        {"output": {"status": "passed", "passed": 2, "failed": 0}},
    ]


def default_model_registry() -> ModelConfigRegistry:
    return ModelConfigRegistry(
        configs=(
            ModelConfig(
                model_id=settings.default_model_id,
                provider_type="fake",
                model_name=settings.default_model_id,
            ),
            ModelConfig(
                model_id=settings.lmstudio_model_id,
                provider_type="lmstudio",
                model_name=settings.lmstudio_model,
                base_url=settings.lmstudio_base_url,
                timeout=settings.lmstudio_timeout,
            ),
        ),
        default_model_id=settings.default_model_id,
    )


def default_provider_factory() -> ProviderFactory:
    return ProviderFactory(
        fake_responses=default_fake_responses(),
        providers={
            "lmstudio": lambda config: LMStudioProvider(
                config.model_name,
                config.base_url,
                config.timeout,
            )
        },
    )


class RunService:
    def __init__(
        self,
        registry: ModelConfigRegistry | None = None,
        provider_factory: ProviderFactory | None = None,
        workspace_root: Path | None = None,
        skills_root: Path | None = None,
        storage=None,
        approval_mode: str | None = None,
        policy_rules: dict[str, str] | None = None,
        policy_version: str = "1",
        approval_ttl_seconds: float | None = None,
    ):
        self.registry = registry or default_model_registry()
        self.provider_factory = provider_factory or default_provider_factory()
        self.workspace_root = workspace_root or Path(settings.workspaces_dir)
        self.skills_root = skills_root or Path(settings.skills_dir)
        self.storage = storage or store
        self.approval_mode = approval_mode if approval_mode is not None else getattr(settings, "approval_mode", "disabled")
        self.policy_rules = dict(policy_rules or {})
        self.policy_version = str(policy_version)
        self.approval_ttl_seconds = approval_ttl_seconds

    def current_policy_snapshot(self, approval_mode: str | None = None) -> PolicyExecutionSnapshot:
        selected_approval_mode = approval_mode if approval_mode is not None else self.approval_mode
        policy_config = PolicyExecutionSnapshot(
            mode=selected_approval_mode,
            policy_version=self.policy_version,
            rules=tuple(sorted((name, str(action)) for name, action in self.policy_rules.items())),
            approval_ttl_seconds=self.approval_ttl_seconds,
        )
        PolicyEvaluator(
            mode=policy_config.mode,
            policy_version=policy_config.policy_version,
            rules=dict(policy_config.rules),
        )
        return policy_config

    def start(
        self,
        mission,
        model_id: str | None = None,
        approval_mode: str | None = None,
        *,
        model_snapshot: ModelExecutionSnapshot | None = None,
        runtime_config: dict | None = None,
        policy_snapshot: PolicyExecutionSnapshot | None = None,
        skill_versions: tuple | None = None,
        fixture: Path | None = None,
        run_id=None,
        initial_workspace_snapshot_id=None,
    ):
        policy_config = policy_snapshot or self.current_policy_snapshot(approval_mode)
        if model_snapshot is None:
            resolved_config = self.registry.resolve(model_id)
            model_snapshot = ModelExecutionSnapshot.from_config(resolved_config)
            provider = self.provider_factory.create(resolved_config)
        else:
            provider = self.provider_factory.create_from_snapshot(model_snapshot)
        if skill_versions is None:
            loader = FilesystemSkillLoader(self.skills_root)
            frozen_skill_versions = loader.snapshot(
                [
                    "common/tool_usage.md",
                    "common/handoff.md",
                    "roles/pm/SKILL.md",
                    "roles/developer/SKILL.md",
                    "roles/qa/SKILL.md",
                ]
            )
        else:
            frozen_skill_versions = tuple(skill_versions)
            loader = SnapshotSkillLoader(frozen_skill_versions)
        frozen_runtime_config = runtime_config if runtime_config is not None else {
            "max_retries": 0,
            "max_recovery_attempts": 1,
            "approval_mode": policy_config.mode,
            "policy_version": policy_config.policy_version,
            "policy_rules": dict(policy_config.rules),
            "approval_ttl_seconds": policy_config.approval_ttl_seconds,
        }
        recorder = InMemoryTraceRecorder()
        snapshot_manager = LocalWorkspaceSnapshotManager(self.workspace_root, self.storage)
        checkpoint_manager = InMemoryCheckpointManager(snapshot_manager, self.storage)
        manifest = ExecutionManifest(
            mission_id=mission.id,
            mission_version=mission.version,
            employee_assignments={role: uuid4() for role in Role},
            model_references={},
            role_levels={role: Level.SENIOR for role in Role},
            skill_versions=frozen_skill_versions,
            runtime_config=frozen_runtime_config,
            initial_workspace_snapshot_id=initial_workspace_snapshot_id or uuid4(),
            model_snapshot=model_snapshot,
            policy_snapshot=policy_config,
        )
        result = BasicMissionOrchestrator(
            provider,
            DeterministicPromptCompiler(loader),
            WorkspaceTools,
            recorder,
            snapshot_manager,
            checkpoint_manager,
            self.storage,
        ).run(
            mission.id,
            manifest,
            fixture or Path(mission.fixture),
            self.workspace_root,
            mission_context=_mission_context(mission),
            run_id=run_id,
        )
        events = recorder.for_run(result.mission_run_id)
        self.storage.finalize_run(result, events)
        return result

    def resume(self, run_id, checkpoint_id):
        parent = self.storage.get_run(run_id)
        if parent is None:
            raise ResumeNotFoundError(f"run not found: {run_id}")
        if parent.status not in {"FAILED", "EXHAUSTED"}:
            raise ResumeError("only failed runs can be resumed")
        if checkpoint_id not in parent.checkpoint_ids:
            raise ResumeError("checkpoint does not belong to run")

        try:
            checkpoint = self.storage.get_checkpoint(checkpoint_id)
        except Exception as error:
            raise ResumeError("checkpoint is corrupted") from error
        if checkpoint is None:
            raise ResumeNotFoundError(f"checkpoint not found: {checkpoint_id}")
        if checkpoint.mission_run_id != parent.mission_run_id:
            raise ResumeError("checkpoint does not belong to run")

        state = checkpoint.agent_state
        if (
            state.role == Role.PM
            and state.finished
            and checkpoint.current_step == "pm_handoff"
            and {"mission_summary", "developer_task"} <= state.handoffs.keys()
        ):
            stage = "developer"
            required_skills = {
                "common/tool_usage.md",
                "common/handoff.md",
                "roles/developer/SKILL.md",
            }
        elif state.role == Role.DEVELOPER and state.finished and {"status", "summary"} <= state.handoffs.keys():
            stage = "qa"
            required_skills = {
                "common/tool_usage.md",
                "common/handoff.md",
                "roles/qa/SKILL.md",
            }
        else:
            raise ResumeError("checkpoint is not a supported handoff boundary")

        if not parent.execution_manifest.model_snapshot:
            raise ResumeError("run has no model snapshot")
        skills = tuple(checkpoint.skill_versions)
        if not required_skills <= {skill.name for skill in skills}:
            raise ResumeError("checkpoint has incomplete skill snapshots")

        mission = self.storage.get_mission(parent.mission_id)
        if mission is None:
            raise ResumeError("mission is missing")
        try:
            snapshot = self.storage.get_workspace_snapshot(checkpoint.workspace_snapshot_id)
        except Exception as error:
            raise ResumeError("workspace snapshot is corrupted") from error
        if snapshot is None or snapshot.id != checkpoint.workspace_snapshot_id or not Path(snapshot.location).is_dir():
            raise ResumeError("workspace snapshot is missing")

        new_run_id = uuid4()
        workspace = self.workspace_root / str(new_run_id)
        if workspace.exists():
            raise ResumeError("resume workspace already exists")
        expected_files = _workspace_hashes(Path(snapshot.location))
        try:
            LocalWorkspaceSnapshotManager(self.workspace_root, self.storage).restore(
                snapshot.id, workspace
            )
            if _workspace_hashes(workspace) != expected_files:
                raise ResumeError("workspace snapshot restore verification failed")
        except ResumeError:
            if workspace.exists():
                shutil.rmtree(workspace)
            raise
        except Exception as error:
            if workspace.exists():
                shutil.rmtree(workspace)
            raise ResumeError("workspace snapshot could not be restored") from error

        try:
            provider = self.provider_factory.create_from_snapshot(parent.execution_manifest.model_snapshot)
        except Exception as error:
            shutil.rmtree(workspace)
            raise ResumeError("historical model provider could not be created") from error

        recorder = InMemoryTraceRecorder()
        snapshot_manager = LocalWorkspaceSnapshotManager(self.workspace_root, self.storage)
        checkpoint_manager = InMemoryCheckpointManager(snapshot_manager, self.storage)
        result = BasicMissionOrchestrator(
            provider,
            DeterministicPromptCompiler(SnapshotSkillLoader(skills)),
            WorkspaceTools,
            recorder,
            snapshot_manager,
            checkpoint_manager,
            self.storage,
        ).run(
            mission.id,
            parent.execution_manifest,
            Path(mission.fixture),
            self.workspace_root,
            mission_context=_mission_context(mission),
            run_id=new_run_id,
            workspace=workspace,
            resume_checkpoint=checkpoint,
            resume_stage=stage,
            resumed_from_run_id=parent.mission_run_id,
            resumed_from_checkpoint_id=checkpoint_id,
            parent_pm_agent_run_id=parent.pm_agent_run_id,
        )
        self.storage.finalize_run(result, recorder.for_run(result.mission_run_id))
        return result

    def get(self, run_id):
        return self.storage.get_run(run_id)

    def events_for(self, run_id):
        return self.storage.list_events(run_id)

    def checkpoint(self, checkpoint_id):
        return self.storage.get_checkpoint(checkpoint_id)

    def approvals_for(self, run_id):
        try:
            approvals = self.storage.list_approvals(run_id)
            for approval in approvals:
                self._expire_if_due(approval)
            return self.storage.list_approvals(run_id)
        except Exception as error:
            if isinstance(error, ApprovalError):
                raise
            raise ApprovalError("approval is corrupted") from error

    def approval(self, approval_id):
        try:
            approval = self.storage.get_approval(approval_id)
            if approval is not None:
                self._expire_if_due(approval)
                approval = self.storage.get_approval(approval_id)
            return approval
        except Exception as error:
            if isinstance(error, ApprovalError):
                raise
            raise ApprovalError("approval is corrupted") from error

    @staticmethod
    def _policy_snapshot(run):
        return run.execution_manifest.policy_snapshot

    def _approval_checkpoint(self, run, approval_id):
        matches = []
        for checkpoint_id in run.checkpoint_ids:
            try:
                checkpoint = self.storage.get_checkpoint(checkpoint_id)
            except Exception as error:
                raise ApprovalError("approval checkpoint is corrupted") from error
            if checkpoint and checkpoint.agent_state.pending_approval_id == approval_id:
                if checkpoint.mission_run_id != run.mission_run_id or checkpoint.agent_state.mission_run_id != run.mission_run_id:
                    raise ApprovalError("approval checkpoint ownership mismatch")
                matches.append(checkpoint)
        if len(matches) > 1:
            raise ApprovalError("approval checkpoint is missing or ambiguous")
        return matches[0] if matches else None

    def _expire_if_due(self, approval):
        if approval.status != ApprovalStatus.PENDING:
            return approval
        run = self.storage.get_run(approval.run_id)
        snapshot = self._policy_snapshot(run) if run else None
        ttl = snapshot.approval_ttl_seconds if snapshot else None
        if run is None or run.status != "WAITING_APPROVAL" or ttl is None:
            return approval
        created_at = approval.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) <= created_at + timedelta(seconds=ttl):
            return approval
        checkpoint = self._approval_checkpoint(run, approval.approval_id)
        if checkpoint is None:
            return approval
        events = self.storage.list_events(run.mission_run_id)
        event = TraceEvent(
            mission_id=run.mission_id,
            mission_run_id=run.mission_run_id,
            agent_run_id=checkpoint.current_agent_run_id,
            sequence=len(events) + 1,
            event_type="approval_expired",
            payload=self._approval_audit_payload(approval, reason="approval_expired"),
        )
        expired = self._transition_with_event(approval, ApprovalStatus.EXPIRED, event, "approval expired")
        if expired is None:
            raise ApprovalError("approval is already decided")
        self._finish_decision_run(run, event_count=len(events) + 1, reason="approval_expired")
        return expired

    @staticmethod
    def _approval_audit_payload(approval, reason=None):
        payload = {
            "approval_id": str(approval.approval_id),
            "agent_role": approval.agent_role.value,
            "tool_name": approval.tool_call.tool_name,
            "policy_id": approval.policy_id,
            "policy_version": approval.policy_version,
            "arguments_digest": approval.tool_call.arguments_digest,
            "call_id": approval.tool_call.call_id,
        }
        if reason:
            payload["reason"] = reason
        return payload

    def _transition_with_event(self, approval, status, event, decision_reason=None):
        try:
            return self.storage.transition_approval_with_event(
                approval.approval_id,
                ApprovalStatus.PENDING,
                status,
                event,
                decision_reason,
            )
        except Exception as error:
            raise ApprovalError("approval decision could not be persisted") from error

    def _finish_decision_run(self, run, event_count, reason):
        events = self.storage.list_events(run.mission_run_id)
        recorder = InMemoryTraceRecorder(events)
        recorder.record(TraceEvent(
            mission_id=run.mission_id,
            mission_run_id=run.mission_run_id,
            sequence=len(events) + 1,
            event_type="mission_finished",
            payload={"status": "FAILED", "reason": reason, "recovery_count": run.recovery_count},
        ))
        failed = run.model_copy(update={
            "status": "FAILED",
            "final_qa_result": {"status": "failed", "issues": [reason]},
            "event_count": len(recorder.for_run(run.mission_run_id)),
        })
        self.storage.finalize_run(failed, recorder.for_run(run.mission_run_id)[event_count:])
        return failed

    def _fail_stale_approval(self, approval, run, checkpoint):
        events = self.storage.list_events(run.mission_run_id)
        recorder = InMemoryTraceRecorder(events)
        recorder.record(TraceEvent(
            mission_id=run.mission_id,
            mission_run_id=run.mission_run_id,
            agent_run_id=checkpoint.current_agent_run_id,
            sequence=len(events) + 1,
            event_type="approval_stale",
            payload=self._approval_audit_payload(approval, reason="approval_stale"),
        ))
        recorder.record(TraceEvent(
            mission_id=run.mission_id,
            mission_run_id=run.mission_run_id,
            sequence=len(events) + 2,
            event_type="mission_finished",
            payload={"status": "FAILED", "reason": "approval_stale", "recovery_count": run.recovery_count},
        ))
        failed = run.model_copy(update={
            "status": "FAILED",
            "final_qa_result": {"status": "failed", "issues": ["approval_stale"]},
            "event_count": len(recorder.for_run(run.mission_run_id)),
        })
        self.storage.finalize_run(failed, recorder.for_run(run.mission_run_id)[len(events):])
        return failed

    def _approval_context(self, approval_id, validate_workspace=True):
        approval = self.approval(approval_id)
        if approval is None:
            raise ApprovalNotFoundError(f"approval not found: {approval_id}")
        if approval.status != ApprovalStatus.PENDING:
            raise ApprovalError("approval is already decided")
        run = self.storage.get_run(approval.run_id)
        if run is None:
            raise ApprovalError("approval run is missing")
        if run.status != "WAITING_APPROVAL":
            raise ApprovalError("run is not waiting for approval")
        pending = [item for item in self.storage.list_approvals(run.mission_run_id) if item.status == ApprovalStatus.PENDING]
        if len(pending) != 1 or pending[0].approval_id != approval.approval_id:
            raise ApprovalError("approval does not match the active run state")
        policy_snapshot = self._policy_snapshot(run)
        if not approval.policy_id or (policy_snapshot and approval.policy_version != policy_snapshot.policy_version):
            raise ApprovalError("approval policy snapshot does not match run")
        try:
            required_events = [
                event for event in self.storage.list_events(run.mission_run_id)
                if event.event_type == "approval_required"
                and event.payload.get("approval_id") == str(approval.approval_id)
            ]
        except Exception as error:
            raise ApprovalError("approval audit is corrupted") from error
        if len(required_events) != 1:
            raise ApprovalError("approval audit record is missing or ambiguous")
        required = required_events[0].payload
        if any(required.get(key) != value for key, value in {
            "policy_id": approval.policy_id,
            "policy_version": approval.policy_version,
            "tool_name": approval.tool_call.tool_name,
            "arguments_digest": approval.tool_call.arguments_digest,
            "call_id": approval.tool_call.call_id,
        }.items()):
            raise ApprovalError("approval audit record does not match snapshot")
        checkpoints = []
        for checkpoint_id in run.checkpoint_ids:
            try:
                checkpoint = self.storage.get_checkpoint(checkpoint_id)
            except Exception as error:
                raise ApprovalError("approval checkpoint is corrupted") from error
            if checkpoint is None:
                continue
            if checkpoint.mission_run_id != run.mission_run_id or checkpoint.agent_state.mission_run_id != run.mission_run_id:
                raise ApprovalError("approval checkpoint ownership mismatch")
            if checkpoint.agent_state.waiting_approval and checkpoint.agent_state.pending_approval_id == approval.approval_id:
                checkpoints.append(checkpoint)
        if len(checkpoints) != 1:
            raise ApprovalError("approval checkpoint is missing or ambiguous")
        checkpoint = checkpoints[0]
        if checkpoint.current_agent_run_id != checkpoint.agent_state.agent_run_id or checkpoint.agent_state.role != approval.agent_role:
            raise ApprovalError("approval agent role mismatch")
        try:
            snapshot = ToolCallSnapshot.from_parts(
                approval.tool_call.tool_name,
                approval.tool_call.agent_role,
                approval.tool_call.arguments,
                approval.tool_call.call_id,
            )
        except ValueError as error:
            raise ApprovalError("approval tool snapshot is corrupted") from error
        try:
            pending_snapshot = ToolCallSnapshot.model_validate(checkpoint.agent_state.pending_tool_call or {})
        except Exception as error:
            raise ApprovalError("approval checkpoint tool snapshot is corrupted") from error
        if snapshot != approval.tool_call or pending_snapshot != approval.tool_call:
            raise ApprovalError("approval tool snapshot does not match checkpoint")
        workspace = Path(run.workspace_reference)
        if validate_workspace:
            try:
                workspace_snapshot = self.storage.get_workspace_snapshot(checkpoint.workspace_snapshot_id)
            except Exception as error:
                raise ApprovalError("approval workspace snapshot is corrupted") from error
            if workspace_snapshot is None or not workspace.is_dir() or not Path(workspace_snapshot.location).is_dir():
                raise ApprovalError("approval workspace snapshot is missing")
            if workspace_snapshot.mission_run_id and workspace_snapshot.mission_run_id != run.mission_run_id:
                raise ApprovalError("approval workspace snapshot ownership mismatch")
            if Path(workspace_snapshot.source_workspace).resolve() != workspace.resolve():
                raise ApprovalError("approval workspace snapshot ownership mismatch")
            if _workspace_hashes(workspace) != _workspace_hashes(Path(workspace_snapshot.location)):
                raise ApprovalStaleError("approval workspace is stale", (approval, run, checkpoint, workspace))
        return approval, run, checkpoint, workspace

    def _approval_response(self, approval, run):
        return {
            "approval_id": approval.approval_id,
            "approval_status": approval.status.value,
            "run_id": run.mission_run_id,
            "run_status": run.status,
        }

    def approve(self, approval_id):
        try:
            approval, run, checkpoint, workspace = self._approval_context(approval_id)
        except ApprovalStaleError as error:
            approval, run, checkpoint, _ = error.context
            self._fail_stale_approval(approval, run, checkpoint)
            raise ApprovalError("approval workspace is stale") from error
        if not run.execution_manifest.model_snapshot:
            raise ApprovalError("approval run has no model snapshot")
        try:
            provider = self.provider_factory.create_from_snapshot(run.execution_manifest.model_snapshot)
        except Exception as error:
            raise ApprovalError("historical model provider could not be created") from error
        events = self.storage.list_events(run.mission_run_id)
        approval_event = TraceEvent(
            mission_id=run.mission_id,
            mission_run_id=run.mission_run_id,
            agent_run_id=checkpoint.current_agent_run_id,
            sequence=len(events) + 1,
            event_type="approval_approved",
            payload=self._approval_audit_payload(approval),
        )
        approved = self._transition_with_event(approval, ApprovalStatus.APPROVED, approval_event)
        if approved is None:
            raise ApprovalError("approval is already decided")
        events = self.storage.list_events(run.mission_run_id)
        recorder = InMemoryTraceRecorder(events)
        running = run.model_copy(update={"status": "RUNNING"})
        running = running.model_copy(update={"event_count": len(events)})
        self.storage.save_run(running)
        snapshot_manager = LocalWorkspaceSnapshotManager(self.workspace_root, self.storage)
        orchestrator = BasicMissionOrchestrator(
            provider,
            DeterministicPromptCompiler(SnapshotSkillLoader(checkpoint.skill_versions)),
            WorkspaceTools,
            recorder,
            snapshot_manager,
            InMemoryCheckpointManager(snapshot_manager, self.storage),
            self.storage,
        )
        mission = self.storage.get_mission(run.mission_id)
        if mission is None:
            raise ApprovalError("approval mission is missing")
        result = orchestrator.run(
            run.mission_id,
            run.execution_manifest,
            Path(mission.fixture),
            self.workspace_root,
            mission_context=_mission_context(mission),
            run_id=run.mission_run_id,
            workspace=workspace,
            continue_checkpoint=checkpoint,
            approved_approval=approved,
            prior_result=running,
            resumed_from_run_id=run.resumed_from_run_id,
            resumed_from_checkpoint_id=run.resumed_from_checkpoint_id,
            parent_pm_agent_run_id=run.pm_agent_run_id,
        )
        all_events = recorder.for_run(run.mission_run_id)
        self.storage.finalize_run(result, all_events[len(events):])
        return self._approval_response(approved, result)

    def reject(self, approval_id, reason=None):
        approval, run, checkpoint, _ = self._approval_context(approval_id, validate_workspace=False)
        bounded_reason = sanitize_text((reason or "approval rejected")[:200])
        events = self.storage.list_events(run.mission_run_id)
        rejection_event = TraceEvent(
            mission_id=run.mission_id,
            mission_run_id=run.mission_run_id,
            agent_run_id=checkpoint.current_agent_run_id,
            sequence=len(events) + 1,
            event_type="approval_rejected",
            payload=self._approval_audit_payload(approval, reason=bounded_reason),
        )
        rejected = self._transition_with_event(approval, ApprovalStatus.REJECTED, rejection_event, bounded_reason)
        if rejected is None:
            raise ApprovalError("approval is already decided")
        events = self.storage.list_events(run.mission_run_id)
        recorder = InMemoryTraceRecorder(events)
        recorder.record(TraceEvent(
            mission_id=run.mission_id,
            mission_run_id=run.mission_run_id,
            sequence=len(events) + 1,
            event_type="mission_finished",
            payload={"status": "FAILED", "reason": "approval_rejected", "recovery_count": run.recovery_count},
        ))
        failed = run.model_copy(update={
            "status": "FAILED",
            "final_qa_result": {"status": "failed", "issues": ["approval_rejected"]},
            "event_count": len(recorder.for_run(run.mission_run_id)),
        })
        self.storage.finalize_run(failed, recorder.for_run(run.mission_run_id)[len(events):])
        return self._approval_response(rejected, failed)
