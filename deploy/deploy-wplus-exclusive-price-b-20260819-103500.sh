#!/usr/bin/env bash
set -euo pipefail
s=wanda-v3-backend; old=/opt/wanda-v3-backend/releases/v3-cross-platform-wanda-cinema-20260819-101500; new=/opt/wanda-v3-backend/releases/v3-wplus-exclusive-price-b-20260819-103500; c=/etc/systemd/system/wanda-v3-backend.service.d/10-v3-runtime.conf
[[ "$(systemctl show $s -p WorkingDirectory --value)" == "$old" ]] || exit 1; test ! -e "$new"; cp "$c" /tmp/wplus-b.conf
rollback(){ cp /tmp/wplus-b.conf "$c"; systemctl daemon-reload; systemctl restart "$s"; rm -rf "$new"; }
mkdir -p "$new"; cp -al "$old/." "$new/"; install -m644 /tmp/wanda_quote.py "$new/app/wanda_quote.py"; install -m644 /tmp/schemas.py "$new/app/schemas.py"; sed -i "s#$old#$new#g" "$c"; systemctl daemon-reload; systemctl restart "$s"
for i in $(seq 1 15); do systemctl is-active --quiet "$s" && curl -fsS http://127.0.0.1:8011/health >/dev/null && break; sleep 1; done
if ! systemctl is-active --quiet "$s" || ! curl -fsS http://127.0.0.1:8011/health >/dev/null; then rollback; exit 1; fi
PYTHONPATH="$new" /opt/wanda-v3-backend/venv/bin/python -c 'from app.schemas import SeatZoneType; from app.wanda_quote import _unit_quote_cents; assert _unit_quote_cents(SeatZoneType.WPLUS,5090,None,wplus_adjustment_cents=-290,regular_adjustment_cents=100)==4800; print("wplus-exclusive-price-b:OK")'
systemctl show "$s" -p WorkingDirectory --value
