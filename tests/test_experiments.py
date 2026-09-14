import hashlib
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.domain.experiments import ExperimentCase, ExperimentModelTarget, ExperimentSpec, ExperimentStatus
from app.domain.models import ModelConfig, PolicyExecutionSnapshot, SkillVersion
from app.models.registry import DisabledModelError, ModelConfigRegistry, UnknownModelError
from app.persistence.sqlite import SQLiteStore
from app.services.experiments import ExperimentService
from app.services.store import AppStore


def spec(*model_ids, case_ids=("case-a", "case-b"), repetitions=2, runtime_config=None):
    model_ids = model_ids or ("model-a",)
    return ExperimentSpec(
        name="auth benchmark",
        description="same workload across frozen model targets",
        cases=tuple(
            ExperimentCase(
                case_id=case_id,
                name=case_id.title(),
                mission_input="Fix the authentication expiry bug",
                workspace_source="missions/demo_auth_bug/repo",
                expected_test_target="tests",
            )
            for case_id in case_ids
        ),
        models=tuple(ExperimentModelTarget(model_id=model_id) for model_id in model_ids),
        repetitions=repetitions,
        runtime_config=runtime_config or {"max_recovery_attempts": 1, "max_retries": 0},
    )


def registry(*configs):
    return ModelConfigRegistry(configs, configs[0].model_id)


def model(model_id="model-a", enabled=True, name="qwen3-8b"):
    return ModelConfig(
        model_id=model_id,
        provider_type="fake",
        model_name=name,
        base_url="http://user:secret@example.test:1234/v1?api_key=hidden",
        credential_ref="runtime-secret-ref",
        enabled=enabled,
    )


def test_create_is_draft_ordered_and_derives_expected_run_count_without_runs():
    storage = AppStore()
    service = ExperimentService(storage, registry(model(), model("model-b", name="other")))

    experiment = service.create(spec("model-a", "model-b", case_ids=("b", "a"), repetitions=3))

    assert experiment.status == ExperimentStatus.DRAFT
    assert experiment.expected_run_count == 12
    assert [item.case_id for item in experiment.cases] == ["b", "a"]
    assert [item.model_id for item in experiment.models] == ["model-a", "model-b"]
    assert all(item.snapshot is None for item in experiment.models)
    assert storage.list_runs() == []


def test_seal_resolves_all_models_to_safe_snapshots_and_is_idempotent():
    config = model()
    storage = AppStore()
    service = ExperimentService(storage, registry(config))
    experiment = service.create(spec("model-a", case_ids=("case-a",)))

    sealed = service.seal(experiment.experiment_id)
    again = service.seal(experiment.experiment_id)

    assert sealed.status == ExperimentStatus.SEALED
    assert sealed == again
    assert sealed.models[0].snapshot.model_name == "qwen3-8b"
    assert sealed.models[0].snapshot.base_url == "http://example.test:1234/v1"
    serialized = sealed.model_dump_json().lower()
    assert "credential_ref" not in serialized and "secret" not in serialized and "api_key" not in serialized


def test_seal_freezes_snapshot_against_registry_drift():
    original = model(name="original")
    changed = model(name="changed")
    storage = AppStore()
    models = registry(original)
    service = ExperimentService(storage, models)
    experiment = service.create(spec("model-a", case_ids=("case-a",), repetitions=1))
    sealed = service.seal(experiment.experiment_id)

    models.register(changed)

    stored = service.get(experiment.experiment_id)
    assert stored.models[0].snapshot.model_name == "original"
    assert service.seal(experiment.experiment_id).models[0].snapshot.model_name == "original"
    assert sealed == stored


def test_unknown_and_disabled_models_fail_without_fallback_or_partial_seal():
    storage = AppStore()
    service = ExperimentService(storage, registry(model("valid")))
    unknown = service.create(spec("missing", case_ids=("case-a",), repetitions=1))

    with pytest.raises(UnknownModelError):
        service.seal(unknown.experiment_id)
    assert service.get(unknown.experiment_id).status == ExperimentStatus.DRAFT
    assert service.get(unknown.experiment_id).models[0].snapshot is None

    disabled_storage = AppStore()
    disabled_service = ExperimentService(disabled_storage, registry(model("disabled", enabled=False)))
    disabled = disabled_service.create(spec("disabled", case_ids=("case-a",), repetitions=1))
    with pytest.raises(DisabledModelError):
        disabled_service.seal(disabled.experiment_id)
    assert disabled_service.get(disabled.experiment_id).status == ExperimentStatus.DRAFT


def test_multi_model_seal_is_atomic_when_one_target_cannot_resolve():
    storage = AppStore()
    service = ExperimentService(storage, registry(model("valid")))
    experiment = service.create(spec("valid", "missing", case_ids=("case-a",), repetitions=1))

    with pytest.raises(UnknownModelError):
        service.seal(experiment.experiment_id)

    current = service.get(experiment.experiment_id)
    assert current.status == ExperimentStatus.DRAFT
    assert [item.snapshot for item in current.models] == [None, None]


