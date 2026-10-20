"""Persist order-specific QR delivery; external receipts own delivery status."""
import base64
import hashlib
from datetime import datetime, timezone

from . import ledger
from .purchase_actions import platform_order


def image_bytes(data):
    from .ticket_image import detect_kind
    if not isinstance(data, str) or len(data) > 7 * 1024 * 1024:
        raise ValueError('INVALID_QR_IMAGE')
    if data.startswith('data:'):
        data = data.split(',', 1)[-1]
    try:
        raw = base64.b64decode(data, validate=True)
    except ValueError:
        raise ValueError('INVALID_QR_IMAGE') from None
    if not raw or len(raw) > 5 * 1024 * 1024 or not detect_kind(raw):
        raise ValueError('INVALID_QR_IMAGE')
    return raw


def receive(order_id, data, *, tenant, shops, operator='', upstream_id='', binding=None):
    from integrations.yumaiduo.shop_client import upload_ticket_image, resolve_plugin_secret
    from .wanda_delivery import validate_image
    raw = image_bytes(data)
    digest = hashlib.sha256(raw).hexdigest()
    store = ledger.stores().orders
    order = store.get(order_id) if store else None
    if (order is None or order.provider != 'WANDA' or order.tenant_id != tenant
            or order.shop_id not in shops):
        raise ValueError('ORDER_OUTSIDE_SCOPE')
    if binding is not None and order.payload.get('ticket_system_binding') != binding:
        raise ValueError('BINDING_MISMATCH')
    receipt = order.payload.get('wanda_qr_receipt')
    if receipt:
        if receipt['sha256'] != digest:
            raise ValueError('TICKET_CONFLICT')
        return {'ok': True, 'duplicate': True, 'status': 'ACCEPTED'}
    if (order.is_terminal or order.shipped or order.ticket_codes or order.failure_code
            or order.payment_state != 'CONFIRMED' or not order.paid_fen >= order.amount_fen > 0
            or order.payload.get('quote_revision_pending')):
        raise ValueError('ORDER_NOT_DELIVERABLE')
    detail = platform_order(order, {k: getattr(order, k) for k in ('tenant_id','shop_id','buyer_id','chat_id')})
    if type(detail.get('orderStatus')) is not int or detail['orderStatus'] not in (2,3,4) or str(detail.get('payment')) != str(order.amount_fen):
        raise ValueError('PLATFORM_PAYMENT_NOT_CONFIRMED')
    result = upload_ticket_image(base64.b64encode(raw).decode(), secret=resolve_plugin_secret(),
                                 tenant_id=tenant, account_unb=order.shop_id)
    if not result.get('ok'):
        raise ValueError('QR_UPLOAD_FAILED')
    image = result['image']
    validate_image(image)
    with store.lock:
        current = store.get(order_id)
        if current.to_dict() != order.to_dict():
            prior = current.payload.get('wanda_qr_receipt') or {}
            if prior.get('sha256') == digest:
                return {'ok': True, 'duplicate': True, 'status': 'ACCEPTED'}
            raise ValueError('ORDER_CHANGED_RETRY_READ')
        order.payload['wanda_ticket_image'] = image
        order.payload['wanda_qr_receipt'] = {'sha256': digest, 'operator': operator,
            'upstream_order_id': upstream_id, 'received_at': datetime.now(timezone.utc).isoformat()}
        order.state = 'TICKETED'
        order.touch()
        store.put(order)
    return {'ok': True, 'duplicate': False, 'status': 'ACCEPTED'}
