"""Resolve OCR facts through Wanda's read-only match endpoint before asking buyers."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import re
import unicodedata

from integrations.wanda.bridge import CinemaRoute
from .wanda_showtime import fetch_shows, pick_show, normalize_minutes


class RecoveryError(ValueError):
    pass


def normalized(value):
    return re.sub(r'[\s()（）·]', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


def hall_identity(value):
    # UI may append the label 银幕 to the displayed hall name. Do not remove
    # hall numbers or use a broad fuzzy match between different actual halls.
    return normalized(value).removesuffix('银幕')


def eligible(result, bridge=None):
    raw = result.raw_results or {}
    if result.match_level == 'EXACT':
        return False
    # Branding is not route authority. A configured read-only bridge can resolve
    # an unmatched cinema even when its OCR name contains no brand at all.
    return ('万达' in str(raw.get('cinema') or '') or
            (bridge is not None and callable(getattr(bridge, 'match_facts', None))
             and all(raw.get(k) for k in ('city', 'cinema', 'film', 'hall', 'showtime'))))


def recover(bridge, result):
    raw = result.raw_results or {}
    fields = {k: str(raw.get(v) or '').strip() for k, v in
              [('city', 'city'), ('cinema', 'cinema'), ('movie', 'film'), ('hall', 'hall'), ('showtime', 'showtime')]}
    if not all(fields.values()):
        raise RecoveryError('图片中的城市、完整影院、影片或场次信息还不完整，请补充缺失信息；暂不选择异地候选。')
    try:
        wanted = datetime.fromisoformat(fields['showtime'])
    except ValueError:
        raise RecoveryError('图片中的观影日期和开场时间尚未核实，请补充。')
    payload = bridge.match_facts(fields)
    data = payload.get('data') or {}
    cinema, city, match = (data.get(k) or {} for k in ('cinema', 'city', 'showtime_match'))
    matched = data.get('showtime') or {}
    if (payload.get('success') is not True or match.get('candidate_count') != 1
            or not cinema.get('id') or not city.get('id') or not matched.get('id')):
        raise RecoveryError('已按图片查询万达，但还没有唯一匹配到影院和场次，请核对完整影院名称及开场时间。')
    if normalized(cinema.get('name')) != normalized(fields['cinema']):
        raise RecoveryError('万达返回的影院名称与图片尚未一致，请确认完整影院名称；不会自动选择其他影院。')
    wanted_city = fields['city'].removesuffix('市')
    city_ok = (wanted_city == str(city.get('name') or '').removesuffix('市') or
               (wanted_city in str(cinema.get('name') or '') and wanted_city + '市' in str(cinema.get('address') or '')))
    if not city_ok:
        raise RecoveryError('万达返回的影院城市与图片不一致，请核对城市和影院。')
    # Independently verify the returned show against Wanda's cinema schedule.
    day = wanted.strftime('%Y%m%d')
    show = pick_show(fetch_shows(bridge, wanda_cinema_id=str(cinema['id']), date=day),
                     date=day, minutes=normalize_minutes(wanted.strftime('%H:%M')), film_name=fields['movie'], tolerance_minutes=0)
    if (str(show.showtime_id) != str(matched['id'])
            or hall_identity(show.hall_name) != hall_identity(fields['hall'])
            or (matched.get('hall') and hall_identity(matched['hall']) != hall_identity(show.hall_name))):
        raise RecoveryError('万达场次或影厅与截图未唯一核实，请补充完整场次截图。')
    if normalized(show.film_name) != normalized(fields['movie']):
        raise RecoveryError('万达影片名与截图不一致，请核对影片。')
    actual_time = datetime.fromtimestamp(show.realtime_ms / 1000, timezone(timedelta(hours=8)))
    expected_time = wanted.replace(tzinfo=timezone(timedelta(hours=8))) if wanted.tzinfo is None else wanted
    if actual_time != expected_time:
        raise RecoveryError('万达开场时间与截图不一致，请核对日期及时间。')
    seats = raw.get('seat') or []
    if not isinstance(seats, list) or any(not isinstance(s, dict) or not s.get('seatName') for s in seats):
        raise RecoveryError('截图中的座位信息不完整，请补充座位；不会忽略已选座位。')
    recovered = deepcopy(result)
    recovered.no_match_reason = None
    recovered.cinema_id = recovered.movie_id = recovered.show_id = None
    recovered.cinema_name = str(cinema['name'])
    recovered.cinema_address = str(cinema.get('address') or '')
    recovered.city_name = fields['city']
    recovered.movie_name = show.film_name
    recovered.start_time = wanted.isoformat()
    recovered.hall_name = show.hall_name
    recovered.brand_name = '万达影城'
    recovered.candidates = {}
    recovered.final_results = {}
    recovered.match_level = 'EXACT'
    recovered.wanda_verified_route = CinemaRoute(mapped=True, wanda_city_id=str(city['id']),
        wanda_cinema_id=str(cinema['id']), wanda_cinema_name=str(cinema['name']))
    recovered.wanda_verified_show_id = str(show.showtime_id)
    # Only literal seat labels; authoritative prices/availability come from Wanda.
    recovered.seats = [{'seatNo': s['seatName']} for s in raw.get('seat', [])
                       if isinstance(s, dict) and s.get('seatName')]
    return recovered