def test_spec_validation_rejects_empty_duplicates_and_host_paths():
    with pytest.raises(ValidationError):
        spec(case_ids=())
    with pytest.raises(ValidationError):
        ExperimentSpec(
            name="bad",
            cases=(ExperimentCase(case_id="a", name="A", mission_input="x", workspace_source="repo"),),
            models=(),
        )
    with pytest.raises(ValidationError):
        spec(*("model-a",), case_ids=("case-a",), repetitions=0)
    with pytest.raises(ValidationError):
        spec(*("model-a", "model-a"), case_ids=("case-a",))
    with pytest.raises(ValidationError):
        spec(*("model-a",), case_ids=("case-a", "case-a"))
    with pytest.raises(ValidationError):
        ExperimentSpec(
            name="bad",
            cases=(ExperimentCase(case_id="a", name="A", mission_input="x", workspace_source="/Users/me/repo"),),
            models=(ExperimentModelTarget(model_id="model-a"),),
        )
    with pytest.raises(ValidationError):
        ExperimentSpec(
            name="bad",
            cases=(ExperimentCase(case_id="a", name="A", mission_input="x", workspace_source="repo"),),
            models=(ExperimentModelTarget(model_id="model-a"),),
            runtime_config={"api_key": "secret"},
        )


def test_runtime_policy_and_skill_snapshots_round_trip():
    skill = SkillVersion(name="qa", version="1", content="# QA", checksum=hashlib.sha256(b"# QA").hexdigest())
    original = spec("model-a", case_ids=("case-a",), repetitions=1)
    original = original.model_copy(update={
        "policy_snapshot": PolicyExecutionSnapshot(mode="policy", policy_version="2", rules=(("edit_file", "require_approval"),)),
        "skill_versions": (skill,),
    })
    storage = SQLiteStore(":memory:")
    service = ExperimentService(storage, registry(model()))

    created = service.create(original)
    sealed = service.seal(created.experiment_id)
    restored = service.get(created.experiment_id)

    assert restored == sealed
    assert restored.policy_snapshot.policy_version == "2"
    assert restored.skill_versions[0].checksum == skill.checksum


def test_sealed_experiment_cannot_be_overwritten():
    storage = AppStore()
    service = ExperimentService(storage, registry(model()))
    sealed = service.seal(service.create(spec("model-a", case_ids=("case-a",), repetitions=1)).experiment_id)

    with pytest.raises(ValueError, match="immutable"):
        storage.save_experiment(sealed.model_copy(update={"name": "changed"}))
    with pytest.raises(ValidationError):
        sealed.name = "changed"


def test_create_and_seal_do_not_touch_workspace_or_create_run(tmp_path):
    marker = tmp_path / "marker.txt"
    marker.write_text("unchanged")
    before = marker.read_bytes()
    storage = AppStore()
    service = ExperimentService(storage, registry(model()))

    service.seal(service.create(spec("model-a", case_ids=("case-a",), repetitions=1)).experiment_id)

    assert marker.read_bytes() == before
    assert storage.list_runs() == []
    assert storage.events == {}


def test_restart_retrieval_is_stable_and_expected_count_is_not_persisted_truth(tmp_path):
    database = tmp_path / "experiments.db"
    storage = SQLiteStore(database)
    service = ExperimentService(storage, registry(model(), model("model-b", name="other")))
    sealed = service.seal(service.create(spec("model-a", "model-b", repetitions=4)).experiment_id)
    storage.close()

    restarted = SQLiteStore(database)
    restored = ExperimentService(restarted, registry(model())).get(sealed.experiment_id)
    restarted.close()
    raw = sqlite3.connect(str(database)).execute(
        "SELECT spec_json FROM experiments WHERE experiment_id = ?",
        (str(sealed.experiment_id),),
    ).fetchone()[0]

    assert restored == sealed
    assert restored.expected_run_count == 16
    assert "expected_run_count" in sealed.model_dump_json()
    assert "expected_run_count" not in raw


def test_api_create_get_seal_and_unknown_experiment_semantics():
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    payload = {
        "name": "api experiment",
        "cases": [{"case_id": "case-a", "name": "Case A", "mission_input": "Fix auth", "workspace_source": "missions/demo_auth_bug/repo"}],
        "models": [{"model_id": "fake-default"}],
        "repetitions": 1,
    }
    created = client.post("/experiments", json=payload)
    experiment_id = created.json()["experiment_id"]

    assert created.status_code == 200
    assert created.json()["status"] == "DRAFT"
    assert client.get(f"/experiments/{experiment_id}").json()["expected_run_count"] == 1
    sealed = client.post(f"/experiments/{experiment_id}/seal")
    assert sealed.status_code == 200 and sealed.json()["status"] == "SEALED"
    assert client.post(f"/experiments/{experiment_id}/seal").json() == sealed.json()
    assert client.get(f"/experiments/{experiment_id}").json() == sealed.json()
    assert client.get(f"/experiments/{experiment_id}").text.find("credential_ref") == -1
    assert client.get(f"/experiments/{experiment_id}").text.find("secret") == -1
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/experiments/{missing}").status_code == 404
    assert client.post(f"/experiments/{missing}/seal").status_code == 404


def test_api_unknown_model_seal_returns_404_without_default_fallback():
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    created = client.post("/experiments", json={
        "name": "unknown model",
        "cases": [{"case_id": "case-a", "name": "Case A", "mission_input": "Fix auth", "workspace_source": "missions/demo_auth_bug/repo"}],
        "models": [{"model_id": "not-registered"}],
        "repetitions": 1,
    })
    experiment_id = created.json()["experiment_id"]

    assert client.post(f"/experiments/{experiment_id}/seal").status_code == 404
    assert client.get(f"/experiments/{experiment_id}").json()["status"] == "DRAFT"
