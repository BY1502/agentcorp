from collections.abc import Callable
from app.domain.models import ModelConfig, ModelExecutionSnapshot
from app.domain.contracts import ModelProvider
from .fake import FakeModelProvider

class ProviderFactory:
    def __init__(self, fake_responses=None, providers: dict[str, Callable[[ModelConfig], ModelProvider]] | None = None):
        self.fake_responses=fake_responses or []; self.providers=providers or {}
    def create(self, config: ModelConfig) -> ModelProvider:
        if not config.enabled: raise ValueError(f"model is disabled: {config.model_id}")
        if config.provider_type == "fake": return FakeModelProvider(self.fake_responses)
        builder=self.providers.get(config.provider_type)
        if builder is None: raise ValueError(f"unknown provider type: {config.provider_type}")
        return builder(config)

    def create_from_snapshot(self, snapshot: ModelExecutionSnapshot) -> ModelProvider:
        return self.create(
            ModelConfig(
                model_id=snapshot.model_id,
                provider_type=snapshot.provider_type,
                model_name=snapshot.model_name,
                base_url=snapshot.base_url,
                timeout=snapshot.timeout,
            )
        )
