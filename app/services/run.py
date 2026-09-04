from pathlib import Path
from uuid import uuid4

from app.config import settings
from app.domain.models import (
    ExecutionManifest,
    Level,
    ModelConfig,
    ModelExecutionSnapshot,
    Role,
)
from app.models.factory import ProviderFactory
from app.models.lmstudio import LMStudioProvider
from app.models.registry import ModelConfigRegistry
from app.runtime.orchestrator import BasicMissionOrchestrator
from app.skills.filesystem import DeterministicPromptCompiler, FilesystemSkillLoader
from app.tools.filesystem import WorkspaceTools
from app.tracing.recorder import InMemoryTraceRecorder
from .store import store


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
    ):
        self.registry = registry or default_model_registry()
        self.provider_factory = provider_factory or default_provider_factory()
        self.workspace_root = workspace_root or Path(settings.workspaces_dir)
        self.skills_root = skills_root or Path(settings.skills_dir)

    def start(self, mission, model_id: str | None = None):
        resolved_config = self.registry.resolve(model_id)
        provider = self.provider_factory.create(resolved_config)
        loader = FilesystemSkillLoader(self.skills_root)
        recorder = InMemoryTraceRecorder()
        manifest = ExecutionManifest(
            mission_id=mission.id,
            mission_version=mission.version,
            employee_assignments={role: uuid4() for role in Role},
            model_references={},
            role_levels={role: Level.SENIOR for role in Role},
            skill_versions=loader.snapshot(["common/tool_usage.md"]),
            runtime_config={"max_retries": 0},
            initial_workspace_snapshot_id=uuid4(),
            model_snapshot=ModelExecutionSnapshot.from_config(resolved_config),
        )
        result = BasicMissionOrchestrator(
            provider,
            DeterministicPromptCompiler(loader),
            WorkspaceTools,
            recorder,
        ).run(mission.id, manifest, Path(mission.fixture), self.workspace_root)
        store.runs[result.mission_run_id] = result
        store.events[result.mission_run_id] = recorder.for_run(result.mission_run_id)
        return result

    def get(self, run_id):
        return store.runs.get(run_id)

    def events_for(self, run_id):
        return store.events.get(run_id)
