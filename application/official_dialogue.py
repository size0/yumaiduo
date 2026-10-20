"""Read scoped platform dialogue as context, never as transaction authority."""
import json
import urllib.request
from datetime import datetime, timedelta, timezone


class _ReadTransport:
    def send(self, url, body, headers, timeout):
        request = urllib.request.Request(url, data=body.encode('utf-8'), headers=headers, method='POST')
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(2 * 1024 * 1024).decode('utf-8')


def _stamp(row):
    try:
        value = datetime.fromisoformat(str(row.get('sentAt', '')).replace('Z', '+00:00'))
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except (TypeError, ValueError):
        return None


def read_dialogue(session, config, *, client=None, now=None):
    """Use only this event and earlier messages; a missing anchor is not safe history."""
    empty = {'status': 'UNAVAILABLE', 'source': 'official_im', 'messages': []}
    ident = {k: str(getattr(session.conversation, k, '') or '')
             for k in ('tenant_id', 'shop_id', 'buyer_id', 'chat_id')}
    anchor_id = str(getattr(session, 'current_message_id', '') or '')
    if not all(ident.values()) or not anchor_id:
        return {**empty, 'status': 'ANCHOR_UNAVAILABLE'}
    try:
        if client is None:
            from integrations.yumaiduo.shop_client import PluginShopsClient, resolve_plugin_secret
            secret = resolve_plugin_secret()
            if not secret:
                return empty
            client = PluginShopsClient(secret=secret, timeout=2.0, transport=_ReadTransport())
        status, raw = client._post('/internal/messages/read', ident)
        rows = json.loads(raw) if status == 200 else None
        if not isinstance(rows, list):
            return empty
        scoped = []
        for row in rows[:100]:
            if not isinstance(row, dict) or any(str(row.get(field, '')) != ident[key]
                    for field, key in (('accountUnb', 'shop_id'), ('peerUnb', 'buyer_id'), ('chatId', 'chat_id'))):
                continue
            if ((row.get('direction') == 'inbound' and str(row.get('senderUnb')) == ident['buyer_id'])
                    or (row.get('direction') == 'outbound' and str(row.get('senderUnb')) == ident['shop_id'])):
                scoped.append(row)
        anchors = [row for row in scoped if row.get('messageId') == anchor_id
                   and row.get('direction') == 'inbound' and not row.get('recalledAt')]
        if len(anchors) != 1 or _stamp(anchors[0]) is None:
            return {**empty, 'status': 'ANCHOR_UNAVAILABLE'}
        boundary = _stamp(anchors[0])
        current = now or datetime.now(timezone.utc)
        cutoff = current - timedelta(hours=max(1, min(24, int(config.memory_hours))))
        projected, seen, events, images = [], set(), [], []
        recalled_ids = {row.get('messageId') for row in scoped if row.get('recalledAt')
                        and isinstance(row.get('messageId'), str)}
        for row in scoped:
            stamp, mid = _stamp(row), row.get('messageId')
            if (not isinstance(mid, str) or not mid or mid in seen or mid == anchor_id
                    or mid in recalled_ids or stamp is None or not cutoff <= stamp <= current or stamp >= boundary):
                continue
            seen.add(mid)
            speaker = 'buyer' if row['direction'] == 'inbound' else 'seller'
            if str(row.get('messageType')) in ('14', '26'):
                events.append({'message_id': mid, 'speaker': speaker, 'at': stamp.isoformat(),
                    'type': 'platform_notice', 'is_buyer_request': False,
                    'text': str(row.get('content') or '')[:1500]})
                continue
            if str(row.get('messageType')) not in ('1', '2'):
                continue
            if str(row.get('messageType')) == '2':
                from urllib.parse import urlparse
                url = str(row.get('content') or '')
                parsed = urlparse(url)
                if (parsed.scheme == 'https' and (parsed.hostname or '').endswith('.alicdn.com')
                        and not parsed.username and not parsed.password and len(url) <= 2048):
                    images.append(dict(ident, message_id=mid, speaker=speaker, at=stamp.isoformat(), url=url))
            text = row.get('content') if str(row.get('messageType')) == '1' else '[图片消息；内容需通过图像识别核实]'
            if not isinstance(text, str) or not text.strip():
                continue
            projected.append({'message_id': mid, 'speaker': 'buyer' if row['direction'] == 'inbound' else 'seller',
                              'text': text[:1500], 'at': stamp.isoformat()})
        projected.sort(key=lambda row: row['at'])
        limit = max(2, min(100, int(config.memory_messages)))
        selected, size = [], 0
        for row in reversed(projected[-limit:]):
            if size + len(row['text']) > 12000:
                break
            selected.append(row)
            size += len(row['text'])
        from .seller_context import annotate
        return {**empty, 'status': 'AVAILABLE', 'anchor_message_id': anchor_id, 'identity':ident,
                'messages': annotate(list(reversed(selected)),ident), 'max_page_size': 100,
                'events': sorted(events, key=lambda r: r['at'])[-limit:],
                'images': sorted(images, key=lambda r: r['at'])[-limit:]}
    except Exception:
        # History failures must not stop the reply or expose SDK errors/secrets.
        return empty
