#!/usr/bin/env bash
set -euo pipefail
s=wanda-v3-backend
old=/opt/wanda-v3-backend/releases/v3-reply-template-completion-20260819-093000
new=/opt/wanda-v3-backend/releases/v3-cross-platform-wanda-cinema-20260819-101500
conf=/etc/systemd/system/wanda-v3-backend.service.d/10-v3-runtime.conf
[[ "$(systemctl show $s -p WorkingDirectory --value)" == "$old" ]] || exit 1
test ! -e "$new"; cp "$conf" /tmp/cross-platform-wanda.previous.conf
rollback(){ cp /tmp/cross-platform-wanda.previous.conf "$conf"; systemctl daemon-reload; systemctl restart "$s"; rm -rf "$new"; }
mkdir -p "$new"; cp -al "$old/." "$new/"; install -m644 /tmp/wanda_quote.py "$new/app/wanda_quote.py"; install -m644 /tmp/main.py "$new/app/main.py"
sed -i "s#$old#$new#g" "$conf"; systemctl daemon-reload; systemctl restart "$s"
for i in $(seq 1 15); do systemctl is-active --quiet "$s" && curl -fsS http://127.0.0.1:8011/health >/dev/null && break; sleep 1; done
if ! systemctl is-active --quiet "$s" || ! curl -fsS http://127.0.0.1:8011/health >/dev/null; then rollback; exit 1; fi
PYTHONPATH="$new" /opt/wanda-v3-backend/venv/bin/python -c 'from app.schemas import Recognition; from app.wanda_quote import RealtimeQuoteService; r=Recognition.model_validate({"platform":"MAOYAN","cinema":"测试万达影城"}); assert r.cinema and "万达" in r.cinema; print("cross-platform-wanda:OK")'
systemctl show "$s" -p WorkingDirectory --value
