from pathlib import Path

from app.domain.benchmarks import (
    BenchmarkSuite,
    BenchmarkSuiteSpec,
    BenchmarkSuiteStatus,
    benchmark_suite_digest,
)


class BenchmarkSuiteNotFoundError(LookupError):
    pass


class BenchmarkSuiteConflictError(ValueError):
    pass


class BenchmarkSuiteValidationError(ValueError):
    pass


class BenchmarkSuiteService:
    """Create, publish, and inspect benchmark definitions without execution side effects."""

    def __init__(self, storage):
        self.storage = storage

    def create(self, spec: BenchmarkSuiteSpec) -> BenchmarkSuite:
        if self.storage.get_benchmark_suite(spec.suite_id, spec.version) is not None:
            raise BenchmarkSuiteConflictError("benchmark suite version already exists")
        suite = BenchmarkSuite(**spec.model_dump())
        try:
            self.storage.save_benchmark_suite(suite)
        except ValueError as error:
            raise BenchmarkSuiteConflictError(str(error)) from error
        return suite

    def get(self, suite_id: str, version: int) -> BenchmarkSuite:
        suite = self.storage.get_benchmark_suite(suite_id, version)
        if suite is None:
            raise BenchmarkSuiteNotFoundError((suite_id, version))
        return suite

    def list(self) -> list[BenchmarkSuite]:
        return self.storage.list_benchmark_suites()

    def publish(self, suite_id: str, version: int) -> BenchmarkSuite:
        suite = self.get(suite_id, version)
        if suite.status == BenchmarkSuiteStatus.PUBLISHED:
            return suite
        if not suite.cases:
            raise BenchmarkSuiteValidationError("benchmark suite must contain at least one case")
        for case in suite.cases:
            if not Path(case.workspace_source).is_dir():
                raise BenchmarkSuiteValidationError(
                    f"workspace source is unavailable: {case.workspace_source}"
                )
        published = suite.model_copy(update={
            "status": BenchmarkSuiteStatus.PUBLISHED,
            "spec_digest": benchmark_suite_digest(suite),
        })
        try:
            self.storage.save_benchmark_suite(published)
        except ValueError as error:
            raise BenchmarkSuiteConflictError(str(error)) from error
        return published
