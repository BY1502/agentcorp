from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.domain.experiment_analytics import ExperimentIntegrityStatus
from app.domain.experiments import ExperimentCell, ExperimentCase, ExperimentModelTarget, ExperimentSpec, ExperimentStatus
from app.domain.models import ModelConfig, ModelExecutionSnapshot
from app.models.factory import ProviderFactory
from app.models.registry import ModelConfigRegistry
from app.services.experiment_analytics import ExperimentAnalyticsService
from app.services.experiments import ExperimentExecutionService, ExperimentService
from app.services.run import RunService, default_fake_responses
from app.services.store import AppStore


def model(model_id="model-a", name="qwen3-8b"):
    return ModelConfig(model_id=model_id, provider_type="fake", model_name=name)


def specification(model_ids=("model-a",), case_ids=("case-a", "case-b"), repetitions=2):
    return ExperimentSpec(
        name="analytics experiment",
        cases=tuple(
            ExperimentCase(
                case_id=case_id,
                name=case_id,
                mission_input="Fix auth",
                workspace_source="missions/demo_auth_bug/repo",
            )
            for case_id in case_ids
        ),
        models=tuple(ExperimentModelTarget(model_id=model_id) for model_id in model_ids),
        repetitions=repetitions,
        runtime_config={"max_recovery_attempts": 1},
    )


def setup_services(tmp_path, *, model_configs=(model(),), case_ids=("case-a", "case-b"), repetitions=2):
    storage = AppStore()
    registry = ModelConfigRegistry(model_configs, model_configs[0].model_id)
    runs = RunService(
        registry=registry,
        provider_factory=ProviderFactory(fake_responses=default_fake_responses()),
        workspace_root=tmp_path / "workspaces",
        skills_root=Path(__file__).parents[1] / "skills",
        storage=storage,
    )
    experiments = ExperimentService(storage, registry)
    execution = ExperimentExecutionService(storage, runs, experiments)
    analytics = ExperimentAnalyticsService(storage)
    spec = specification(tuple(config.model_id for config in model_configs), case_ids, repetitions)
    return storage, registry, runs, experiments, execution, analytics, spec


def test_draft_and_sealed_unexecuted_analytics_are_read_only(tmp_path):
    storage, registry, _, experiments, execution, analytics, spec = setup_services(tmp_path, case_ids=("case-a",), repetitions=1)
    draft = experiments.create(spec)

    draft_report = analytics.get(draft.experiment_id)
    sealed = experiments.seal(draft.experiment_id)
    sealed_report = analytics.get(sealed.experiment_id)

    assert draft_report.experiment_status == ExperimentStatus.DRAFT
    assert draft_report.expected_run_count == 1
    assert draft_report.materialized_run_count == 0
    assert draft_report.models == () and draft_report.cases == ()
    assert sealed_report.experiment_status == ExperimentStatus.SEALED
    assert sealed_report.materialized_run_count == 0
    assert sealed_report.integrity_status == ExperimentIntegrityStatus.INCOMPLETE
    assert sealed_report.models[0].expected_cell_count == 1
    assert sealed_report.models[0].materialized_cell_count == 0
    assert storage.list_experiment_cells(sealed.experiment_id) == []
    assert storage.list_runs() == []
    assert execution is not None and registry.default_model_id == "model-a"


def test_analytics_reuses_metrics_evaluation_and_preserves_matrix_order(tmp_path):
    configs = (model(), model("model-b", "gemma"))
    storage, registry, _, experiments, execution, analytics, spec = setup_services(tmp_path, model_configs=configs)
    sealed = experiments.seal(experiments.create(spec).experiment_id)
    execution.execute(sealed.experiment_id)

    before = analytics.get(sealed.experiment_id)
    registry.resolve = lambda model_id=None: (_ for _ in ()).throw(AssertionError("registry resolved during analytics"))
    after = analytics.get(sealed.experiment_id)

    assert before == after
    assert after.experiment_status == ExperimentStatus.COMPLETED
    assert after.integrity_status == ExperimentIntegrityStatus.VALID
    assert after.expected_run_count == after.materialized_run_count == 8
    assert after.terminal_run_count == 8 and after.non_terminal_run_count == 0
    assert after.overall.passed_count == 8
    assert after.overall.terminal_pass_rate == 1.0
    assert [(item.model_id, item.model_name) for item in after.models] == [("model-a", "qwen3-8b"), ("model-b", "gemma")]
    assert [(item.case_id, len(item.models)) for item in after.cases] == [("case-a", 2), ("case-b", 2)]
    assert all(item.expected_cell_count == item.materialized_cell_count == 4 for item in after.models)
    assert all(item.terminal_cell_count == 4 and item.passed_count == 4 for item in after.models)
    for case in after.cases:
        assert all(item.expected_cell_count == item.materialized_cell_count == 2 for item in case.models)
    assert not hasattr(after, "winner") and not hasattr(after, "rank")


def test_mixed_and_all_failed_runs_are_results_not_experiment_failure(tmp_path):
    storage, _, _, experiments, execution, analytics, spec = setup_services(tmp_path, case_ids=("case-a",), repetitions=4)
    sealed = experiments.seal(experiments.create(spec).experiment_id)
    execution.execute(sealed.experiment_id)
    cells = storage.list_experiment_cells(sealed.experiment_id)
    for cell in cells[:2]:
        run = storage.get_run(cell.run_id)
        storage.save_run(run.model_copy(update={"status": "FAILED"}))

    mixed = analytics.get(sealed.experiment_id)
    assert mixed.experiment_status == ExperimentStatus.COMPLETED
    assert mixed.integrity_status == ExperimentIntegrityStatus.VALID
    assert mixed.overall.passed_count == 2
    assert mixed.overall.failed_count == 2
    assert mixed.overall.terminal_pass_rate == 0.5

    for cell in cells[2:]:
        run = storage.get_run(cell.run_id)
        storage.save_run(run.model_copy(update={"status": "FAILED"}))
    failed = analytics.get(sealed.experiment_id)
    assert failed.experiment_status == ExperimentStatus.COMPLETED
    assert failed.overall.passed_count == 0
    assert failed.overall.failed_count == 4
    assert failed.overall.terminal_pass_rate == 0.0


