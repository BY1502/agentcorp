from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from .aggregates import RunAggregate
from .experiments import ExperimentStatus


class ExperimentIntegrityStatus(StrEnum):
    VALID = "valid"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"


class ExperimentModelResult(RunAggregate):
    model_config = ConfigDict(frozen=True)

    model_id: str
    provider_type: str
    model_name: str
    expected_cell_count: int = 0
    materialized_cell_count: int = 0
    sample_count: int = 0
    terminal_cell_count: int = 0
    non_terminal_cell_count: int = 0


class ExperimentCaseResult(RunAggregate):
    model_config = ConfigDict(frozen=True)

    case_id: str
    expected_cell_count: int = 0
    materialized_cell_count: int = 0
    sample_count: int = 0
    terminal_cell_count: int = 0
    non_terminal_cell_count: int = 0
    models: tuple[ExperimentModelResult, ...] = ()


class ExperimentAnalytics(BaseModel):
    model_config = ConfigDict(frozen=True)

    experiment_id: UUID
    experiment_status: ExperimentStatus
    expected_run_count: int
    materialized_run_count: int
    terminal_run_count: int
    non_terminal_run_count: int
    integrity_status: ExperimentIntegrityStatus
    integrity_issues: tuple[str, ...] = ()
    evaluation_versions: tuple[str, ...] = ()
    overall: RunAggregate
    models: tuple[ExperimentModelResult, ...] = ()
    cases: tuple[ExperimentCaseResult, ...] = ()
