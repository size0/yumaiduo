#!/usr/bin/env bash
set -euo pipefail

expected_release=/opt/wanda-v3-backend/releases/v3-unselected-wplus-member-price-20260819-007000
release=/opt/wanda-v3-backend/releases/v3-quote-rules-regression-20260819-012852
source_file=/tmp/wanda_quote.py
service=wanda-v3-backend
service_dropin=/etc/systemd/system/wanda-v3-backend.service.d/zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz-quote-rules-regression-20260819-012852.conf
source_dropin=/tmp/wanda-v3-quote-rules-regression-20260819-012852.service.conf
old_release="$(systemctl show "$service" -p WorkingDirectory --value)"

if [[ "$old_release" != "$expected_release" ]]; then
  echo "refusing deployment: expected $expected_release, found $old_release" >&2
  exit 1
fi
test -f "$source_file"
test -f "$source_dropin"
test ! -e "$release"

rollback() {
  rm -f "$service_dropin"
  systemctl daemon-reload
  systemctl restart "$service"
  rm -rf "$release"
}

mkdir -p "$release"
cp -al "$old_release/." "$release/"
install -m 0644 "$source_file" "$release/app/wanda_quote.py"
install -D -m 0644 "$source_dropin" "$service_dropin"
systemctl daemon-reload
systemctl restart "$service"

healthy=false
for _ in $(seq 1 15); do
  if systemctl is-active --quiet "$service" && curl -fsS http://127.0.0.1:8011/health >/dev/null; then
    healthy=true
    break
  fi
  sleep 1
done
if [[ "$healthy" != true ]]; then
  rollback
  echo "quote-rules release failed health check and was rolled back" >&2
  exit 1
fi

# Guard the precise P0 behavior and ensure no unselected-seat bypass remains.
PYTHONPATH="$release" /opt/wanda-v3-backend/venv/bin/python - <<'PY'
from app.schemas import SeatZoneType
from app.wanda_quote import _unit_quote_cents

assert _unit_quote_cents(
    SeatZoneType.WPLUS,
    5190,
    4470,
    wplus_adjustment_cents=-290,
    wplus_member_price_threshold_cents=6000,
    regular_adjustment_cents=100,
) == 4900
PY
if grep -Fq 'member_unit if not request.recognition.official_selection.is_selected' "$release/app/wanda_quote.py"; then
  rollback
  echo "quote-rules release retained the unselected-seat bypass and was rolled back" >&2
  exit 1
fi

systemctl show "$service" -p WorkingDirectory --value
curl -fsS http://127.0.0.1:8011/health