def test_waiting_approval_is_non_terminal_and_does_not_become_failure(tmp_path):
    storage, _, _, experiments, execution, analytics, spec = setup_services(tmp_path, case_ids=("case-a",), repetitions=2)
    sealed = experiments.seal(experiments.create(spec).experiment_id)
    execution.execute(sealed.experiment_id)
    cells = storage.list_experiment_cells(sealed.experiment_id)
    waiting = storage.get_run(cells[0].run_id).model_copy(update={"status": "WAITING_APPROVAL"})
    storage.save_run(waiting)
    experiments.update_status(experiments.get(sealed.experiment_id), ExperimentStatus.RUNNING)

    report = analytics.get(sealed.experiment_id)

    assert report.experiment_status == ExperimentStatus.RUNNING
    assert report.integrity_status == ExperimentIntegrityStatus.INCOMPLETE
    assert report.materialized_run_count == 2
    assert report.terminal_run_count == 1
    assert report.non_terminal_run_count == 1
    assert report.overall.failed_count == 0


def test_integrity_surfaces_missing_run_and_snapshot_mismatch_without_silent_skip(tmp_path):
    storage, _, _, experiments, execution, analytics, spec = setup_services(tmp_path, case_ids=("case-a",), repetitions=2)
    sealed = experiments.seal(experiments.create(spec).experiment_id)
    execution.execute(sealed.experiment_id)
    cells = storage.list_experiment_cells(sealed.experiment_id)
    missing = cells[0]
    del storage.runs[missing.run_id]
    report = analytics.get(sealed.experiment_id)
    assert report.integrity_status == ExperimentIntegrityStatus.INVALID
    assert "missing_mapped_run" in report.integrity_issues
    assert report.materialized_run_count == 2

    restored_storage, _, _, restored_experiments, restored_execution, restored_analytics, restored_spec = setup_services(tmp_path / "mismatch", case_ids=("case-a",), repetitions=1)
    restored = restored_experiments.seal(restored_experiments.create(restored_spec).experiment_id)
    restored_execution.execute(restored.experiment_id)
    cell = restored_storage.list_experiment_cells(restored.experiment_id)[0]
    run = restored_storage.get_run(cell.run_id)
    wrong = ModelExecutionSnapshot(model_id="model-a", provider_type="fake", model_name="wrong", timeout=120)
    manifest = run.execution_manifest.model_copy(update={"model_snapshot": wrong})
    restored_storage.save_run(run.model_copy(update={"execution_manifest": manifest}))
    mismatch = restored_analytics.get(restored.experiment_id)
    assert mismatch.integrity_status == ExperimentIntegrityStatus.INVALID
    assert "model_snapshot_mismatch" in mismatch.integrity_issues
    assert mismatch.models[0].materialized_cell_count == 1
    assert mismatch.models[0].passed_count == 0


class CorruptCellStore(AppStore):
    def __init__(self, cells):
        super().__init__()
        self.corrupt_cells = cells

    def list_experiment_cells(self, experiment_id):
        return [cell.model_copy(deep=True) for cell in self.corrupt_cells if cell.experiment_id == experiment_id]


def test_unexpected_cell_is_invalid_and_analytics_never_materializes(tmp_path):
    storage, _, _, experiments, _, _, spec = setup_services(tmp_path, case_ids=("case-a",), repetitions=1)
    sealed = experiments.seal(experiments.create(spec).experiment_id)
    expected = ExperimentCell(
        cell_id=uuid4(),
        experiment_id=sealed.experiment_id,
        case_id="not-a-case",
        case_index=99,
        model_id="model-a",
        model_index=0,
        repetition_index=0,
    )
    corrupt = CorruptCellStore([expected])
    corrupt.experiments[sealed.experiment_id] = sealed
    report = ExperimentAnalyticsService(corrupt).get(sealed.experiment_id)
    assert report.integrity_status == ExperimentIntegrityStatus.INVALID
    assert "unexpected_experiment_cell" in report.integrity_issues
    assert report.materialized_run_count == 0


def test_analytics_api_is_read_only_and_unknown_experiment_is_404():
    from app.main import app

    client = TestClient(app)
    created = client.post("/experiments", json={
        "name": "analytics api",
        "cases": [{"case_id": "case-a", "name": "Case A", "mission_input": "Fix auth", "workspace_source": "missions/demo_auth_bug/repo"}],
        "models": [{"model_id": "fake-default"}],
        "repetitions": 1,
    })
    experiment_id = created.json()["experiment_id"]
    assert client.post(f"/experiments/{experiment_id}/seal").status_code == 200
    report = client.get(f"/experiments/{experiment_id}/analytics")
    assert report.status_code == 200
    assert report.json()["expected_run_count"] == 1
    assert report.json()["materialized_run_count"] == 0
    assert "secret" not in report.text and "credential_ref" not in report.text

    missing = "00000000-0000-0000-0000-000000000000"
    response = client.get(f"/experiments/{missing}/analytics")
    assert response.status_code == 404
