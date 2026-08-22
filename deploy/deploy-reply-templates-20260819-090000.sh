#!/usr/bin/env bash
set -euo pipefail
backend=wanda-v3-backend; plugin=wanda-seat-autoquote
b_old=/opt/wanda-v3-backend/releases/v3-wplus-area-probe-20260819-085000
p_old=/opt/wanda-preview-plugin/releases/v3-wplus-area-probe-20260819-085000
b_new=/opt/wanda-v3-backend/releases/v3-reply-templates-20260819-090000
p_new=/opt/wanda-preview-plugin/releases/v3-reply-templates-20260819-090000
b_conf=/etc/systemd/system/wanda-v3-backend.service.d/10-v3-runtime.conf
p_conf=/etc/systemd/system/wanda-seat-autoquote.service.d/10-v3-runtime.conf
[[ "$(systemctl show $backend -p WorkingDirectory --value)" == "$b_old" ]] || exit 1
[[ "$(systemctl show $plugin -p WorkingDirectory --value)" == "$p_old" ]] || exit 1
test ! -e "$b_new"; test ! -e "$p_new"
cp "$b_conf" /tmp/reply-templates-backend.conf; cp "$p_conf" /tmp/reply-templates-plugin.conf
rollback() { cp /tmp/reply-templates-backend.conf "$b_conf"; cp /tmp/reply-templates-plugin.conf "$p_conf"; systemctl daemon-reload; systemctl restart "$backend" "$plugin"; rm -rf "$b_new" "$p_new"; }
mkdir -p "$b_new" "$p_new"; cp -al "$b_old/." "$b_new/"; cp -al "$p_old/." "$p_new/"
install -m 0644 /tmp/main.py "$b_new/app/main.py"; install -m 0644 /tmp/schemas.py "$b_new/app/schemas.py"; install -m 0644 /tmp/plugin_bridge_store.py "$b_new/app/plugin_bridge_store.py"
install -m 0644 /tmp/application.mjs "$p_new/src/application.mjs"; install -m 0644 /tmp/app.js "$p_new/ui/app.js"; install -m 0644 /tmp/index.html "$p_new/ui/index.html"
install -m 0644 /tmp/wanda-v3-reply-templates-20260819-090000.service.conf "$b_conf"; install -m 0644 /tmp/wanda-plugin-reply-templates-20260819-090000.service.conf "$p_conf"
systemctl daemon-reload; systemctl restart "$backend" "$plugin"
for i in $(seq 1 15); do systemctl is-active --quiet "$backend" && systemctl is-active --quiet "$plugin" && curl -fsS http://127.0.0.1:8011/health >/dev/null && break; sleep 1; done
if ! systemctl is-active --quiet "$backend" || ! systemctl is-active --quiet "$plugin" || ! curl -fsS http://127.0.0.1:8011/health >/dev/null; then rollback; exit 1; fi
if ! PYTHONPATH="$b_new" /opt/wanda-v3-backend/venv/bin/python -c 'from app.main import _validate_quote_reply_template as v; v("报价 {单价} 元"); print("reply-template-guard:OK")' || ! node --check "$p_new/ui/app.js"; then rollback; exit 1; fi
systemctl show "$backend" -p WorkingDirectory --value; systemctl show "$plugin" -p WorkingDirectory --value
