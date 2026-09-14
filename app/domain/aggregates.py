from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RunAggregate(BaseModel):
    model_config = ConfigDict(frozen=True)

    evaluation_version: str = "agentcorp-eval-v1"
    run_count: int = 0
    evaluated_run_count: int = 0
    unavailable_run_count: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)
    terminal_run_count: int = 0
    non_terminal_run_count: int = 0
    passed_count: int = 0
    failed_count: int = 0
    terminal_pass_rate: float | None = None
    qa_final_test_pass_count: int = 0
    qa_final_test_fail_count: int = 0
    qa_final_test_unknown_count: int = 0
    qa_final_test_sample_count: int = 0
    qa_final_test_pass_rate: float | None = None
    recovery_run_count: int = 0
    total_recovery_attempts: int = 0
    recovered_to_pass_count: int = 0
    qa_evidence_conflict_run_count: int = 0
    tool_call_count: int = 0
    tool_success_count: int = 0
    tool_failure_count: int = 0
    tool_calls_by_name: dict[str, int] = Field(default_factory=dict)
    tool_failure_rate: float | None = None
    approval_run_count: int = 0
    approval_count: int = 0
    approval_pending_count: int = 0
    approved_count: int = 0
    rejected_count: int = 0
    expired_count: int = 0
    event_count: int = 0
    checkpoint_count: int = 0
    model_call_count: int = 0
    duration_sample_count: int = 0
    duration_avg_ms: float | None = None
    duration_median_ms: float | None = None
    duration_min_ms: float | None = None
    duration_max_ms: float | None = None
    evaluation_pass_count: int = 0
    evaluation_fail_count: int = 0
    evaluation_incomplete_count: int = 0
    evaluation_sample_count: int = 0
    evaluation_pass_rate: float | None = None


class ModelAggregate(RunAggregate):
    model_id: str | None = None
    provider_type: str | None = None
    model_name: str | None = None
    group_key: tuple[str | None, str | None, str | None]
