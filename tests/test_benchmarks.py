import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.domain.benchmarks import (
    BenchmarkCase,
    BenchmarkCaseSpec,
    BenchmarkSuiteSpec,
    BenchmarkSuiteStatus,
    benchmark_suite_digest,
)
from app.persistence.sqlite import SQLiteStore
from app.services.benchmarks import (
    BenchmarkSuiteConflictError,
    BenchmarkSuiteService,
    BenchmarkSuiteValidationError,
)
from app.services.store import AppStore


def case(case_id="auth", *, mission_input="Fix auth", workspace_source="missions/demo_auth_bug/repo"):
    return BenchmarkCaseSpec(
        case_id=case_id,
        name=case_id.title(),
        description="deterministic coding case",
        mission_input=mission_input,
        workspace_source=workspace_source,
        expected_test_target="tests",
    )


def spec(suite_id="coding-core", version=1, cases=(case(),), description="metadata"):
    return BenchmarkSuiteSpec(
        suite_id=suite_id,
        version=version,
        name="Coding Core",
        description=description,
        cases=cases,
    )


def test_case_reuses_experiment_definition_without_exposing_runtime_fields():
    assert BenchmarkCase is BenchmarkCaseSpec
    benchmark_case = case()
    assert benchmark_case.workspace_source == "missions/demo_auth_bug/repo"
    assert benchmark_case.expected_test_target == "tests"
    with pytest.raises(ValidationError):
        BenchmarkCaseSpec(**{**benchmark_case.model_dump(), "credential_ref": "secret-ref"})


def test_create_is_draft_and_duplicate_versions_are_rejected():
    storage = AppStore()
    service = BenchmarkSuiteService(storage)

    created = service.create(spec())

    assert created.status == BenchmarkSuiteStatus.DRAFT
    assert created.spec_digest is None
    assert service.get("coding-core", 1) == created
    assert storage.list_runs() == []
    with pytest.raises(BenchmarkSuiteConflictError):
        service.create(spec())
    with pytest.raises(ValidationError):
        spec(cases=(case("auth"), case("auth")))
    with pytest.raises(ValidationError):
        BenchmarkSuiteSpec(suite_id="coding-core", version=0, name="bad")


def test_publish_validates_atomically_and_is_idempotent():
    storage = AppStore()
    service = BenchmarkSuiteService(storage)
    created = service.create(spec())

    published = service.publish(created.suite_id, created.version)
    assert published.status == BenchmarkSuiteStatus.PUBLISHED
    assert published.spec_digest == benchmark_suite_digest(created)
    assert service.publish(created.suite_id, created.version) == published

    with pytest.raises(ValueError, match="immutable"):
        storage.save_benchmark_suite(published.model_copy(update={"name": "changed"}))

    empty = service.create(spec("empty", cases=()))
    with pytest.raises(BenchmarkSuiteValidationError, match="at least one"):
        service.publish(empty.suite_id, empty.version)
    assert service.get("empty", 1).status == BenchmarkSuiteStatus.DRAFT
    assert service.get("empty", 1).spec_digest is None

    invalid = service.create(spec("invalid", cases=(case(workspace_source="missing/fixture"),)))
    with pytest.raises(BenchmarkSuiteValidationError, match="workspace source"):
        service.publish(invalid.suite_id, invalid.version)
    unchanged = service.get("invalid", 1)
    assert unchanged.status == BenchmarkSuiteStatus.DRAFT
    assert unchanged.spec_digest is None


def test_digest_is_ordered_and_excludes_description_metadata():
    original = spec()
    same_execution = spec(description="different documentation")
    reversed_cases = spec(cases=(case("validation"), case("auth")))
    changed_case = spec(cases=(case(mission_input="Fix a different bug"),))

    assert benchmark_suite_digest(original) == benchmark_suite_digest(same_execution)
    assert benchmark_suite_digest(original) != benchmark_suite_digest(reversed_cases)
    assert benchmark_suite_digest(original) != benchmark_suite_digest(changed_case)


def test_sqlite_suite_persistence_restart_and_digest_are_stable(tmp_path):
    database = tmp_path / "benchmarks.db"
    storage = SQLiteStore(database)
    service = BenchmarkSuiteService(storage)
    created = service.create(spec())
    published = service.publish(created.suite_id, created.version)
    storage.close()

    connection = sqlite3.connect(str(database))
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
    connection.close()

    restarted = SQLiteStore(database)
    restored = BenchmarkSuiteService(restarted).get("coding-core", 1)
    assert restored == published
    assert restarted.list_benchmark_suites() == [published]
    restarted.close()


def test_api_crud_publish_ordering_and_404_semantics():
    from app.main import app

    client = TestClient(app)
    suite_prefix = f"api-{uuid4().hex}"
    payload = {
        "suite_id": f"{suite_prefix}-coding-core",
        "version": 1,
        "name": "API Coding Core",
        "description": "reusable workload",
        "cases": [{
            "case_id": "auth",
            "name": "Auth",
            "description": "auth fixture",
            "mission_input": "Fix auth",
            "workspace_source": "missions/demo_auth_bug/repo",
            "expected_test_target": "tests",
        }],
    }
    created = client.post("/benchmark-suites", json=payload)
    assert created.status_code == 200
    assert created.json()["status"] == "DRAFT"
    assert created.json()["spec_digest"] is None
    assert client.get(f"/benchmark-suites/{suite_prefix}-coding-core/versions/1").json() == created.json()
    assert client.post("/benchmark-suites", json=payload).status_code == 409

    published = client.post(f"/benchmark-suites/{suite_prefix}-coding-core/versions/1/publish")
    assert published.status_code == 200
    assert published.json()["status"] == "PUBLISHED"
    assert published.json()["spec_digest"]
    assert client.post(f"/benchmark-suites/{suite_prefix}-coding-core/versions/1/publish").json() == published.json()
    assert "credential_ref" not in published.text
    assert "secret" not in published.text
    assert "/Users/" not in published.text

    second = {**payload, "version": 2}
    assert client.post("/benchmark-suites", json=second).status_code == 200
    listed = client.get("/benchmark-suites")
    assert listed.status_code == 200
    selected = [item for item in listed.json() if item["suite_id"].startswith(suite_prefix)]
    assert [(item["suite_id"], item["version"]) for item in selected] == [
        (f"{suite_prefix}-coding-core", 1),
        (f"{suite_prefix}-coding-core", 2),
    ]
    assert client.get("/benchmark-suites/missing/versions/1").status_code == 404
    assert client.get(f"/benchmark-suites/{suite_prefix}-coding-core/versions/9").status_code == 404
    assert client.post("/benchmark-suites/missing/versions/1/publish").status_code == 404


def test_api_rejects_forbidden_suite_fields():
    from app.main import app

    client = TestClient(app)
    response = client.post("/benchmark-suites", json={
        "suite_id": "unsafe-suite",
        "version": 1,
        "name": "Unsafe",
        "credential_ref": "runtime-secret",
    })
    assert response.status_code == 422
