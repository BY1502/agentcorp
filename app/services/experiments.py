from uuid import UUID, uuid4

from app.domain.experiments import Experiment, ExperimentModelTarget, ExperimentSpec, ExperimentStatus
from app.domain.models import ModelExecutionSnapshot
from app.models.registry import ModelConfigRegistry


class ExperimentNotFoundError(LookupError):
    pass


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

    def seal(self, experiment_id: UUID) -> Experiment:
        experiment = self.get(experiment_id)
        if experiment.status == ExperimentStatus.SEALED:
            return experiment

        frozen_targets = []
        for target in experiment.models:
            config = self.registry.resolve(target.model_id)
            frozen_targets.append(target.model_copy(update={"snapshot": ModelExecutionSnapshot.from_config(config)}))

        sealed = experiment.model_copy(update={"status": ExperimentStatus.SEALED, "models": tuple(frozen_targets)})
        self.storage.save_experiment(sealed)
        return sealed
