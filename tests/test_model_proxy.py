"""Offline model configuration and wire-contract checks. No model requests."""
import json
from unittest.mock import Mock, patch

import pytest
import settings
from application import model_config
from application.conversation_ai import build_ai
from integrations.model.client import ModelClient, ModelError, UrllibTransport
from integrations.model.client import PinnedHTTPSConnection
from integrations.model.connection import base_url


def transport_reply(content='OK', calls=None):
    return Mock(post=Mock(return_value=(200, json.dumps(dict(choices=[dict(message=dict(
        role='assistant', content=content, tool_calls=calls or []))])))))


def test_openai_proxy_uses_exact_model_endpoint_and_tool_contract():
    call=dict(id='probe',type='function',function=dict(name='read_quote',arguments='{}'))
    transport=transport_reply('',[call])
    client=ModelClient('fake-secret',model='proxy-gpt-example',protocol='openai_chat',
                       base_url='https://proxy.example/v1/',transport=transport)
    reply=client.chat([dict(role='user',content='test')],tools=[dict(type='function')])
    url,body,headers,_=transport.post.call_args.args
    assert url=='https://proxy.example/v1/chat/completions'
    assert body['model']=='proxy-gpt-example' and body['max_completion_tokens']==1024
    assert 'thinking' not in body and 'temperature' not in body and 'max_tokens' not in body
    assert body['tool_choice']=='auto' and reply.raw['tool_calls']==[call]
    assert headers['Authorization']=='Bearer fake-secret'


def test_deepseek_default_payload_stays_compatible():
    transport=transport_reply()
    ModelClient('fake',transport=transport).chat([dict(role='user',content='test')])
    _,body,_,_=transport.post.call_args.args
    assert body['thinking']==dict(type='disabled') and body['max_tokens']==1024
    assert 'max_completion_tokens' not in body


@pytest.mark.parametrize('url',[
    'http://proxy.example/v1','https://localhost/v1','https://127.0.0.1/v1',
    'https://[::1]/v1','https://10.0.0.1/v1','https://proxy.local/v1',
    'https://user:secret@proxy.example/v1','https://proxy.example/v1?key=secret',
    'https://proxy.example/v1#key','https://proxy.example:bad/v1',
])
def test_unsafe_model_endpoint_rejected_without_network(url):
    with pytest.raises(ValueError):base_url(url)


def test_full_completion_url_normalizes_without_duplicate_path():
    assert base_url('https://proxy.example/v1/chat/completions')=='https://proxy.example/v1'


def test_custom_transport_rejects_private_dns_before_http():
    with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]), \
            patch('urllib.request.build_opener') as opener:
        with pytest.raises(ModelError):
            UrllibTransport(custom_endpoint=True).post('https://proxy.example/v1/chat/completions',{}, {},1)
        opener.assert_not_called()


def test_connection_uses_the_validated_ip_without_resolving_twice():
    addresses=[(2,1,6,'',('8.8.8.8',443))]
    context=Mock()
    with patch('socket.getaddrinfo',return_value=addresses) as resolve, \
            patch('socket.socket') as socket_factory:
        conn=PinnedHTTPSConnection('proxy.example',context=context,timeout=10)
        conn.connect()
        resolve.assert_called_once()
        socket_factory.return_value.connect.assert_called_once_with(('8.8.8.8',443))
        context.wrap_socket.assert_called_once_with(socket_factory.return_value,server_hostname='proxy.example')


@pytest.mark.parametrize('operation',['refresh','test','build'])
def test_key_and_endpoint_are_loaded_in_a_single_snapshot(operation):
    first=dict(model_api_key='first-secret',model_protocol='openai_chat',
        model_base_url='https://first.example/v1',custom_model_name='first-gpt')
    second=dict(model_api_key='second-secret',model_protocol='openai_chat',
        model_base_url='https://second.example/v1',custom_model_name='second-gpt')
    with patch.object(model_config.store,'load_credentials',side_effect=[first,second]) as read:
        if operation=='test':
            with patch.object(model_config,'ModelClient') as client:
                client.return_value.chat.return_value.content='OK'
                assert model_config.test_connection()['ok']
                assert client.call_args.kwargs['api_key']=='first-secret'
                assert client.call_args.kwargs['base_url']=='https://first.example/v1'
        else:
            if operation=='build':
                ai=build_ai(settings.Settings(),transport=transport_reply())
            else:
                service=Mock()
                model_config.refresh_service(service)
                ai=service.ai
            assert ai.client.api_key=='first-secret' and ai.client.base_url=='https://first.example/v1'
        read.assert_called_once()


