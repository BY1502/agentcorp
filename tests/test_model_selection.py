from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.domain.models import ModelConfig
from app.models.factory import ProviderFactory
from app.models.fake import FakeModelProvider
from app.models.registry import ModelConfigRegistry
from app.services.run import RunService, default_fake_responses
from app.services.store import MissionRecord


client = TestClient(main_module.app)


class RecordingProviderFactory:
    def __init__(self):
        self.configs = []

    def create(self, config):
        self.configs.append(config.model_copy(deep=True))
        return FakeModelProvider(default_fake_responses())


def test_api_selects_valid_fake_model_and_exposes_safe_snapshot():
    mission = client.post("/missions", json={}).json()
    model_id = main_module.runs.registry.default_model_id

    response = client.post(
        f"/missions/{mission['id']}/runs",
        json={"model_id": model_id},
    )

    assert response.status_code == 200
    snapshot = response.json()["model_snapshot"]
    assert snapshot["model_id"] == model_id
    assert snapshot["provider_type"] == "fake"
    assert "credential_ref" not in snapshot


def test_api_omitted_model_id_preserves_configured_default():
    mission = client.post("/missions", json={}).json()

    response = client.post(f"/missions/{mission['id']}/runs")

    assert response.status_code == 200
    assert response.json()["model_snapshot"]["model_id"] == main_module.runs.registry.default_model_id


def test_api_rejects_unknown_model_without_fallback():
    mission = client.post("/missions", json={}).json()

    response = client.post(
        f"/missions/{mission['id']}/runs",
        json={"model_id": "does-not-exist"},
    )

    assert response.status_code == 404
    assert "unknown model" in response.json()["detail"]


def test_api_rejects_disabled_model_without_fallback(monkeypatch):
    registry = ModelConfigRegistry(
        configs=(
            ModelConfig(model_id="fake-default", provider_type="fake", model_name="fake"),
            ModelConfig(
                model_id="disabled-model",
                provider_type="fake",
                model_name="disabled",
                enabled=False,
            ),
        ),
        default_model_id="fake-default",
    )
    monkeypatch.setattr(main_module, "runs", RunService(registry=registry))
    mission = client.post("/missions", json={}).json()

    response = client.post(
        f"/missions/{mission['id']}/runs",
        json={"model_id": "disabled-model"},
    )

    assert response.status_code == 409
    assert "disabled" in response.json()["detail"]


def test_run_start_resolves_lm_config_without_network_and_freezes_safe_snapshot(tmp_path):
    registry = ModelConfigRegistry(
        configs=(
            ModelConfig(
                model_id="local-qwen",
                provider_type="lmstudio",
                model_name="qwen3-8b",
                base_url="http://user:password@example.test:1234/v1?api_key=query-secret#fragment",
                timeout=180,
                credential_ref="keychain:lmstudio",
            ),
        ),
        default_model_id="local-qwen",
    )
    factory = RecordingProviderFactory()
    service = RunService(
        registry=registry,
        provider_factory=factory,
        workspace_root=tmp_path / "workspaces",
        skills_root=Path(__file__).parents[1] / "skills",
    )

    result = service.start(MissionRecord("demo", "missions/demo_auth_bug/repo"), "local-qwen")
    snapshot = result.execution_manifest.model_snapshot

    assert snapshot is not None
    assert snapshot.model_id == "local-qwen"
    assert snapshot.provider_type == "lmstudio"
    assert snapshot.model_name == "qwen3-8b"
    assert snapshot.base_url == "http://example.test:1234/v1"
    assert snapshot.timeout == 180
    assert factory.configs[0].model_name == "qwen3-8b"
    assert set(snapshot.model_dump()) == {
        "model_id",
        "provider_type",
        "model_name",
        "base_url",
        "timeout",
    }
    serialized = snapshot.model_dump_json()
    assert "credential_ref" not in serialized
    assert "password" not in serialized
    assert "query-secret" not in serialized


def test_model_snapshot_and_provider_identity_do_not_change_after_registry_mutation(tmp_path):
    original = ModelConfig(
        model_id="selected",
        provider_type="fake",
        model_name="fake-v1",
        base_url="http://127.0.0.1:1234",
        timeout=120,
    )
    registry = ModelConfigRegistry((original,), default_model_id="selected")
    factory = RecordingProviderFactory()
    service = RunService(
        registry=registry,
        provider_factory=factory,
        workspace_root=tmp_path / "workspaces",
        skills_root=Path(__file__).parents[1] / "skills",
    )

    result = service.start(MissionRecord("demo", "missions/demo_auth_bug/repo"), "selected")
    registry.register(
        ModelConfig(
            model_id="selected",
            provider_type="lmstudio",
            model_name="changed-after-start",
            base_url="http://changed.example",
            timeout=1,
        )
    )

    stored = service.get(result.mission_run_id)
    assert stored is not None
    assert stored.execution_manifest.model_snapshot.model_name == "fake-v1"
    assert stored.execution_manifest.model_snapshot.base_url == "http://127.0.0.1:1234"
    assert factory.configs[0].provider_type == "fake"
    assert factory.configs[0].model_name == "fake-v1"
    assert registry.resolve("selected").model_name == "changed-after-start"


def test_historical_run_result_preserves_model_snapshot_after_current_config_changes(tmp_path):
    registry = ModelConfigRegistry(
        (ModelConfig(model_id="selected", provider_type="fake", model_name="historical-v1"),),
        default_model_id="selected",
    )
    service = RunService(
        registry=registry,
        provider_factory=RecordingProviderFactory(),
        workspace_root=tmp_path / "workspaces",
        skills_root=Path(__file__).parents[1] / "skills",
    )
    result = service.start(MissionRecord("demo", "missions/demo_auth_bug/repo"), "selected")
    historical_snapshot = result.execution_manifest.model_snapshot.model_copy(deep=True)

    registry.register(
        ModelConfig(model_id="selected", provider_type="fake", model_name="current-v2")
    )

    restored = service.get(result.mission_run_id)
    assert restored is not None
    assert restored.execution_manifest.model_snapshot == historical_snapshot
    assert restored.execution_manifest.model_snapshot.model_name == "historical-v1"


def test_model_config_validation_rejects_non_positive_timeout():
    with pytest.raises(ValueError, match="timeout must be positive"):
        ModelConfig(model_id="invalid", provider_type="fake", model_name="invalid", timeout=0)


def test_provider_factory_routes_registered_provider_type_without_network():
    received = []
    factory = ProviderFactory(
        providers={
            "lmstudio": lambda config: (
                received.append(config),
                FakeModelProvider([]),
            )[1]
        }
    )
    config = ModelConfig(
        model_id="local-qwen",
        provider_type="lmstudio",
        model_name="qwen3-8b",
        base_url="http://127.0.0.1:1234",
    )

    provider = factory.create(config)

    assert isinstance(provider, FakeModelProvider)
    assert received[0].model_id == "local-qwen"
