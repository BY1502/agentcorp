from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.domain.benchmarks import BenchmarkCaseSpec, BenchmarkSuiteSpec
from app.domain.experiments import (
    BenchmarkSuiteProvenance,
    ExperimentCase,
    ExperimentModelTarget,
    ExperimentSpec,
    ExperimentStatus,
)
from app.domain.models import ModelConfig
from app.models.factory import ProviderFactory
from app.models.registry import ModelConfigRegistry
from app.persistence.sqlite import SQLiteStore
from app.services.benchmarks import (
    BenchmarkSuiteNotFoundError,
    BenchmarkSuiteService,
)
from app.services.experiment_analytics import ExperimentAnalyticsService
from app.services.experiments import ExperimentCreationError, ExperimentExecutionService, ExperimentService
from app.services.run import RunService, default_fake_responses
from app.services.store import AppStore


def suite_spec(suite_id="coding-core", version=1, cases=None):
    cases = cases or (
        BenchmarkCaseSpec(
            case_id="case-c",
            name="Case C",
            mission_input="Fix C",
            workspace_source="missions/demo_auth_bug/repo",
            expected_test_target="tests",
        ),
        BenchmarkCaseSpec(
            case_id="case-a",
            name="Case A",
            mission_input="Fix A",
            workspace_source="missions/demo_auth_bug/repo",
            expected_test_target="tests/test_auth.py",
        ),
    )
    return BenchmarkSuiteSpec(suite_id=suite_id, version=version, name="Coding Core", cases=cases)


def integration_services(tmp_path):
    storage = AppStore()
    config = ModelConfig(model_id="model-a", provider_type="fake", model_name="fake")
    registry = ModelConfigRegistry((config,), "model-a")
    runs = RunService(
        registry=registry,
        provider_factory=ProviderFactory(fake_responses=default_fake_responses()),
        workspace_root=tmp_path / "workspaces",
        skills_root=Path(__file__).parents[1] / "skills",
        storage=storage,
    )
    suites = BenchmarkSuiteService(storage)
    experiments = ExperimentService(storage, registry, suites)
    execution = ExperimentExecutionService(storage, runs, experiments)
    analytics = ExperimentAnalyticsService(storage)
    return storage, registry, suites, experiments, execution, analytics


def test_experiment_requires_exactly_one_workload_source():
    target = (ExperimentModelTarget(model_id="model-a"),)
    with pytest.raises(ValidationError):
        ExperimentSpec(name="missing", models=target)
    with pytest.raises(ValidationError):
        ExperimentSpec(
            name="both",
            cases=(ExperimentCase(case_id="case", name="Case", mission_input="Fix", workspace_source="repo"),),
            benchmark_suite=BenchmarkSuiteProvenance(suite_id="coding-core", version=1, digest="digest"),
            models=target,
        )


def test_published_suite_is_snapshotted_and_execution_never_reloads_it(tmp_path):
    storage, registry, suites, experiments, execution, analytics = integration_services(tmp_path)
    created = suites.create(suite_spec())
    published = suites.publish(created.suite_id, created.version)
    provenance = BenchmarkSuiteProvenance(
        suite_id=published.suite_id,
        version=published.version,
        digest=published.spec_digest,
    )
    experiment = experiments.create(ExperimentSpec(
        name="suite experiment",
        benchmark_suite=provenance,
        models=(ExperimentModelTarget(model_id="model-a"),),
        repetitions=2,
    ))

    assert experiment.status == ExperimentStatus.DRAFT
    assert experiment.benchmark_suite == provenance
    assert [case.case_id for case in experiment.cases] == ["case-c", "case-a"]
    assert [case.workspace_source for case in experiment.cases] == [
        "missions/demo_auth_bug/repo",
        "missions/demo_auth_bug/repo",
    ]
    assert [case.expected_test_target for case in experiment.cases] == ["tests", "tests/test_auth.py"]
    assert storage.list_runs() == []
    assert storage.missions == {}

    stored_suite = storage.benchmark_suites[(published.suite_id, published.version)]
    storage.benchmark_suites[(published.suite_id, published.version)] = stored_suite.model_copy(update={"name": "drift"})

    class UnavailableSuiteService:
        def get(self, suite_id, version):
            raise AssertionError("suite repository was consulted after snapshot")

    experiments.benchmark_suites = UnavailableSuiteService()
    sealed = experiments.seal(experiment.experiment_id)
    completed = execution.execute(sealed.experiment_id)
    report = analytics.get(completed.experiment_id)

    assert completed.status == ExperimentStatus.COMPLETED
    assert [case.case_id for case in experiments.get(experiment.experiment_id).cases] == ["case-c", "case-a"]
    assert report.expected_run_count == report.materialized_run_count == 4
    assert report.integrity_status == "valid"
    assert registry.resolve("model-a").model_name == "fake"


