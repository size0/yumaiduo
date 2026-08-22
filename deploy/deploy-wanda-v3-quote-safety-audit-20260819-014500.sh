#!/usr/bin/env bash
set -euo pipefail

service=wanda-v3-backend
expected_release=/opt/wanda-v3-backend/releases/v3-quote-rules-regression-20260819-012852
release=/opt/wanda-v3-backend/releases/v3-quote-safety-audit-20260819-014500
canonical_dropin=/etc/systemd/system/wanda-v3-backend.service.d/10-v3-runtime.conf
previous_dropin=/etc/systemd/system/wanda-v3-backend.service.d/zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz-quote-rules-regression-20260819-012852.conf
source_dropin=/tmp/wanda-v3-quote-safety-audit-20260819-014500.service.conf
old_release="$(systemctl show "$service" -p WorkingDirectory --value)"
canonical_backup=/tmp/wanda-v3-quote-safety-audit-20260819-014500.previous.conf
previous_backup=/tmp/wanda-v3-quote-safety-audit-20260819-014500.previous-release.conf

[[ "$old_release" == "$expected_release" ]] || { echo "unexpected release: $old_release" >&2; exit 1; }
test -f "$source_dropin"
test ! -e "$release"
for source in /tmp/wanda_quote.py /tmp/main.py /tmp/plugin_bridge_store.py; do test -f "$source"; done
cp -a "$canonical_dropin" "$canonical_backup"
cp -a "$previous_dropin" "$previous_backup"

rollback() {
  install -m 0644 "$canonical_backup" "$canonical_dropin"
  install -m 0644 "$previous_backup" "$previous_dropin"
  systemctl daemon-reload
  systemctl restart "$service"
  rm -rf "$release"
}

mkdir -p "$release"
cp -al "$old_release/." "$release/"
install -m 0644 /tmp/wanda_quote.py "$release/app/wanda_quote.py"
install -m 0644 /tmp/main.py "$release/app/main.py"
install -m 0644 /tmp/plugin_bridge_store.py "$release/app/plugin_bridge_store.py"
install -m 0644 "$source_dropin" "$canonical_dropin"
rm -f "$previous_dropin"
systemctl daemon-reload
systemctl restart "$service"

healthy=false
for _ in $(seq 1 15); do
  if systemctl is-active --quiet "$service" && curl -fsS http://127.0.0.1:8011/health >/dev/null; then healthy=true; break; fi
  sleep 1
done
if [[ "$healthy" != true ]]; then rollback; echo "backend health check failed; rolled back" >&2; exit 1; fi

if ! PYTHONPATH="$release" /opt/wanda-v3-backend/venv/bin/python - <<'PY'
from app.schemas import SeatZoneType
from app.wanda_quote import _unit_quote_cents
assert _unit_quote_cents(SeatZoneType.WPLUS, 5190, 4470, wplus_adjustment_cents=-290, wplus_member_price_threshold_cents=6000, regular_adjustment_cents=100) == 4900
PY
then
  rollback
  echo "backend quote rule verification failed; rolled back" >&2
  exit 1
fi
if grep -Eq 'visible_target_prices|buyer_zone_cap|member_unit if not request\.recognition\.official_selection\.is_selected' "$release/app/wanda_quote.py"; then
  rollback
  echo "backend retained a screenshot-price or unselected-seat bypass; rolled back" >&2
  exit 1
fi
systemctl show "$service" -p WorkingDirectory --value
curl -fsS http://127.0.0.1:8011/health
