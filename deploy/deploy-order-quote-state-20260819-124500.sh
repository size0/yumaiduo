#!/usr/bin/env bash
set -euo pipefail
s=wanda-seat-autoquote.service; old=/opt/wanda-preview-plugin/releases/v3-short-area-quote-reply-20260819-113000; new=/opt/wanda-preview-plugin/releases/v3-order-quote-state-20260819-124500; c=/etc/systemd/system/wanda-seat-autoquote.service.d/10-v3-runtime.conf
[[ "$(systemctl show "$s" -p WorkingDirectory --value)" == "$old" ]] || exit 1; test ! -e "$new"; cp "$c" /tmp/order-quote-state.conf
rollback(){ cp /tmp/order-quote-state.conf "$c"; systemctl daemon-reload; systemctl restart "$s"; rm -rf "$new"; }
mkdir -p "$new"; cp -al "$old/." "$new/"; install -m644 /tmp/workflow.mjs "$new/src/workflow.mjs"; install -m644 /tmp/quote-preview-client.mjs "$new/src/quote-preview-client.mjs"; sed -i "s#$old#$new#g" "$c"; systemctl daemon-reload; systemctl restart "$s"
for i in $(seq 1 15); do systemctl is-active --quiet "$s" && curl -fsS http://127.0.0.1:4003/health >/dev/null && break; sleep 1; done
if ! systemctl is-active --quiet "$s" || ! curl -fsS http://127.0.0.1:4003/health >/dev/null; then rollback; exit 1; fi
node --check "$new/src/workflow.mjs"; node --check "$new/src/quote-preview-client.mjs"; systemctl show "$s" -p WorkingDirectory --value
