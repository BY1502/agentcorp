import hashlib
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.domain.experiments import ExperimentCase, ExperimentModelTarget, ExperimentSpec, ExperimentStatus
from app.domain.models import ModelConfig, PolicyExecutionSnapshot, SkillVersion
from app.models.factory import ProviderFactory
from app.models.registry import DisabledModelError, ModelConfigRegistry, UnknownModelError
from app.persistence.sqlite import SQLiteStore
from app.services.experiments import (
    ExperimentExecutionError,
    ExperimentExecutionService,
    ExperimentService,
    expand_experiment,
)
from app.services.run import RunService, default_fake_responses
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


def executor(tmp_path, storage=None, configs=None):
    storage = storage or AppStore()
    configs = configs or (model(),)
    registry_instance = registry(*configs)
    run_service = RunService(
        registry=registry_instance,
        provider_factory=ProviderFactory(fake_responses=default_fake_responses()),
        workspace_root=tmp_path / "workspaces",
        skills_root=Path(__file__).parents[1] / "skills",
        storage=storage,
    )
    experiment_service = ExperimentService(storage, registry_instance)
    execution = ExperimentExecutionService(storage, run_service, experiment_service)
    return storage, run_service, experiment_service, execution


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


def test_expand_is_canonical_and_has_unique_deterministic_cell_ids():
    storage = AppStore()
    service = ExperimentService(storage, registry(model(), model("model-b", name="other")))
    sealed = service.seal(service.create(spec("model-a", "model-b", repetitions=2)).experiment_id)

    cells = expand_experiment(sealed)
    assert len(cells) == sealed.expected_run_count == 8
    assert [(cell.case_id, cell.model_id, cell.repetition_index) for cell in cells] == [
        (case_id, model_id, repetition)
        for case_id in ("case-a", "case-b")
        for model_id in ("model-a", "model-b")
        for repetition in (0, 1)
    ]
    assert len({cell.cell_id for cell in cells}) == 8
    assert cells == expand_experiment(sealed)


def test_draft_cannot_execute_and_sealed_matrix_reuses_existing_run_flow(tmp_path):
    storage, run_service, service, execution = executor(tmp_path)
    draft = service.create(spec("model-a", case_ids=("case-a",), repetitions=1))
    with pytest.raises(ExperimentExecutionError, match="sealed"):
        execution.execute(draft.experiment_id)

    sealed = service.seal(draft.experiment_id)
    completed = execution.execute(sealed.experiment_id)
    cells = storage.list_experiment_cells(sealed.experiment_id)
    run = storage.get_run(cells[0].run_id)

    assert completed.status == ExperimentStatus.COMPLETED
    assert len(cells) == len(storage.list_runs()) == 1
    assert cells[0].mission_id == run.mission_id
    assert cells[0].run_id == run.mission_run_id
    assert run.execution_manifest.model_snapshot == sealed.models[0].snapshot
    assert run.execution_manifest.runtime_config == sealed.runtime_config
    assert run.execution_manifest.initial_workspace_snapshot_id == cells[0].workspace_snapshot_id
    assert Path(run.workspace_reference).is_dir()

    execution.execute(sealed.experiment_id)
    assert len(storage.list_experiment_cells(sealed.experiment_id)) == 1
    assert len(storage.list_runs()) == 1
    assert run_service.registry.resolve("model-a").model_name == "qwen3-8b"


def test_execution_does_not_reresolve_registry_and_isolates_matrix_runs(tmp_path):
    configs = (model(), model("model-b", name="other"))
    storage, run_service, service, execution = executor(tmp_path, configs=configs)
    sealed = service.seal(service.create(spec("model-a", "model-b", repetitions=2)).experiment_id)

    def forbidden_resolve(model_id=None):
        raise AssertionError(f"registry was resolved during execution: {model_id}")

    run_service.registry.resolve = forbidden_resolve
    completed = execution.execute(sealed.experiment_id)
    cells = storage.list_experiment_cells(sealed.experiment_id)
    runs = [storage.get_run(cell.run_id) for cell in cells]

    assert completed.status == ExperimentStatus.COMPLETED
    assert [
        (cell.case_id, cell.model_id, cell.repetition_index)
        for cell in cells
    ] == [
        (case_id, model_id, repetition)
        for case_id in ("case-a", "case-b")
        for model_id in ("model-a", "model-b")
        for repetition in (0, 1)
    ]
    assert len({run.mission_run_id for run in runs}) == 8
    assert len({run.mission_id for run in runs}) == 8
    assert len({run.workspace_reference for run in runs}) == 8
    assert len({cell.workspace_snapshot_id for cell in cells if cell.case_id == "case-a"}) == 1
    assert len({cell.workspace_snapshot_id for cell in cells if cell.case_id == "case-b"}) == 1
    assert {run.status for run in runs} == {"PASSED"}
    assert all(run.execution_manifest.runtime_config == runs[0].execution_manifest.runtime_config for run in runs)
    assert all(run.execution_manifest.policy_snapshot == runs[0].execution_manifest.policy_snapshot for run in runs)
    assert all(run.execution_manifest.skill_versions == runs[0].execution_manifest.skill_versions for run in runs)


