"""Model discovery uses mock HTTP only, never paid inference."""
import json
from unittest.mock import Mock, patch

import pytest
from application import model_config


def discover(payload=None, *, status=200, data=None, credentials=None):
    connection = Mock()
    response = connection.getresponse.return_value
    response.status = status
    response.read.return_value = json.dumps(payload or {'data': [{'id': 'gpt-example'}]}).encode()
    with patch.object(model_config.store, 'load_credentials', return_value=credentials or {}), \
            patch('integrations.model.client.PinnedHTTPSConnection', return_value=connection):
        result = model_config.list_models(data or {'model_base_url': 'https://proxy.example/v1', 'api_key': 'secret'})
    return result, connection


def test_discovery_lists_sorted_unique_ids_without_saving_or_inference():
    with patch.object(model_config.store, 'save_credentials') as save, patch.object(model_config, 'ModelClient') as model:
        result, connection = discover({'data': [{'id': 'z'}, {'id': 'a'}, {'id': 'z'}, {'id': 'bad id'}, {}, {'id': 'secret'}]})
        assert result == {'ok': True, 'models': ['a', 'z']}
        connection.request.assert_called_once_with('GET', '/v1/models', headers={'Authorization': 'Bearer secret'})
        connection.close.assert_called_once()
        save.assert_not_called()
        model.assert_not_called()


def test_same_saved_endpoint_can_reuse_its_key():
    _, connection = discover(data={'model_base_url': 'https://proxy.example/v1/'}, credentials={
        'model_protocol': 'openai_chat', 'model_base_url': 'https://proxy.example/v1', 'model_api_key': 'saved'})
    assert connection.request.call_args.kwargs['headers']['Authorization'] == 'Bearer saved'


def test_new_endpoint_never_receives_old_key():
    with pytest.raises(ValueError, match='不会使用其他接口'):
        discover(data={'model_base_url': 'https://proxy.example/v1'}, credentials={
            'model_protocol': 'deepseek', 'model_api_key': 'old'})


@pytest.mark.parametrize('status', [301, 401, 403, 404, 500])
def test_failure_never_exposes_provider_response(status):
    with pytest.raises(ValueError) as error:
        discover({'error': 'secret'}, status=status)
    assert 'secret' not in str(error.value)


@pytest.mark.parametrize('payload', [{'data': []}, {'data': 'secret'}, ['secret']])
def test_invalid_model_lists_are_rejected(payload):
    with pytest.raises(ValueError):
        discover(payload)


def test_private_endpoint_rejected():
    with pytest.raises(ValueError):
        discover(data={'model_base_url': 'https://127.0.0.1/v1', 'api_key': 'secret'})