def test_atomic_credentials_failure_preserves_old_file_and_permissions(tmp_path):
    from application import store
    import os,stat
    path=tmp_path/'credentials.json'
    path.write_text('{"model_api_key":"old"}',encoding='utf-8')
    os.chmod(path,0o600)
    before=path.read_bytes();mode=stat.S_IMODE(path.stat().st_mode)
    with patch.object(store,'CRED_FILE',path),patch.object(store.os,'replace',side_effect=OSError('disk failure')):
        with pytest.raises(OSError):store.save_credentials(dict(model_api_key='new'))
    assert path.read_bytes()==before and stat.S_IMODE(path.stat().st_mode)==mode
    assert list(tmp_path.iterdir())==[path]
    with patch.object(store,'CRED_FILE',path):store.save_credentials(dict(model_api_key='new'))
    assert json.loads(path.read_text())['model_api_key']=='new'
    assert stat.S_IMODE(path.stat().st_mode)==mode


def test_endpoint_change_cannot_reuse_old_key():
    saved=dict(model_api_key='old-secret')
    with patch.object(model_config.store,'load_credentials',return_value=saved), \
            patch.object(model_config.store,'save_credentials') as write:
        with pytest.raises(ValueError,match='同时填写'):
            model_config.save_connection(dict(model_protocol='openai_chat',
                model_base_url='https://proxy.example/v1',custom_model_name='gpt-example'))
        write.assert_not_called()
    assert saved==dict(model_api_key='old-secret')


def test_connection_saves_atomically_and_never_returns_secret():
    with patch.object(model_config.store,'load_credentials',return_value=dict(app_secret='supplier-secret')), \
            patch.object(model_config.store,'save_credentials') as write:
        model_config.save_connection(dict(api_key='new-model-secret',model_protocol='openai_chat',
            model_base_url='https://proxy.example/v1',custom_model_name='gpt-example'))
        saved=write.call_args.args[0]
        assert saved['app_secret']=='supplier-secret' and saved['model_api_key']=='new-model-secret'
    with patch.object(model_config.store,'load_credentials',return_value=saved):
        public=model_config.status()
        assert 'new-model-secret' not in str(public)
        assert public['api_key_tail']=='cret' and public['custom_model_name']=='gpt-example'
        ai=build_ai(settings.Settings(),api_key='new-model-secret',transport=transport_reply())
        assert ai.client.model=='gpt-example' and ai.client.protocol=='openai_chat'
        assert ai.client.base_url=='https://proxy.example/v1'


@pytest.mark.parametrize('status,body',[(401,'secret-echo'),(200,'not json secret-echo'),
    (200,json.dumps(dict(error=dict(message='secret-echo'))))])
def test_proxy_error_cannot_echo_credentials(status,body):
    transport=Mock(post=Mock(return_value=(status,body)))
    client=ModelClient('secret-echo',model='gpt-example',protocol='openai_chat',
                       base_url='https://proxy.example/v1',transport=transport)
    with pytest.raises(ModelError) as error:client.chat([dict(role='user',content='test')])
    assert 'secret-echo' not in str(error.value) and 'secret-echo' not in client.last_error


def test_proxy_malformed_status_line_cannot_echo_credentials():
    import http.client
    with patch('integrations.model.client.PinnedHTTPSConnection') as connection:
        connection.return_value.getresponse.side_effect=http.client.BadStatusLine('secret-echo')
        client=ModelClient('secret-echo',model='gpt-example',protocol='openai_chat',
                           base_url='https://proxy.example/v1')
        with pytest.raises(ModelError) as error:
            client.chat([dict(role='user',content='test')])
        assert 'secret-echo' not in str(error.value)
        assert error.value.__suppress_context__


def test_admin_connection_test_uses_same_proxy_and_one_request():
    saved=dict(model_api_key='fake',model_protocol='openai_chat',
        model_base_url='https://proxy.example/v1',custom_model_name='gpt-example')
    with patch.object(model_config.store,'load_credentials',return_value=saved), \
            patch.object(model_config,'ModelClient') as client:
        client.return_value.chat.return_value.content='OK'
        assert model_config.test_connection()==dict(ok=True,model_name='gpt-example')
        assert client.call_args.kwargs['base_url']=='https://proxy.example/v1'
        assert client.call_args.kwargs['protocol']=='openai_chat'
        assert client.call_args.kwargs['retries']==0
        client.return_value.chat.assert_called_once()
