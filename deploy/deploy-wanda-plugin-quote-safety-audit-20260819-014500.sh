#!/usr/bin/env bash
set -euo pipefail

service=wanda-seat-autoquote
expected_release=/opt/wanda-preview-plugin/releases/v3-quote-scope-clarity-20260819-007500
release=/opt/wanda-preview-plugin/releases/v3-quote-safety-audit-20260819-014500
canonical_dropin=/etc/systemd/system/wanda-seat-autoquote.service.d/10-v3-runtime.conf
source_dropin=/tmp/wanda-plugin-quote-safety-audit-20260819-014500.service.conf
backup=/tmp/wanda-plugin-quote-safety-audit-20260819-014500.previous.conf
old_release="$(systemctl show "$service" -p WorkingDirectory --value)"

[[ "$old_release" == "$expected_release" ]] || { echo "unexpected release: $old_release" >&2; exit 1; }
test -f "$source_dropin"
test ! -e "$release"
for source in /tmp/quote-preview-client.mjs /tmp/app.js /tmp/index.html; do test -f "$source"; done
cp -a "$canonical_dropin" "$backup"

rollback() {
  install -m 0644 "$backup" "$canonical_dropin"
  systemctl daemon-reload
  systemctl restart "$service"
  rm -rf "$release"
}

mkdir -p "$release"
cp -al "$old_release/." "$release/"
install -m 0644 /tmp/quote-preview-client.mjs "$release/src/quote-preview-client.mjs"
install -m 0644 /tmp/app.js "$release/ui/app.js"
install -m 0644 /tmp/index.html "$release/ui/index.html"
install -m 0644 "$source_dropin" "$canonical_dropin"
systemctl daemon-reload
systemctl restart "$service"

healthy=false
for _ in $(seq 1 15); do
  if systemctl is-active --quiet "$service"; then healthy=true; break; fi
  sleep 1
done
if [[ "$healthy" != true ]]; then rollback; echo "plugin health check failed; rolled back" >&2; exit 1; fi

if ! node --check "$release/src/quote-preview-client.mjs" || ! node --check "$release/ui/app.js"; then
  rollback
  echo "plugin syntax verification failed; rolled back" >&2
  exit 1
fi
if grep -Eq '临时锁座|释放锁座' "$release/ui/index.html"; then
  rollback
  echo "plugin retained a seat-lock workflow claim; rolled back" >&2
  exit 1
fi
systemctl show "$service" -p WorkingDirectory --value
