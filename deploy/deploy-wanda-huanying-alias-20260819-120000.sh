#!/usr/bin/env bash
set -euo pipefail
s=wanda-v3-backend; old=/opt/wanda-v3-backend/releases/v3-vision-json-zone-normalizer-20260819-114500; new=/opt/wanda-v3-backend/releases/v3-wanda-huanying-alias-20260819-120000; c=/etc/systemd/system/wanda-v3-backend.service.d/10-v3-runtime.conf
[[ "$(systemctl show $s -p WorkingDirectory --value)" == "$old" ]] || exit 1; test ! -e "$new"; cp "$c" /tmp/huanying.conf
rollback(){ cp /tmp/huanying.conf "$c"; systemctl daemon-reload; systemctl restart "$s"; rm -rf "$new"; }
mkdir -p "$new"; cp -al "$old/." "$new/"; install -m644 /tmp/wanda_quote.py "$new/app/wanda_quote.py"; install -m644 /tmp/main.py "$new/app/main.py"; sed -i "s#$old#$new#g" "$c"; systemctl daemon-reload; systemctl restart "$s"
for i in $(seq 1 15); do systemctl is-active --quiet "$s" && curl -fsS http://127.0.0.1:8011/health >/dev/null && break; sleep 1; done
if ! systemctl is-active --quiet "$s" || ! curl -fsS http://127.0.0.1:8011/health >/dev/null; then rollback; exit 1; fi
PYTHONPATH="$new" /opt/wanda-v3-backend/venv/bin/python -c 'from app.wanda_quote import is_wanda_cinema_name; assert is_wanda_cinema_name("寰映影城"); print("huanying-alias:OK")'
systemctl show "$s" -p WorkingDirectory --value
