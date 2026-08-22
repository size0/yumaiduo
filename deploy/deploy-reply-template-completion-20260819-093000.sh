#!/usr/bin/env bash
set -euo pipefail
b=wanda-v3-backend; p=wanda-seat-autoquote
bo=/opt/wanda-v3-backend/releases/v3-reply-templates-20260819-090000; po=/opt/wanda-preview-plugin/releases/v3-reply-templates-20260819-090000
bn=/opt/wanda-v3-backend/releases/v3-reply-template-completion-20260819-093000; pn=/opt/wanda-preview-plugin/releases/v3-reply-template-completion-20260819-093000
bc=/etc/systemd/system/wanda-v3-backend.service.d/10-v3-runtime.conf; pc=/etc/systemd/system/wanda-seat-autoquote.service.d/10-v3-runtime.conf
[[ "$(systemctl show $b -p WorkingDirectory --value)" == "$bo" && "$(systemctl show $p -p WorkingDirectory --value)" == "$po" ]] || exit 1
test ! -e "$bn"; test ! -e "$pn"; cp "$bc" /tmp/rt-b.conf; cp "$pc" /tmp/rt-p.conf
rollback(){ cp /tmp/rt-b.conf "$bc"; cp /tmp/rt-p.conf "$pc"; systemctl daemon-reload; systemctl restart "$b" "$p"; rm -rf "$bn" "$pn"; }
mkdir -p "$bn" "$pn"; cp -al "$bo/." "$bn/"; cp -al "$po/." "$pn/"
install -m644 /tmp/main.py "$bn/app/main.py"; install -m644 /tmp/plugin_bridge_store.py "$bn/app/plugin_bridge_store.py"
for f in application.mjs quote-preview-client.mjs workflow.mjs; do install -m644 "/tmp/$f" "$pn/src/$f"; done
install -m644 /tmp/app.js "$pn/ui/app.js"; install -m644 /tmp/index.html "$pn/ui/index.html"
sed -i "s#$bo#$bn#g" "$bc"; sed -i "s#$po#$pn#g" "$pc"; systemctl daemon-reload; systemctl restart "$b" "$p"
for i in $(seq 1 15); do systemctl is-active --quiet "$b" && systemctl is-active --quiet "$p" && curl -fsS http://127.0.0.1:8011/health >/dev/null && break; sleep 1; done
if ! systemctl is-active --quiet "$b" || ! systemctl is-active --quiet "$p" || ! curl -fsS http://127.0.0.1:8011/health >/dev/null || ! node --check "$pn/src/workflow.mjs"; then rollback; exit 1; fi
systemctl show "$b" -p WorkingDirectory --value; systemctl show "$p" -p WorkingDirectory --value