def test_sqlite_restart_keeps_cells_and_execute_is_idempotent(tmp_path):
    database = tmp_path / "experiment-execution.db"
    storage = SQLiteStore(database)
    storage, run_service, service, execution = executor(tmp_path, storage=storage)
    sealed = service.seal(service.create(spec("model-a", case_ids=("case-a",), repetitions=2)).experiment_id)
    completed = execution.execute(sealed.experiment_id)
    before = storage.list_experiment_cells(sealed.experiment_id)
    storage.close()

    reopened = SQLiteStore(database)
    _, restarted_run_service, restarted_service, restarted_execution = executor(tmp_path, storage=reopened)
    restarted_run_service.registry.resolve = lambda model_id=None: (_ for _ in ()).throw(AssertionError("registry re-resolved"))
    again = restarted_execution.execute(sealed.experiment_id)
    after = reopened.list_experiment_cells(sealed.experiment_id)

    assert completed.status == again.status == ExperimentStatus.COMPLETED
    assert after == before
    assert len(reopened.list_runs()) == 2
    assert restarted_service.get(sealed.experiment_id).status == ExperimentStatus.COMPLETED
    reopened.close()


def test_waiting_approval_is_non_terminal_and_reentry_does_not_duplicate_run(tmp_path):
    storage = AppStore()
    registry_instance = registry(model())
    run_service = RunService(
        registry=registry_instance,
        provider_factory=ProviderFactory(fake_responses=default_fake_responses()),
        workspace_root=tmp_path / "workspaces",
        skills_root=Path(__file__).parents[1] / "skills",
        storage=storage,
        approval_mode="policy",
        policy_rules={"edit_file": "require_approval"},
    )
    service = ExperimentService(storage, registry_instance)
    execution = ExperimentExecutionService(storage, run_service, service)
    policy = run_service.current_policy_snapshot()
    sealed = service.seal(service.create(spec("model-a", case_ids=("case-a",), repetitions=1).model_copy(update={"policy_snapshot": policy})).experiment_id)

    running = execution.execute(sealed.experiment_id)
    first_cells = storage.list_experiment_cells(sealed.experiment_id)
    second = execution.execute(sealed.experiment_id)
    second_cells = storage.list_experiment_cells(sealed.experiment_id)

    assert running.status == second.status == ExperimentStatus.RUNNING
    assert storage.get_run(first_cells[0].run_id).status == "WAITING_APPROVAL"
    assert second_cells == first_cells
    assert len(storage.list_runs()) == 1


def test_failed_terminal_cell_is_not_automatically_rerun(tmp_path):
    storage, _, service, execution = executor(tmp_path)
    sealed = service.seal(service.create(spec("model-a", case_ids=("case-a",), repetitions=1)).experiment_id)
    execution.execute(sealed.experiment_id)
    cell = storage.list_experiment_cells(sealed.experiment_id)[0]
    failed = storage.get_run(cell.run_id).model_copy(update={"status": "FAILED"})
    storage.save_run(failed)
    service.update_status(service.get(sealed.experiment_id), ExperimentStatus.RUNNING)

    completed = execution.execute(sealed.experiment_id)

    assert completed.status == ExperimentStatus.COMPLETED
    assert len(storage.list_runs()) == 1
    assert storage.get_run(cell.run_id).status == "FAILED"


def test_experiment_runs_api_returns_safe_canonical_views():
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    created = client.post("/experiments", json={
        "name": "api execution",
        "cases": [{"case_id": "case-a", "name": "Case A", "mission_input": "Fix auth", "workspace_source": "missions/demo_auth_bug/repo"}],
        "models": [{"model_id": "fake-default"}],
        "repetitions": 1,
    })
    experiment_id = created.json()["experiment_id"]
    assert client.post(f"/experiments/{experiment_id}/seal").status_code == 200
    executed = client.post(f"/experiments/{experiment_id}/execute")
    views = client.get(f"/experiments/{experiment_id}/runs")

    assert executed.status_code == 200
    assert executed.json()["status"] == "COMPLETED"
    assert views.status_code == 200
    assert len(views.json()) == 1
    assert views.json()[0]["run_status"] == "PASSED"
    assert set(views.json()[0]) == {
        "cell_id", "experiment_id", "case_id", "model_id", "provider_type",
        "model_name", "repetition_index", "mission_id", "run_id", "run_status",
    }
    assert "credential_ref" not in views.text
    assert "base_url" not in views.text
