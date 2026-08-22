#!/usr/bin/env bash
set -euo pipefail
s=wanda-v3-backend; old=/opt/wanda-v3-backend/releases/v3-short-need-count-reply-20260819-104500; new=/opt/wanda-v3-backend/releases/v3-vision-schema-normalizer-20260819-110000; c=/etc/systemd/system/wanda-v3-backend.service.d/10-v3-runtime.conf
[[ "$(systemctl show $s -p WorkingDirectory --value)" == "$old" ]] || exit 1; test ! -e "$new"; cp "$c" /tmp/vision-normalizer.conf
rollback(){ cp /tmp/vision-normalizer.conf "$c"; systemctl daemon-reload; systemctl restart "$s"; rm -rf "$new"; }
mkdir -p "$new"; cp -al "$old/." "$new/"; install -m644 /tmp/vision.py "$new/app/vision.py"; sed -i "s#$old#$new#g" "$c"; systemctl daemon-reload; systemctl restart "$s"
for i in $(seq 1 15); do systemctl is-active --quiet "$s" && curl -fsS http://127.0.0.1:8011/health >/dev/null && break; sleep 1; done
if ! systemctl is-active --quiet "$s" || ! curl -fsS http://127.0.0.1:8011/health >/dev/null; then rollback; exit 1; fi
PYTHONPATH="$new" /opt/wanda-v3-backend/venv/bin/python -c 'from app.vision import _normalize_recognition_payload; assert _normalize_recognition_payload("{\"image_type\":\"SEAT_MAP\",\"extra\":1}").image_type.value=="SEAT_MAP"; print("vision-normalizer:OK")'
systemctl show "$s" -p WorkingDirectory --value
