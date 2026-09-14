from collections import defaultdict
from uuid import UUID

from app.domain.experiment_analytics import (
    ExperimentAnalytics,
    ExperimentCaseResult,
    ExperimentIntegrityStatus,
    ExperimentModelResult,
)
from app.domain.experiments import Experiment, ExperimentCell, ExperimentStatus
from app.services.aggregates import RunAggregateService
from app.services.experiments import ExperimentNotFoundError, expand_experiment


_TERMINAL_RUN_STATUSES = {"PASSED", "FAILED", "EXHAUSTED"}


class ExperimentAnalyticsService:
    """Read-only analytics over the Runs explicitly mapped to one Experiment."""

    def __init__(self, storage, aggregate_service: RunAggregateService | None = None):
        self.storage = storage
        self.aggregates = aggregate_service or RunAggregateService(storage)

    @staticmethod
    def _key(cell: ExperimentCell):
        return (cell.case_id, cell.model_id, cell.repetition_index)

    @staticmethod
    def _status(cells, experiment: Experiment, mapped_by_key, issues):
        materialized = sum(cell is not None and cell.run_id is not None for cell in cells)
        terminal = sum(run.status in _TERMINAL_RUN_STATUSES for run in mapped_by_key.values())
        non_terminal = len(mapped_by_key) - terminal
        if experiment.status == ExperimentStatus.COMPLETED and (
            materialized != experiment.expected_run_count or terminal != experiment.expected_run_count
        ):
            issues.append("completed_experiment_has_incomplete_runs")
        if issues:
            integrity = ExperimentIntegrityStatus.INVALID
        elif materialized != experiment.expected_run_count or non_terminal:
            integrity = ExperimentIntegrityStatus.INCOMPLETE
        else:
            integrity = ExperimentIntegrityStatus.VALID
        return materialized, terminal, non_terminal, integrity

    def _aggregate(self, pairs, readings_by_run):
        unique = {}
        for cell, run in pairs:
            unique[run.mission_run_id] = (cell, run)
        runs = list(unique.values())
        readings = [readings_by_run[run.mission_run_id] for _, run in runs if run.mission_run_id in readings_by_run]
        aggregate = self.aggregates.aggregate_readings(readings, len(runs), len(runs) - len(readings))
        terminal = sum(run.status in _TERMINAL_RUN_STATUSES for _, run in runs)
        materialized = len(runs)
        return aggregate.model_copy(update={
            "terminal_run_count": terminal,
            "non_terminal_run_count": materialized - terminal,
        })

    def _model_result(self, target, expected_cells, valid_by_key, mapped_by_key, readings_by_run):
        pairs = [
            (cell, valid_by_key[self._key(cell)])
            for cell in expected_cells
            if self._key(cell) in valid_by_key
        ]
        materialized = sum(self._key(cell) in mapped_by_key for cell in expected_cells)
        terminal = sum(run.status in _TERMINAL_RUN_STATUSES for _, run in pairs)
        aggregate = self._aggregate(pairs, readings_by_run)
        return ExperimentModelResult(
            **aggregate.model_dump(),
            model_id=target.model_id,
            provider_type=target.snapshot.provider_type,
            model_name=target.snapshot.model_name,
            expected_cell_count=len(expected_cells),
            materialized_cell_count=materialized,
            sample_count=materialized,
            terminal_cell_count=terminal,
            non_terminal_cell_count=materialized - terminal,
        )

    def get(self, experiment_id: UUID) -> ExperimentAnalytics:
        experiment = self.storage.get_experiment(experiment_id)
        if experiment is None:
            raise ExperimentNotFoundError(experiment_id)
        if experiment.status == ExperimentStatus.DRAFT:
            return ExperimentAnalytics(
                experiment_id=experiment.experiment_id,
                experiment_status=experiment.status,
                expected_run_count=experiment.expected_run_count,
                materialized_run_count=0,
                terminal_run_count=0,
                non_terminal_run_count=0,
                integrity_status=ExperimentIntegrityStatus.INCOMPLETE,
                integrity_issues=("experiment_not_sealed",),
                overall=self.aggregates.aggregate_readings([], 0),
            )

        expected = expand_experiment(experiment)
        expected_by_key = {self._key(cell): cell for cell in expected}
        persisted = self.storage.list_experiment_cells(experiment_id)
        persisted_by_key = {}
        issues = []
        for cell in persisted:
            key = self._key(cell)
            if key not in expected_by_key:
                issues.append("unexpected_experiment_cell")
                continue
            if key in persisted_by_key:
                issues.append("duplicate_experiment_cell")
                continue
            expected_cell = expected_by_key[key]
            if (
                cell.cell_id != expected_cell.cell_id
                or cell.case_index != expected_cell.case_index
                or cell.model_index != expected_cell.model_index
            ):
                issues.append("experiment_cell_identity_mismatch")
            persisted_by_key[key] = cell

        valid_by_key = {}
        mapped_by_key = {}
        run_by_id = {}
        for key, cell in persisted_by_key.items():
            if cell.run_id is None:
                continue
            run = self.storage.get_run(cell.run_id)
            if run is None:
                issues.append("missing_mapped_run")
                continue
            mapped_by_key[key] = run
            if cell.mission_id is None or run.mission_id != cell.mission_id:
                issues.append("mission_mapping_mismatch")
                continue
            expected_cell = expected_by_key[key]
            target = experiment.models[expected_cell.model_index]
            if target.snapshot is None or run.execution_manifest.model_snapshot != target.snapshot:
                issues.append("model_snapshot_mismatch")
                continue
            if cell.run_id in run_by_id:
                issues.append("duplicate_run_reference")
                continue
            valid_by_key[key] = run
            run_by_id[cell.run_id] = run

        valid_runs = list(run_by_id.values())
        readings, unavailable = self.aggregates.read_runs(valid_runs)
        readings_by_run = {run.mission_run_id: (run, metrics, evaluation) for run, metrics, evaluation in readings}
        evaluation_versions = tuple(sorted({evaluation.evaluation_version for _, _, evaluation in readings}))
        if len(evaluation_versions) > 1:
            issues.append("mixed_evaluation_versions")

        persisted_cells = [persisted_by_key.get(self._key(cell)) for cell in expected]
        materialized, terminal, non_terminal, integrity = self._status(persisted_cells, experiment, mapped_by_key, issues)
        overall = self._aggregate(
            [(expected_by_key[key], run) for key, run in valid_by_key.items()],
            readings_by_run,
        )
        overall = overall.model_copy(update={
            "run_count": len(valid_runs),
            "unavailable_run_count": len(unavailable),
            "terminal_run_count": terminal,
            "non_terminal_run_count": non_terminal,
        })

        model_cells = defaultdict(list)
        case_cells = defaultdict(list)
        for cell in expected:
            model_cells[cell.model_index].append(cell)
            case_cells[cell.case_index].append(cell)
        models = tuple(
            self._model_result(target, model_cells[index], valid_by_key, mapped_by_key, readings_by_run)
            for index, target in enumerate(experiment.models)
        )
        cases = []
        for case_index, case in enumerate(experiment.cases):
            expected_case_cells = case_cells[case_index]
            pairs = [
                (cell, valid_by_key[self._key(cell)])
                for cell in expected_case_cells
                if self._key(cell) in valid_by_key
            ]
            case_aggregate = self._aggregate(pairs, readings_by_run)
            case_materialized = sum(self._key(cell) in mapped_by_key for cell in expected_case_cells)
            case_terminal = sum(run.status in _TERMINAL_RUN_STATUSES for _, run in pairs)
            cases.append(ExperimentCaseResult(
                **case_aggregate.model_dump(),
                case_id=case.case_id,
                expected_cell_count=len(expected_case_cells),
                materialized_cell_count=case_materialized,
                sample_count=case_materialized,
                terminal_cell_count=case_terminal,
                non_terminal_cell_count=case_materialized - case_terminal,
                models=tuple(
                    self._model_result(
                        target,
                        [cell for cell in expected_case_cells if cell.model_id == target.model_id],
                        valid_by_key,
                        mapped_by_key,
                        readings_by_run,
                    )
                    for target in experiment.models
                ),
            ))

        return ExperimentAnalytics(
            experiment_id=experiment.experiment_id,
            experiment_status=experiment.status,
            expected_run_count=experiment.expected_run_count,
            materialized_run_count=materialized,
            terminal_run_count=terminal,
            non_terminal_run_count=non_terminal,
            integrity_status=integrity,
            integrity_issues=tuple(sorted(set(issues))),
            evaluation_versions=evaluation_versions,
            overall=overall,
            models=models,
            cases=tuple(cases),
        )
