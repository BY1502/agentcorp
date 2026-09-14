import re
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from .models import ModelExecutionSnapshot, PolicyExecutionSnapshot, SkillVersion


_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "credential",
    "credential_ref",
    "reasoning",
    "reasoning_content",
}
_SENSITIVE_TEXT = re.compile(r"(?i)\b(api[_-]?key|authorization|access[_-]?token|password|secret|credential(?:_ref)?|reasoning(?:[_-]?content)?)\s*[:=]")


def _has_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in _SENSITIVE_KEYS or _has_secret(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_secret(item) for item in value)
    return isinstance(value, str) and bool(_SENSITIVE_TEXT.search(value))


def _portable_reference(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("workspace reference must not be empty")
    if Path(value).is_absolute() or value.startswith(("/", "\\")) or (len(value) > 2 and value[1] == ":"):
        raise ValueError("workspace reference must be portable, not an absolute host path")
    if ".." in Path(value).parts:
        raise ValueError("workspace reference must not escape its root")
    return value


class ExperimentStatus(StrEnum):
    DRAFT = "DRAFT"
    SEALED = "SEALED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"


class BenchmarkSuiteProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    suite_id: str
    version: int
    digest: str | None = None

    @field_validator("suite_id")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("benchmark suite provenance text must not be empty")
        return value

    @field_validator("digest")
    @classmethod
    def non_empty_digest(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("benchmark suite provenance digest must not be empty")
        return value

    @field_validator("version")
    @classmethod
    def positive_version(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("benchmark suite version must be a positive integer")
        return value


class ExperimentCase(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    name: str
    mission_input: str
    workspace_source: str
    expected_test_target: str | None = None

    @field_validator("case_id", "name", "mission_input")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("experiment case text must not be empty")
        return value

    @field_validator("workspace_source", "expected_test_target")
    @classmethod
    def portable_reference(cls, value: str | None) -> str | None:
        return _portable_reference(value) if value is not None else None


class ExperimentModelTarget(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_id: str
    snapshot: ModelExecutionSnapshot | None = None

    @field_validator("model_id")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("experiment model_id must not be empty")
        return value


class ExperimentSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    _allows_frozen_case_provenance: ClassVar[bool] = False

    spec_version: str = "experiment-spec-v1"
    name: str
    description: str | None = None
    cases: tuple[ExperimentCase, ...] = ()
    benchmark_suite: BenchmarkSuiteProvenance | None = None
    models: tuple[ExperimentModelTarget, ...]
    repetitions: int = 1
    runtime_config: dict[str, Any] = Field(default_factory=dict)
    policy_snapshot: PolicyExecutionSnapshot | None = None
    skill_versions: tuple[SkillVersion, ...] = ()

    @field_validator("name")
    @classmethod
    def name_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("experiment name must not be empty")
        return value

    @field_validator("repetitions", mode="before")
    @classmethod
    def repetitions_positive(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("repetitions must be at least 1")
        return value

    @field_validator("runtime_config")
    @classmethod
    def runtime_config_safe(cls, value: dict[str, Any]) -> dict[str, Any]:
        if _has_secret(value):
            raise ValueError("experiment runtime_config contains forbidden sensitive data")
        return value

    @model_validator(mode="after")
    def validate_matrix(self):
        if not self.cases and self.benchmark_suite is None:
            raise ValueError("experiment requires inline cases or a benchmark suite")
        if self.cases and self.benchmark_suite is not None and not self._allows_frozen_case_provenance:
            raise ValueError("experiment cannot combine inline cases and a benchmark suite")
        if not self.models:
            raise ValueError("experiment must contain at least one model")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("experiment case_id values must be unique")
        model_ids = [model.model_id for model in self.models]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("experiment model_id values must be unique")
        return self

    @computed_field
    @property
    def expected_run_count(self) -> int:
        return len(self.cases) * len(self.models) * self.repetitions


class Experiment(ExperimentSpec):
    model_config = ConfigDict(frozen=True)
    _allows_frozen_case_provenance: ClassVar[bool] = True

    experiment_id: UUID = Field(default_factory=uuid4)
    status: ExperimentStatus = ExperimentStatus.DRAFT


class ExperimentCell(BaseModel):
    """The durable identity and Run association for one matrix cell."""

    model_config = ConfigDict(frozen=True)

    cell_id: UUID
    experiment_id: UUID
    case_id: str
    case_index: int
    model_id: str
    model_index: int
    repetition_index: int
    workspace_snapshot_id: UUID | None = None
    mission_id: UUID | None = None
    run_id: UUID | None = None

    @field_validator("case_index", "model_index", "repetition_index")
    @classmethod
    def non_negative_index(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("matrix indexes must be non-negative integers")
        return value


class ExperimentRunView(BaseModel):
    """Safe read model for an Experiment cell and its existing Run."""

    model_config = ConfigDict(frozen=True)

    cell_id: UUID
    experiment_id: UUID
    case_id: str
    model_id: str
    provider_type: str
    model_name: str
    repetition_index: int
    mission_id: UUID | None = None
    run_id: UUID | None = None
    run_status: str
