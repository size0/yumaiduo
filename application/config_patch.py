"""Strict HTTP configuration validation; legacy file loading stays compatible."""
import math
import re
from dataclasses import fields
from typing import get_type_hints

ASSISTANT_READONLY = {
    'model_options', 'model_labels', 'business_scope_options', 'stage_labels',
    'style_presets', 'api_key_tail',
}
ASSISTANT_UNSUPPORTED = {'takeover_seconds', 'human_pause_seconds'}


def validate_patch(target, patch, *, section=''):
    from settings import unwired_reason
    if not isinstance(patch, dict):
        return ['配置必须是对象']
    definitions = {f.name: f for f in fields(target)}
    hints = get_type_hints(type(target))
    errors = []
    for key, value in patch.items():
        path = f'{section}.{key}' if section else key
        if key not in definitions:
            errors.append(f'{path} 是未知参数')
            continue
        if section == 'assistant' and key in ASSISTANT_READONLY:
            errors.append(f'{path} 是只读参数')
            continue
        if section == 'assistant' and key in ASSISTANT_UNSUPPORTED:
            errors.append(f'{path} 尚未实现，不能修改')
            continue
        if not section:
            errors.extend(validate_patch(getattr(target, key), value, section=key))
            continue
        kind = hints[key]
        valid = True
        normalized = value
        if kind is bool:
            valid = type(value) is bool or (isinstance(value, str) and value in ('', '0', '1', 'true', 'false'))
            normalized = value in (True, '1', 'true')
        elif kind in (int, float):
            try:
                normalized = float(value)
                valid = not isinstance(value, bool) and isinstance(value, (str, int, float)) and math.isfinite(normalized)
                valid = valid and (kind is float or normalized.is_integer())
            except (ValueError, TypeError, OverflowError):
                valid = False
        elif kind in (str, list, dict):
            valid = isinstance(value, kind)
        if not valid:
            errors.append(f'{path} 类型不正确')
            continue
        reason = unwired_reason(section, key)
        if reason and normalized != getattr(target, key):
            errors.append(f'{path} 尚未实现，不能修改：{reason}')
        meta = definitions[key].metadata
        if kind in (int, float) and (normalized < meta.get('min', -math.inf) or normalized > meta.get('max', math.inf)):
            errors.append(f'{path} 超出允许范围')
        options = meta.get('values', meta.get('options'))
        if options and kind is not bool:
            items = value if kind is list else [normalized]
            if any(item not in options for item in items):
                errors.append(f'{path} 包含无效选项')
        if value in meta.get('disabled_options', []) and value != getattr(target, key):
            errors.append(f'{path} 所选功能尚未实现')
        if kind is str and (meta.get('control') in ('time', 'time_range') or key in ('end', 'time_end')):
            if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?', value):
                errors.append(f'{path} 必须为 HH:MM 或 HH:MM:SS')
    return errors
