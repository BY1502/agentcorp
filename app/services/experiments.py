from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from app.checkpoints.local import LocalWorkspaceSnapshotManager
from app.domain.experiments import (
    Experiment,
    ExperimentCell,
    ExperimentModelTarget,
    ExperimentRunView,
    ExperimentSpec,
    ExperimentStatus,
)
from app.domain.models import MissionRecord, ModelExecutionSnapshot
from app.models.registry import ModelConfigRegistry
from app.skills.filesystem import FilesystemSkillLoader


class ExperimentNotFoundError(LookupError):
    pass


class ExperimentExecutionError(ValueError):
    status_code = 409


class ExperimentService:
    """Create and seal experiment definitions without executing a mission."""

    def __init__(self, storage, registry: ModelConfigRegistry):
        self.storage = storage
        self.registry = registry

    def create(self, spec: ExperimentSpec) -> Experiment:
        # Client-supplied snapshots are ignored; only registry resolution at seal is authoritative.
        experiment = Experiment(
            experiment_id=uuid4(),
            status=ExperimentStatus.DRAFT,
            **spec.model_dump(exclude={"models", "expected_run_count"}),
            models=tuple(ExperimentModelTarget(model_id=target.model_id) for target in spec.models),
        )
        self.storage.save_experiment(experiment)
        return experiment

    def get(self, experiment_id: UUID) -> Experiment:
        experiment = self.storage.get_experiment(experiment_id)
        if experiment is None:
            raise ExperimentNotFoundError(experiment_id)
        return experiment

    def update_status(self, experiment: Experiment, status: ExperimentStatus) -> Experiment:
        updated = experiment.model_copy(update={"status": status})
        self.storage.save_experiment(updated)
        return updated

    def seal(self, experiment_id: UUID) -> Experiment:
        experiment = self.get(experiment_id)
        if experiment.status != ExperimentStatus.DRAFT:
            return experiment

        frozen_targets = []
        for target in experiment.models:
            config = self.registry.resolve(target.model_id)
            frozen_targets.append(target.model_copy(update={"snapshot": ModelExecutionSnapshot.from_config(config)}))

        sealed = experiment.model_copy(update={"status": ExperimentStatus.SEALED, "models": tuple(frozen_targets)})
        self.storage.save_experiment(sealed)
        return sealed


def expand_experiment(experiment: Experiment) -> tuple[ExperimentCell, ...]:
    """Expand a sealed definition in its declared case/model/repetition order."""
    if experiment.status == ExperimentStatus.DRAFT:
        raise ExperimentExecutionError("only sealed experiments can be expanded")
    cells = tuple(
        ExperimentCell(
            cell_id=uuid5(
                NAMESPACE_URL,
                f"agentcorp:experiment:{experiment.experiment_id}:{case.case_id}:{target.model_id}:{repetition}",
            ),
            experiment_id=experiment.experiment_id,
            case_id=case.case_id,
            case_index=case_index,
            model_id=target.model_id,
            model_index=model_index,
            repetition_index=repetition,
        )
        for case_index, case in enumerate(experiment.cases)
        for model_index, target in enumerate(experiment.models)
        for repetition in range(experiment.repetitions)
    )
    if len(cells) != experiment.expected_run_count:
        raise ExperimentExecutionError("experiment expected run count does not match its matrix")
    if any(target.snapshot is None for target in experiment.models):
        raise ExperimentExecutionError("sealed experiment has an unresolved model snapshot")
    return cells


_TERMINAL_RUN_STATUSES = {"PASSED", "FAILED", "EXHAUSTED"}
_SKILL_NAMES = (
    "common/tool_usage.md",
    "common/handoff.md",
    "roles/pm/SKILL.md",
    "roles/developer/SKILL.md",
    "roles/qa/SKILL.md",
)


