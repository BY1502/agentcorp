import pytest
from app.domain.models import ModelConfig
from app.models.factory import ProviderFactory
from app.models.fake import FakeModelProvider

def test_model_config_and_credential_reference():
    config=ModelConfig(model_id='local',provider_type='fake',model_name='demo',credential_ref='keychain:local')
    assert config.credential_ref=='keychain:local' and 'secret-value' not in config.model_dump_json()

def test_disabled_model_is_rejected():
    with pytest.raises(ValueError,match='disabled'): ProviderFactory().create(ModelConfig(model_id='x',provider_type='fake',model_name='x',enabled=False))

def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError,match='unknown'): ProviderFactory().create(ModelConfig(model_id='x',provider_type='unknown',model_name='x'))

def test_factory_selects_fake_provider():
    provider=ProviderFactory(fake_responses=[{'output':{'ok':True}}]).create(ModelConfig(model_id='x',provider_type='fake',model_name='x'))
    assert isinstance(provider,FakeModelProvider)
