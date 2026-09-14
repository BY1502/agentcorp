from typing import Literal

from pydantic import BaseModel, Field

class DeveloperTask(BaseModel):
    goal: str
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)

class PMToDeveloperHandoff(BaseModel):
    mission_summary: str
    developer_task: DeveloperTask

class DeveloperToQAHandoff(BaseModel):
    status: str
    changed_files: list[str] = Field(default_factory=list)
    tests_run: list[str] = Field(default_factory=list)
    summary: str
    risks: list[str] = Field(default_factory=list)

class QAResult(BaseModel):
    status: Literal["passed", "failed", "pending"] = Field(
        description=(
            "Final QA execution verdict. Use passed only when validated run_test "
            "evidence has exit_code 0 and success true; use failed when exit_code "
            "is non-zero or success is false. Warnings, stderr text, and quality "
            "concerns alone do not make status failed. pending is reserved for "
            "an approval-waiting runtime state, not a final verdict."
        )
    )
    passed: int = 0
    failed: int = 0
    issues: list[str] = Field(default_factory=list)

class FailureEvidence(BaseModel):
    test_command: str
    test_path: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    failed_tests: list[str] = Field(default_factory=list)

class RecoveryContext(BaseModel):
    recovery_attempt: int
    previous_developer_handoff: DeveloperToQAHandoff
    qa_result: QAResult
    failure_evidence: list[FailureEvidence] = Field(default_factory=list)
    failure_summary: str
