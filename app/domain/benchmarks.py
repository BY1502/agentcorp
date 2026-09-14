import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .experiments import ExperimentCase
from .models import now


_SENSITIVE_TEXT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|access[_-]?token|password|secret|credential(?:_ref)?)\s*[:=]"
)


class BenchmarkSuiteStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"


class BenchmarkCaseSpec(ExperimentCase):
    """Reusable case definition built on the existing immutable ExperimentCase fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str | None = None

    @field_validator("description")
    @classmethod
    def safe_description(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if _SENSITIVE_TEXT.search(value):
                raise ValueError("benchmark case contains forbidden sensitive data")
        return value

    @model_validator(mode="after")
    def reject_sensitive_text(self):
        for value in (
            self.case_id,
            self.name,
            self.description,
            self.mission_input,
            self.workspace_source,
            self.expected_test_target,
        ):
            if value is not None and _SENSITIVE_TEXT.search(value):
                raise ValueError("benchmark case contains forbidden sensitive data")
        return self


BenchmarkCase = BenchmarkCaseSpec


class BenchmarkSuiteSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    suite_id: str
    version: int
    name: str
    description: str | None = None
    cases: tuple[BenchmarkCaseSpec, ...] = ()

    @field_validator("suite_id", "name")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("benchmark suite text must not be empty")
        if _SENSITIVE_TEXT.search(value):
            raise ValueError("benchmark suite contains forbidden sensitive data")
        return value

    @field_validator("description")
    @classmethod
    def safe_description(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if _SENSITIVE_TEXT.search(value):
                raise ValueError("benchmark suite contains forbidden sensitive data")
        return value

    @field_validator("version")
    @classmethod
    def positive_version(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("benchmark suite version must be a positive integer")
        return value

    @model_validator(mode="after")
    def unique_case_ids(self):
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("benchmark suite case_id values must be unique")
        return self


class BenchmarkSuite(BenchmarkSuiteSpec):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: BenchmarkSuiteStatus = BenchmarkSuiteStatus.DRAFT
    created_at: datetime = Field(default_factory=now)
    spec_digest: str | None = None


def benchmark_suite_digest(suite: BenchmarkSuiteSpec) -> str:
    """Hash only ordered, execution-relevant suite definition fields."""
    payload = {
        "version": suite.version,
        "cases": [
            {
                "case_id": case.case_id,
                "name": case.name,
                "mission_input": case.mission_input,
                "workspace_source": case.workspace_source,
                "expected_test_target": case.expected_test_target,
            }
            for case in suite.cases
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
