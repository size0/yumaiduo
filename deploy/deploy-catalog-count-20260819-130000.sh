#!/usr/bin/env bash
set -euo pipefail
# Run on the matching service host after copying the corresponding /tmp files.
kind=${1:?backend|plugin}
if [[ $kind == backend ]]; then
 s=wanda-v3-backend; old=/opt/wanda-v3-backend/releases/v3-vision-json-zone-normalizer-20260819-114500; new=/opt/wanda-v3-backend/releases/v3-catalog-canonicalization-20260819-130000; c=/etc/systemd/system/wanda-v3-backend.service.d/10-v3-runtime.conf; root=/opt/wanda-v3-backend/releases
 files=(main.py local_catalog.py)
else
 s=wanda-seat-autoquote.service; old=/opt/wanda-preview-plugin/releases/v3-order-quote-state-20260819-124500; new=/opt/wanda-preview-plugin/releases/v3-explicit-position-count-20260819-130000; c=/etc/systemd/system/wanda-seat-autoquote.service.d/10-v3-runtime.conf; root=/opt/wanda-preview-plugin/releases
 files=(quote-preview-client.mjs)
fi
[[ "$(systemctl show "$s" -p WorkingDirectory --value)" == "$old" ]] || exit 1; test ! -e "$new"; cp "$c" /tmp/catalog-count.conf
rollback(){ cp /tmp/catalog-count.conf "$c"; systemctl daemon-reload; systemctl restart "$s"; rm -rf "$new"; }
mkdir -p "$new"; cp -al "$old/." "$new/"
if [[ $kind == backend ]]; then install -m644 /tmp/main.py "$new/app/main.py"; install -m644 /tmp/local_catalog.py "$new/app/local_catalog.py"; install -m644 /tmp/wanda_quote.py "$new/app/wanda_quote.py"; /opt/wanda-v3-backend/venv/bin/python -m py_compile "$new/app/main.py" "$new/app/local_catalog.py" "$new/app/wanda_quote.py"; else install -m644 /tmp/quote-preview-client.mjs "$new/src/quote-preview-client.mjs"; node --check "$new/src/quote-preview-client.mjs"; fi
sed -i "s#$old#$new#g" "$c"; systemctl daemon-reload; systemctl restart "$s"; sleep 2
if ! systemctl is-active --quiet "$s"; then rollback; exit 1; fi
systemctl show "$s" -p WorkingDirectory --value