class ExperimentExecutionService:
    """Sequentially materialize sealed cells through the existing RunService."""

    def __init__(self, storage, run_service, experiment_service: ExperimentService):
        self.storage = storage
        self.run_service = run_service
        self.experiments = experiment_service
        self.snapshot_manager = LocalWorkspaceSnapshotManager(run_service.workspace_root, storage)

    @staticmethod
    def _key(cell: ExperimentCell):
        return (cell.case_id, cell.model_id, cell.repetition_index)

    def _materialize(self, experiment: Experiment) -> list[ExperimentCell]:
        expected = expand_experiment(experiment)
        existing = self.storage.list_experiment_cells(experiment.experiment_id)
        expected_by_key = {self._key(cell): cell for cell in expected}
        existing_by_key = {self._key(cell): cell for cell in existing}
        if len(existing_by_key) != len(existing) or not set(existing_by_key) <= set(expected_by_key):
            raise ExperimentExecutionError("persisted experiment cells do not match the sealed matrix")
        for cell in expected:
            current = existing_by_key.get(self._key(cell))
            if current is None:
                self.storage.save_experiment_cell(cell)
            elif current.cell_id != cell.cell_id or current.case_index != cell.case_index or current.model_index != cell.model_index:
                raise ExperimentExecutionError("persisted experiment cell identity does not match the sealed matrix")
        return [self.storage.get_experiment_cell(cell.cell_id) or cell for cell in expected]

    def _seed_workspaces(self, experiment: Experiment, cells: list[ExperimentCell]) -> list[ExperimentCell]:
        case_by_id = {case.case_id: case for case in experiment.cases}
        seed_by_case = {
            cell.case_id: cell.workspace_snapshot_id
            for cell in cells
            if cell.workspace_snapshot_id is not None
        }
        for case in experiment.cases:
            if case.case_id in seed_by_case:
                continue
            source = Path(case.workspace_source)
            if not source.is_dir():
                raise ExperimentExecutionError(f"workspace source is unavailable: {case.workspace_source}")
            seed_by_case[case.case_id] = self.snapshot_manager.create(source).id
        updated = []
        for cell in cells:
            if cell.case_id not in case_by_id:
                raise ExperimentExecutionError(f"experiment cell references unknown case: {cell.case_id}")
            seeded = cell.model_copy(update={"workspace_snapshot_id": seed_by_case[cell.case_id]})
            self.storage.save_experiment_cell(seeded)
            updated.append(seeded)
        return updated

    def _conditions(self, experiment: Experiment, cells: list[ExperimentCell]):
        previous = None
        for cell in cells:
            if cell.run_id is not None:
                previous = self.storage.get_run(cell.run_id)
                if previous is not None:
                    break
        if previous is not None:
            manifest = previous.execution_manifest
            runtime_config = dict(manifest.runtime_config)
            policy_snapshot = manifest.policy_snapshot or self.run_service.current_policy_snapshot()
            skill_versions = tuple(manifest.skill_versions)
        else:
            runtime_config = dict(experiment.runtime_config)
            policy_snapshot = experiment.policy_snapshot or self.run_service.current_policy_snapshot()
            skill_versions = tuple(experiment.skill_versions) or FilesystemSkillLoader(self.run_service.skills_root).snapshot(list(_SKILL_NAMES))
        if experiment.runtime_config and runtime_config != experiment.runtime_config:
            raise ExperimentExecutionError("existing Run runtime config differs from sealed experiment")
        if experiment.policy_snapshot and policy_snapshot != experiment.policy_snapshot:
            raise ExperimentExecutionError("existing Run policy snapshot differs from sealed experiment")
        if experiment.skill_versions and skill_versions != experiment.skill_versions:
            raise ExperimentExecutionError("existing Run skill snapshot differs from sealed experiment")
        return runtime_config, policy_snapshot, skill_versions

    def _run_cell(self, experiment: Experiment, cell: ExperimentCell, runtime_config, policy_snapshot, skill_versions):
        if cell.run_id is not None:
            existing = self.storage.get_run(cell.run_id)
            if existing is not None:
                return existing
        case = next(case for case in experiment.cases if case.case_id == cell.case_id)
        target = experiment.models[cell.model_index]
        if target.model_id != cell.model_id or target.snapshot is None:
            raise ExperimentExecutionError("experiment cell model snapshot is invalid")
        mission_id = cell.mission_id or uuid5(NAMESPACE_URL, f"agentcorp:experiment-mission:{cell.cell_id}")
        mission = self.storage.get_mission(mission_id)
        if mission is None:
            mission = MissionRecord(case.mission_input, case.workspace_source, id=mission_id)
            self.storage.save_mission(mission)
        elif mission.title != case.mission_input or mission.fixture != case.workspace_source:
            raise ExperimentExecutionError("experiment mission mapping is inconsistent")
        run_id = cell.run_id or uuid5(NAMESPACE_URL, f"agentcorp:experiment-run:{cell.cell_id}")
        mapped = cell.model_copy(update={"mission_id": mission_id, "run_id": run_id})
        self.storage.save_experiment_cell(mapped)
        snapshot = self.storage.get_workspace_snapshot(cell.workspace_snapshot_id)
        if snapshot is None or not Path(snapshot.location).is_dir():
            raise ExperimentExecutionError("experiment workspace seed is unavailable")
        try:
            return self.run_service.start(
                mission,
                model_snapshot=target.snapshot,
                runtime_config=runtime_config,
                policy_snapshot=policy_snapshot,
                skill_versions=skill_versions,
                fixture=Path(snapshot.location),
                run_id=run_id,
                initial_workspace_snapshot_id=snapshot.id,
            )
        except ExperimentExecutionError:
            raise
        except Exception as error:
            raise ExperimentExecutionError("experiment cell execution failed") from error

    def _refresh_status(self, experiment: Experiment) -> Experiment:
        cells = self.storage.list_experiment_cells(experiment.experiment_id)
        complete = len(cells) == experiment.expected_run_count and all(
            cell.run_id is not None
            and (run := self.storage.get_run(cell.run_id)) is not None
            and run.status in _TERMINAL_RUN_STATUSES
            for cell in cells
        )
        status = ExperimentStatus.COMPLETED if complete else ExperimentStatus.RUNNING
        current = self.experiments.get(experiment.experiment_id)
        return self.experiments.update_status(current, status) if current.status != status else current

    def execute(self, experiment_id: UUID) -> Experiment:
        experiment = self.experiments.get(experiment_id)
        if experiment.status == ExperimentStatus.DRAFT:
            raise ExperimentExecutionError("draft experiments must be sealed before execution")
        if experiment.status == ExperimentStatus.COMPLETED:
            return experiment
        cells = self._seed_workspaces(experiment, self._materialize(experiment))
        runtime_config, policy_snapshot, skill_versions = self._conditions(experiment, cells)
        if experiment.status != ExperimentStatus.RUNNING:
            experiment = self.experiments.update_status(experiment, ExperimentStatus.RUNNING)
        for cell in cells:
            run = self._run_cell(experiment, cell, runtime_config, policy_snapshot, skill_versions)
            if run.status not in _TERMINAL_RUN_STATUSES:
                break
        return self._refresh_status(experiment)

    def runs(self, experiment_id: UUID) -> list[ExperimentRunView]:
        experiment = self.experiments.get(experiment_id)
        if experiment.status == ExperimentStatus.DRAFT:
            return []
        cells = self.storage.list_experiment_cells(experiment_id)
        targets = {target.model_id: target for target in experiment.models}
        views = []
        for cell in cells:
            target = targets.get(cell.model_id)
            if target is None or target.snapshot is None:
                raise ExperimentExecutionError("experiment cell model snapshot is unavailable")
            run = self.storage.get_run(cell.run_id) if cell.run_id else None
            views.append(ExperimentRunView(
                cell_id=cell.cell_id,
                experiment_id=cell.experiment_id,
                case_id=cell.case_id,
                model_id=cell.model_id,
                provider_type=target.snapshot.provider_type,
                model_name=target.snapshot.model_name,
                repetition_index=cell.repetition_index,
                mission_id=cell.mission_id,
                run_id=cell.run_id,
                run_status=run.status if run else "PENDING",
            ))
        return views
