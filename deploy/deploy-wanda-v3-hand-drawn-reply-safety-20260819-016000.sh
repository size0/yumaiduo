#!/usr/bin/env bash
set -euo pipefail

service=wanda-v3-backend
expected_release=/opt/wanda-v3-backend/releases/v3-hand-drawn-count-safety-20260819-015500
release=/opt/wanda-v3-backend/releases/v3-hand-drawn-reply-safety-20260819-016000
canonical_dropin=/etc/systemd/system/wanda-v3-backend.service.d/10-v3-runtime.conf
source_dropin=/tmp/wanda-v3-hand-drawn-reply-safety-20260819-016000.service.conf
backup=/tmp/wanda-v3-hand-drawn-reply-safety-20260819-016000.previous.conf
old_release="$(systemctl show "$service" -p WorkingDirectory --value)"

[[ "$old_release" == "$expected_release" ]] || { echo "unexpected release: $old_release" >&2; exit 1; }
test -f /tmp/main.py
test -f "$source_dropin"
test ! -e "$release"
cp -a "$canonical_dropin" "$backup"

rollback() {
  install -m 0644 "$backup" "$canonical_dropin"
  systemctl daemon-reload
  systemctl restart "$service"
  rm -rf "$release"
}

mkdir -p "$release"
cp -al "$old_release/." "$release/"
install -m 0644 /tmp/main.py "$release/app/main.py"
install -m 0644 "$source_dropin" "$canonical_dropin"
systemctl daemon-reload
systemctl restart "$service"

healthy=false
for _ in $(seq 1 15); do
  if systemctl is-active --quiet "$service" && curl -fsS http://127.0.0.1:8011/health >/dev/null; then healthy=true; break; fi
  sleep 1
done
if [[ "$healthy" != true ]]; then rollback; echo "backend health check failed; rolled back" >&2; exit 1; fi

if ! grep -Fq '已记录您圈出的出票位置偏好' "$release/app/main.py"; then
  rollback
  echo "hand-drawn preference wording verification failed; rolled back" >&2
  exit 1
fi
systemctl show "$service" -p WorkingDirectory --value
curl -fsS http://127.0.0.1:8011/health
