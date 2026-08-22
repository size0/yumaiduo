#!/usr/bin/env bash
set -euo pipefail

service=wanda-seat-autoquote
expected_release=/opt/wanda-preview-plugin/releases/v3-quote-safety-audit-20260819-014500
release=/opt/wanda-preview-plugin/releases/v3-wplus-area-probe-20260819-085000
canonical_dropin=/etc/systemd/system/wanda-seat-autoquote.service.d/10-v3-runtime.conf
source_dropin=/tmp/wanda-plugin-wplus-area-probe-20260819-085000.service.conf
backup=/tmp/wanda-plugin-wplus-area-probe-20260819-085000.previous.conf
old_release="$(systemctl show "$service" -p WorkingDirectory --value)"

[[ "$old_release" == "$expected_release" ]] || { echo "unexpected release: $old_release" >&2; exit 1; }
test -f /tmp/quote-preview-client.mjs
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
install -m 0644 /tmp/quote-preview-client.mjs "$release/src/quote-preview-client.mjs"
install -m 0644 "$source_dropin" "$canonical_dropin"
systemctl daemon-reload
systemctl restart "$service"

healthy=false
for _ in $(seq 1 15); do
  if systemctl is-active --quiet "$service"; then healthy=true; break; fi
  sleep 1
done
if [[ "$healthy" != true ]]; then rollback; echo "plugin health check failed; rolled back" >&2; exit 1; fi
if ! node --check "$release/src/quote-preview-client.mjs" || ! grep -Fq 'wplus_area_unavailable' "$release/src/quote-preview-client.mjs"; then
  rollback
  echo "plugin W+ area review reply verification failed; rolled back" >&2
  exit 1
fi
systemctl show "$service" -p WorkingDirectory --value