def test_draft_unknown_and_corrupt_published_suite_fail_without_partial_experiment(tmp_path):
    storage, _, suites, experiments, _, _, = integration_services(tmp_path)
    with pytest.raises(BenchmarkSuiteNotFoundError):
        suites.get("missing", 1)

    draft = suites.create(suite_spec("draft", 1))
    draft_provenance = BenchmarkSuiteProvenance(suite_id="draft", version=1, digest="not-published")
    with pytest.raises(ExperimentCreationError, match="published"):
        experiments.create(ExperimentSpec(
            name="draft source",
            benchmark_suite=draft_provenance,
            models=(ExperimentModelTarget(model_id="model-a"),),
        ))
    assert draft.status == "DRAFT"
    assert storage.experiments == {}

    created = suites.create(suite_spec("corrupt", 1))
    published = suites.publish(created.suite_id, created.version)
    storage.benchmark_suites[(published.suite_id, published.version)] = published.model_copy(
        update={"spec_digest": "0" * 64}
    )
    corrupt_ref = BenchmarkSuiteProvenance(suite_id="corrupt", version=1, digest=published.spec_digest)
    with pytest.raises(ExperimentCreationError, match="digest"):
        experiments.create(ExperimentSpec(
            name="corrupt source",
            benchmark_suite=corrupt_ref,
            models=(ExperimentModelTarget(model_id="model-a"),),
        ))
    assert storage.experiments == {}


def test_suite_provenance_and_cases_survive_sqlite_restart(tmp_path):
    database = tmp_path / "suite-experiment.db"
    storage = SQLiteStore(database)
    suites = BenchmarkSuiteService(storage)
    created = suites.create(suite_spec("restart", 1))
    published = suites.publish(created.suite_id, created.version)
    registry = ModelConfigRegistry((ModelConfig(model_id="model-a", provider_type="fake", model_name="fake"),), "model-a")
    experiments = ExperimentService(storage, registry, suites)
    experiment = experiments.create(ExperimentSpec(
        name="restart experiment",
        benchmark_suite={
            "suite_id": published.suite_id,
            "version": published.version,
            "digest": published.spec_digest,
        },
        models=(ExperimentModelTarget(model_id="model-a"),),
    ))
    storage.close()

    restarted = SQLiteStore(database)
    restored = ExperimentService(restarted, registry).get(experiment.experiment_id)
    assert restored.benchmark_suite == experiment.benchmark_suite
    assert [case.case_id for case in restored.cases] == ["case-c", "case-a"]
    assert restored.expected_run_count == 2
    restarted.close()


def test_inline_experiment_keeps_null_suite_provenance_and_api_exposes_suite_snapshot():
    inline = ExperimentSpec(
        name="inline",
        cases=(ExperimentCase(case_id="inline", name="Inline", mission_input="Fix", workspace_source="repo"),),
        models=(ExperimentModelTarget(model_id="model-a"),),
    )
    assert inline.benchmark_suite is None

    from app.main import app

    client = TestClient(app)
    suite_id = f"api-integration-{uuid4().hex}"
    suite_payload = {
        "suite_id": suite_id,
        "version": 1,
        "name": "API integration suite",
        "cases": [{
            "case_id": "case-a",
            "name": "Case A",
            "mission_input": "Fix auth",
            "workspace_source": "missions/demo_auth_bug/repo",
            "expected_test_target": "tests",
        }],
    }
    assert client.post("/benchmark-suites", json=suite_payload).status_code == 200
    published = client.post(f"/benchmark-suites/{suite_id}/versions/1/publish")
    assert published.status_code == 200
    experiment = client.post("/experiments", json={
        "name": "suite API experiment",
        "benchmark_suite": {
            "suite_id": suite_id,
            "version": 1,
        },
        "models": [{"model_id": "fake-default"}],
        "repetitions": 1,
    })
    assert experiment.status_code == 200
    body = experiment.json()
    assert body["status"] == "DRAFT"
    assert body["benchmark_suite"]["suite_id"] == suite_id
    assert body["benchmark_suite"]["version"] == 1
    assert body["benchmark_suite"]["digest"] == published.json()["spec_digest"]
    assert body["cases"][0]["case_id"] == "case-a"
    assert "credential_ref" not in experiment.text
    assert "base_url" not in experiment.text
    unknown = client.post("/experiments", json={
        "name": "unknown suite experiment",
        "benchmark_suite": {"suite_id": suite_id, "version": 9},
        "models": [{"model_id": "fake-default"}],
    })
    assert unknown.status_code == 404
