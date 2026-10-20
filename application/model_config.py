"""One model configuration for the admin connection test and buyer assistant."""

from application import store
from application.conversation_ai import build_ai
from integrations.model.client import ModelClient, DEFAULT_BASE_URL
import settings
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelConnection:
    api_key: str = field(repr=False)
    model: str
    base_url: str = DEFAULT_BASE_URL
    protocol: str = 'deepseek'

    def options(self):
        return dict(api_key=self.api_key, model=self.model,
                    base_url=self.base_url, protocol=self.protocol)


def connection_snapshot(default_model, *, api_key=None):
    credentials = store.load_credentials() or {}
    custom = credentials.get('model_protocol') == 'openai_chat'
    return ModelConnection(
        api_key=api_key if api_key is not None else credentials.get('model_api_key', ''),
        model=credentials.get('custom_model_name', '') if custom else default_model,
        base_url=credentials.get('model_base_url', '') if custom else DEFAULT_BASE_URL,
        protocol='openai_chat' if custom else 'deepseek')


def status():
    credentials = store.load_credentials() or {}
    key = str(credentials.get("model_api_key") or "")
    return {"api_key_configured": bool(key), "api_key_tail": key[-4:] if key else "",
            'model_protocol': credentials.get('model_protocol', 'deepseek'),
            'model_base_url': credentials.get('model_base_url', DEFAULT_BASE_URL),
            'custom_model_name': credentials.get('custom_model_name', '')}


def save_key(value):
    return save_connection({'api_key': value})


def validate_key(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("请填写有效的模型 API Key")
    if any(x in value for x in ("•", "…", "*", "\n", "\r")):
        raise ValueError("请填写真实密钥，不能保存遮罩文字")
    return value.strip()


def save_connection(data):
    with store.CREDENTIAL_LOCK:
        return _save_connection(data)


def _save_connection(data):
    if not isinstance(data, dict):
        raise ValueError('模型配置必须为对象')
    credentials = dict(store.load_credentials() or {})
    protocol = data.get('model_protocol', credentials.get('model_protocol', 'deepseek'))
    if protocol not in ('deepseek', 'openai_chat'):
        raise ValueError('请选择支持的模型协议')
    endpoint = DEFAULT_BASE_URL
    model = ''
    if protocol == 'openai_chat':
        from integrations.model.connection import base_url
        endpoint = base_url(data.get('model_base_url', credentials.get('model_base_url', '')))
        model = data.get('custom_model_name', credentials.get('custom_model_name', ''))
        if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(c.isspace() for c in model):
            raise ValueError('请填写中转站提供的模型ID')
    changed = (protocol != credentials.get('model_protocol', 'deepseek') or
               endpoint != credentials.get('model_base_url', DEFAULT_BASE_URL))
    key = data.get('api_key')
    if changed and not key:
        raise ValueError('更换接口时请同时填写该接口的密钥，避免将旧密钥发送到新地址')
    if key is not None:
        credentials['model_api_key'] = validate_key(key)
    if not credentials.get('model_api_key'):
        raise ValueError('请填写模型接口密钥')
    credentials.update(model_protocol=protocol, model_base_url=endpoint, custom_model_name=model)
    store.save_credentials(credentials)
    return status()


def refresh_service(service):
    if service is not None:
        cfg = settings.load()
        connection = connection_snapshot(cfg.assistant.model_name)
        service.ai = build_ai(cfg, connection=connection)


def list_models(data):
    """Discover model IDs without saving credentials or generating any tokens."""
    import json
    from contextlib import closing
    from urllib.parse import urlsplit
    from integrations.model.connection import base_url
    from integrations.model.client import PinnedHTTPSConnection

    if not isinstance(data, dict):
        raise ValueError('模型配置必须为对象')
    endpoint = base_url(data.get('model_base_url', ''))
    credentials = store.load_credentials() or {}
    key = data.get('api_key')
    if key:
        key = validate_key(key)
    elif (credentials.get('model_protocol') == 'openai_chat' and
          endpoint == credentials.get('model_base_url')):
        key = credentials.get('model_api_key', '')
    else:
        raise ValueError('请先填写此中转站的密钥；不会使用其他接口的旧密钥')
    if not key:
        raise ValueError('请填写此中转站的密钥')
    parsed = urlsplit(endpoint)
    try:
        with closing(PinnedHTTPSConnection(parsed.hostname, parsed.port or 443, timeout=10)) as conn:
            conn.request('GET', parsed.path + '/models', headers={'Authorization': 'Bearer ' + key})
            response = conn.getresponse()
            if response.status in (401, 403):
                raise ValueError('获取模型列表未通过鉴权，请检查密钥及权限')
            if response.status != 200:
                raise ValueError('中转站未提供可用的模型列表，请手动填写模型ID')
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('模型列表过大，请手动填写模型ID')
        payload = json.loads(raw)
        rows = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError('模型列表格式不受支持，请手动填写模型ID')
        models = sorted({row['id'] for row in rows if isinstance(row, dict) and
                         isinstance(row.get('id'), str) and 0 < len(row['id']) <= 200 and
                         not any(c.isspace() or ord(c) < 32 for c in row['id']) and key not in row['id']})
        if not models:
            raise ValueError('接口未返回可用模型，请手动填写模型ID')
        return {'ok': True, 'models': models}
    except ValueError as exc:
        # Only our fixed messages are exposed, never provider data or credentials.
        if str(exc) in ('获取模型列表未通过鉴权，请检查密钥及权限',
                        '中转站未提供可用的模型列表，请手动填写模型ID',
                        '模型列表过大，请手动填写模型ID',
                        '模型列表格式不受支持，请手动填写模型ID',
                        '接口未返回可用模型，请手动填写模型ID'):
            raise ValueError(str(exc)) from None
        raise ValueError('获取模型列表失败，请核对接口、密钥及网络') from None
    except Exception:
        raise ValueError('获取模型列表失败，请核对接口、密钥及网络') from None


def test_connection():
    cfg = settings.load()
    connection = connection_snapshot(cfg.assistant.model_name)
    if not connection.api_key:
        return {"ok": False, "error": "尚未配置真实模型 API Key；当前不能进行模型测试"}
    try:
        reply = ModelClient(**connection.options(), retries=0).chat(
            [{"role": "user", "content": "请只回复 OK。"}],
            max_tokens=256 if connection.protocol == 'openai_chat' else 16, temperature=0
        )
        if not reply.content.strip():
            return {"ok": False, "error": "模型返回空内容，连通验证未通过"}
        return {"ok": True, "model_name": connection.model}
    except Exception:
        # Provider errors can echo request details. Never return them to the UI.
        return {"ok": False, "error": "模型调用失败，请核对密钥、模型权限及网络连接"}
