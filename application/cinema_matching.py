"""City-scoped directory matching. Similarity ranks evidence, never grants a route."""
import re
import unicodedata
from difflib import SequenceMatcher


def title_key(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', str(value or '')).casefold()
                   if c.isalnum())


def name_key(value, city=''):
    value=unicodedata.normalize('NFKC', str(value or '')).casefold()
    # Amenities and chain labels do not identify a branch. No cinema-specific aliases.
    value=re.sub(r'(?:地下)?停车.*|儿童.*|首赠.*', '', value)
    for term in ('imaxgt','imax','cinity','real3d','4dx','双激光','激光','杜比','万达广场',
                 '儒意影城','万达影城','寰映影城','电影院','影城','影院','万达','广场','店'):
        value=value.replace(term,'')
    city=title_key(city).removesuffix('市')
    value=title_key(value)
    return value[len(city):].removeprefix('市') if city and value.startswith(city) else value


def match_cinema(api, text, *, city_code='', city_name=''):
    """Return a unique supported identity, a shortlist, or an explicit stop reason."""
    city_name=str(city_name or '').removesuffix('市')
    key=name_key(text,city_name)
    if len(key)<2:
        return dict(status='NEEDS_BRANCH',candidates=[],reason='请补充影院门店名称，只有品牌名无法确定影院。')
    try:
        if not city_code:
            if not city_name:
                return dict(status='NEEDS_CITY',candidates=[],reason='请补充影院所在城市。')
            cities=[r for r in api.ch4_city_list(keyword=city_name)
                    if str(r.get('name') or r.get('cityName') or '').removesuffix('市')==city_name]
            if len(cities)!=1:
                return dict(status='NEEDS_CITY',candidates=[],reason='影院所在城市尚未唯一确认，请补充城市。')
            city_code=str(cities[0].get('code') or cities[0].get('cityCode') or '')
        if not city_code:
            return dict(status='NEEDS_CITY',candidates=[],reason='影院所在城市尚未唯一确认，请补充城市。')
        rows=[]
        complete=False
        for page in range(1,4):
            data=api.ch4_cinema_list(city_code=str(city_code),page=page,page_size=200)
            items=data.get('list') if isinstance(data,dict) else None
            if not isinstance(items,list):
                raise ValueError('directory shape')
            rows.extend(items)
            total=data.get('total')
            if (total is not None and len(rows)>=int(total)) or (total is None and len(items)<200):
                complete=True
                break
        if not complete:
            return dict(status='DIRECTORY_INCOMPLETE',candidates=[],reason='影院目录未查询完整，暂不能确认唯一门店，请店家核对。')
        ranked={}
        for row in rows:
            if not isinstance(row,dict):
                continue
            if row.get('cityCode') and str(row['cityCode'])!=str(city_code):
                continue
            if city_name and row.get('cityName') and str(row['cityName']).removesuffix('市')!=city_name:
                continue
            name=str(row.get('name') or row.get('cinemaName') or '')
            identity=row.get('cinemaId') or row.get('id')
            if not identity or not name:
                continue
            branch=name_key(name,city_name)
            score=SequenceMatcher(None,key,branch).ratio() if branch else 0
            if score<0.5:
                continue
            candidate=dict(cinemaId=identity,name=name,cityCode=str(city_code),
                cityName=row.get('cityName') or city_name,address=row.get('address') or '',
                score=round(score,4),exact_branch=key==branch)
            ranked[str(identity)]=candidate
        candidates=sorted(ranked.values(),key=lambda r:r['score'],reverse=True)
        top=candidates[0] if candidates else None
        margin=top['score']-(candidates[1]['score'] if len(candidates)>1 else 0) if top else 0
        # Automatic confirmation requires identical distinctive branch text.
        # Fuzzy scores alone only produce candidates, never change cinema IDs.
        if top and top['exact_branch'] and margin>=0.2:
            return dict(status='MATCHED',cinema=top,candidates=candidates[:3])
        return dict(status='AMBIGUOUS' if candidates else 'NOT_FOUND',candidates=candidates[:3],
                    reason='请确认具体门店：'+'；'.join(r['name'] for r in candidates[:3])
                    if candidates else '当前城市目录未找到可确认的影院，请补充城市、门店或地址。')
    except Exception:
        return dict(status='DIRECTORY_UNAVAILABLE',candidates=[],reason='影院目录暂时未查询成功，已保留文字需求，请店家核对；不会改选其他影院。')
