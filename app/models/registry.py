from collections.abc import Iterable

from app.domain.models import ModelConfig


class UnknownModelError(LookupError):
    pass


class DisabledModelError(ValueError):
    pass


class ModelConfigRegistry:
    """Small in-memory model configuration source for the v0.1 application layer."""

    def __init__(self, configs: Iterable[ModelConfig], default_model_id: str):
        self._configs: dict[str, ModelConfig] = {}
        for config in configs:
            self.register(config)
        if default_model_id not in self._configs:
            raise UnknownModelError(f"unknown default model: {default_model_id}")
        self.default_model_id = default_model_id

    def register(self, config: ModelConfig) -> None:
        self._configs[config.model_id] = config.model_copy(deep=True)

    def resolve(self, model_id: str | None = None) -> ModelConfig:
        selected_id = model_id if model_id is not None else self.default_model_id
        config = self._configs.get(selected_id)
        if config is None:
            raise UnknownModelError(f"unknown model: {selected_id}")
        if not config.enabled:
            raise DisabledModelError(f"model is disabled: {selected_id}")
        return config.model_copy(deep=True)
