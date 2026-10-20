"""Shop-specific configuration, isolated per request/event using ContextVar."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
from pathlib import Path
from threading import RLock
from urllib.parse import urlparse, parse_qs
import hashlib
import json
import os
import tempfile

ROOT = Path(__file__).resolve().parent.parent / 'data' / 'shop_config'
CURRENT = ContextVar('shop_config_scope', default=('', ''))
SECTION = ContextVar('shop_config_section', default='')
LOCK = RLock()


@contextmanager
def scope(tenant='', shop='', section=''):
    if bool(tenant) != bool(shop):
        raise ValueError('请选择完整的租户和店铺')
    token = CURRENT.set((str(tenant), str(shop)))
    part = SECTION.set(section)
    try:
        yield
    finally:
        SECTION.reset(part)
        CURRENT.reset(token)


def path():
    tenant, shop = CURRENT.get()
    if not shop:
        return None
    key = hashlib.sha256(json.dumps([tenant, shop]).encode()).hexdigest()
    return ROOT / (key + '.json')


def read():
    p = path()
    return json.loads(p.read_text(encoding='utf-8')) if p and p.exists() else {}


def update(fields):
    with LOCK:
        p = path()
        if p is None:
            raise ValueError('请先选择目标店铺')
        data = read()
        data.update(deepcopy(fields))
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(data, out, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def apply_settings(data):
    saved = read()
    data.apply_dict(saved.get('features') or {})
    if 'assistant' in saved:
        data.assistant.apply_dict(saved['assistant'])
    return data


def save_settings(data):
    if path() is None:
        return False
    values = data.to_dict()
    if SECTION.get() == 'assistant':
        update({'assistant': values['assistant']})
    else:
        update({'features': {k:v for k,v in values.items() if k != 'assistant'}})
    return True


def template(row):
    item = read().get('templates', {}).get(row['key'])
    return {**row, **deepcopy(item)} if item is not None else row


def save_template(key, text, channel, inherit=False, variant=None):
    import templates
    with LOCK:
        row = deepcopy(templates.find(key))
        if variant:
            row.setdefault('variants', {})[variant] = text
        elif channel == 'global':
            row['text'] = text
        elif inherit:
            defaults = templates.DEFAULT_CHANNEL_TEXT.get(key, {})
            row.setdefault('channel_text', {}).pop(channel, None)
            if channel in defaults:
                row['channel_text'][channel] = defaults[channel]
        else:
            row.setdefault('channel_text', {})[channel] = text
        values = read().get('templates', {})
        values[key] = {k:row.get(k, {}) for k in ('text','channel_text','variants')}
        update({'templates': values})


def http_scope(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        endpoint = parsed.path.rstrip('/').split('/')[-1]
        allowed = endpoint in ('assistant','settings','templates','config-push','model-test') or '/api/preview/' in parsed.path
        tenant = query.get('tenant_id', [''])[0] if allowed else ''
        shop = query.get('shop_id', [''])[0] if allowed else ''
        if bool(tenant) != bool(shop):
            return self._json({'error':'请选择完整的店铺'},400)
        with scope(tenant, shop, endpoint):
            return fn(self, *args, **kwargs)
    return wrapped


def by_identity(fn):
    @wraps(fn)
    def wrapped(first, *args, **kwargs):
        ident = first if isinstance(first, dict) else vars(first)
        with scope(ident.get('tenant_id',''), ident.get('shop_id','')):
            return fn(first, *args, **kwargs)
    return wrapped


def push(kind, tenant, shop):
    """Copy a saved effective snapshot; shared credentials never enter snapshots."""
    import settings
    import templates
    from . import ticket_image
    if kind == 'assistant':
        fields = {'assistant': settings.load().assistant.to_dict()}
    elif kind == 'features':
        fields = {'features': {k:v for k,v in settings.load().to_dict().items() if k != 'assistant'},
                  'ticket_image_asset': ticket_image.load()}
    elif kind == 'templates':
        fields = {'templates': {row['key']: {k:templates.find(row['key']).get(k,{}) for k in ('text','channel_text')}
                                for row in templates.TEMPLATES}}
    else:
        raise ValueError('未知推送类型')
    with scope(tenant, shop):
        update(fields)
        if any(read().get(k) != v for k,v in fields.items()):
            raise ValueError('推送后的配置核对失败')
    return {'ok':True, 'shop_id':shop, 'kind':kind}


def by_conversation(fn):
    @wraps(fn)
    def wrapped(self, conv, *args, **kwargs):
        with scope(getattr(conv, 'tenant_id', ''), getattr(conv, 'shop_id', '')):
            return fn(self, conv, *args, **kwargs)
    return wrapped
