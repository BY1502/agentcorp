from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EvaluationRuleStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EvaluationRuleResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_id: str
    rule_version: str = "v1"
    status: EvaluationRuleStatus
    message: str
    evidence: dict[str, object] = Field(default_factory=dict)


class RunEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: UUID
    evaluation_version: str = "agentcorp-eval-v1"
    status: Literal["PASS", "FAIL", "INCOMPLETE"]
    rules: tuple[EvaluationRuleResult, ...]
    summary: str
