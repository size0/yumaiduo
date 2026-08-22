#!/usr/bin/env bash
set -euo pipefail
s=wanda-v3-backend; old=/opt/wanda-v3-backend/releases/v3-vision-schema-normalizer-20260819-110000; new=/opt/wanda-v3-backend/releases/v3-vision-showtime-normalizer-20260819-111500; c=/etc/systemd/system/wanda-v3-backend.service.d/10-v3-runtime.conf
[[ "$(systemctl show $s -p WorkingDirectory --value)" == "$old" ]] || exit 1; test ! -e "$new"; cp "$c" /tmp/vision-showtime.conf
rollback(){ cp /tmp/vision-showtime.conf "$c"; systemctl daemon-reload; systemctl restart "$s"; rm -rf "$new"; }
mkdir -p "$new"; cp -al "$old/." "$new/"; install -m644 /tmp/vision.py "$new/app/vision.py"; sed -i "s#$old#$new#g" "$c"; systemctl daemon-reload; systemctl restart "$s"
for i in $(seq 1 15); do systemctl is-active --quiet "$s" && curl -fsS http://127.0.0.1:8011/health >/dev/null && break; sleep 1; done
if ! systemctl is-active --quiet "$s" || ! curl -fsS http://127.0.0.1:8011/health >/dev/null; then rollback; exit 1; fi
PYTHONPATH="$new" /opt/wanda-v3-backend/venv/bin/python -c 'from app.vision import _normalize_recognition_payload as n; assert n("{\"image_type\":\"SEAT_MAP\",\"showtime\":\"16:15 - 19:08\"}").showtime=="16:15-19:08"; print("vision-showtime-normalizer:OK")'
systemctl show "$s" -p WorkingDirectory --value
