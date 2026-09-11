import hashlib
import shutil
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
)
from app.models.factory import ProviderFactory
from app.models.lmstudio import LMStudioProvider
from app.models.registry import ModelConfigRegistry
from app.runtime.orchestrator import BasicMissionOrchestrator
from app.skills.filesystem import DeterministicPromptCompiler, FilesystemSkillLoader, SnapshotSkillLoader
from app.tools.filesystem import WorkspaceTools
from app.tracing.recorder import InMemoryTraceRecorder
from .store import store


class ResumeError(ValueError):
    status_code = 409


class ResumeNotFoundError(ResumeError):
    status_code = 404


class ApprovalError(ValueError):
    status_code = 409


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
    ):
        self.registry = registry or default_model_registry()
        self.provider_factory = provider_factory or default_provider_factory()
        self.workspace_root = workspace_root or Path(settings.workspaces_dir)
        self.skills_root = skills_root or Path(settings.skills_dir)
        self.storage = storage or store
        self.approval_mode = approval_mode or getattr(settings, "approval_mode", "disabled")
        self.policy_rules = policy_rules or {}

    def start(self, mission, model_id: str | None = None, approval_mode: str | None = None):
        resolved_config = self.registry.resolve(model_id)
        provider = self.provider_factory.create(resolved_config)
        loader = FilesystemSkillLoader(self.skills_root)
        selected_approval_mode = approval_mode or self.approval_mode
        recorder = InMemoryTraceRecorder()
        snapshot_manager = LocalWorkspaceSnapshotManager(self.workspace_root, self.storage)
        checkpoint_manager = InMemoryCheckpointManager(snapshot_manager, self.storage)
        manifest = ExecutionManifest(
            mission_id=mission.id,
            mission_version=mission.version,
            employee_assignments={role: uuid4() for role in Role},
            model_references={},
            role_levels={role: Level.SENIOR for role in Role},
            skill_versions=loader.snapshot(
                [
                    "common/tool_usage.md",
                    "common/handoff.md",
                    "roles/pm/SKILL.md",
                    "roles/developer/SKILL.md",
                    "roles/qa/SKILL.md",
                ]
            ),
            runtime_config={"max_retries": 0, "max_recovery_attempts": 1, "approval_mode": selected_approval_mode, "policy_version": "1", "policy_rules": self.policy_rules},
            initial_workspace_snapshot_id=uuid4(),
            model_snapshot=ModelExecutionSnapshot.from_config(resolved_config),
            policy_snapshot=PolicyExecutionSnapshot(mode=selected_approval_mode, policy_version="1"),
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
            Path(mission.fixture),
            self.workspace_root,
            mission_context=_mission_context(mission),
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
            return self.storage.list_approvals(run_id)
        except Exception as error:
            raise ApprovalError("approval is corrupted") from error

    def approval(self, approval_id):
        try:
            return self.storage.get_approval(approval_id)
        except Exception as error:
            raise ApprovalError("approval is corrupted") from error
