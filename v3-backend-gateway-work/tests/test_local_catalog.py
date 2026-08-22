import json
import sqlite3

from app.local_catalog import LocalWandaCatalog
from app.schemas import Recognition


def test_catalog_canonicalizes_unique_cinema_and_embedded_movie_name(tmp_path):
    path = tmp_path / "cinema_cache.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE cinemas (cinema_id TEXT, city_id TEXT, city_name TEXT, cinema_name TEXT);
            CREATE TABLE city_movies (city_id TEXT, day TEXT, movie_id TEXT, source TEXT, sort_order INTEGER, raw_json TEXT, updated_at INTEGER);
        """)
        connection.execute("INSERT INTO cinemas VALUES ('5794', '279', '张家港', '张家港万达广场店')")
        connection.execute("INSERT INTO city_movies VALUES (?, '0', '1', 'hot', 0, ?, 1)", ("279", json.dumps({"name": "空枪"}, ensure_ascii=False)))
    recognition = Recognition(cinema="张家港万达电影院", movie="朱一龙空枪", date="2026-08-19", showtime="17:10-19:34")
    resolved = LocalWandaCatalog(path).canonicalize(recognition)
    assert resolved.cinema == "张家港万达广场店"
    assert resolved.movie == "空枪"


def test_catalog_resolves_a_truncated_imax_suffix_when_the_branch_is_unique(tmp_path):
    path = tmp_path / "cinema_cache.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE cinemas (cinema_id TEXT, city_id TEXT, city_name TEXT, cinema_name TEXT);
            CREATE TABLE city_movies (city_id TEXT, day TEXT, movie_id TEXT, source TEXT, sort_order INTEGER, raw_json TEXT, updated_at INTEGER);
        """)
        connection.execute("INSERT INTO cinemas VALUES ('5794', '279', '苏州', '张家港万达广场店')")
        connection.execute("INSERT INTO cinemas VALUES ('613', '279', '苏州', '常熟万达广场店')")
    resolution = LocalWandaCatalog(path).resolve(Recognition(cinema="万达影城（张家港I…"))
    assert resolution.matched is True
    assert resolution.cinema_id == "5794"
    assert resolution.recognition.cinema == "张家港万达广场店"


def test_catalog_uses_a_visible_cinema_address_only_when_it_uniquely_matches(tmp_path):
    path = tmp_path / "cinema_cache.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE cinemas (cinema_id TEXT PRIMARY KEY, city_id TEXT, city_name TEXT, cinema_name TEXT, address TEXT, search_text TEXT);
            CREATE TABLE city_movies (city_id TEXT, day TEXT, movie_id TEXT, source TEXT, sort_order INTEGER, raw_json TEXT, updated_at INTEGER);
        """)
        connection.execute("INSERT INTO cinemas VALUES ('6601', '184', '亳州', '亳州谯城万达广场店', '亳州市谯城区汤王大道399号谯城万达广场三层', '')")
        connection.execute("INSERT INTO cinemas VALUES ('228', '184', '亳州', '亳州万达广场店', '亳州市谯城区希夷大道与杜仲路交叉口万达广场四层', '')")
    resolution = LocalWandaCatalog(path).resolve(Recognition(
        cinema="万达影城（南万达广场IMAX店）",
        cinema_address_hint="谯城区希夷大道与杜仲路交叉口万达广场",
    ))
    assert resolution.matched is True
    assert resolution.cinema_id == "228"
    assert resolution.recognition.cinema == "亳州万达广场店"


def test_catalog_accepts_a_unique_official_partner_cinema_without_requiring_wanda_in_its_name(tmp_path):
    path = tmp_path / "cinema_cache.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE cinemas (cinema_id TEXT, city_id TEXT, city_name TEXT, cinema_name TEXT);
            CREATE TABLE city_movies (city_id TEXT, day TEXT, movie_id TEXT, source TEXT, sort_order INTEGER, raw_json TEXT, updated_at INTEGER);
        """)
        connection.execute("INSERT INTO cinemas VALUES ('7109', 'xm', '厦门', '厦门寰映影城集美银泰店')")
    resolution = LocalWandaCatalog(path).resolve(Recognition(city="厦门", cinema="厦门寰映影城集美银泰店"))
    assert resolution.matched is True
    assert resolution.cinema_id == "7109"
    assert resolution.recognition.cinema == "厦门寰映影城集美银泰店"


def test_catalog_resolves_noisy_brand_and_format_words_to_the_unique_official_cinema(tmp_path):
    path = tmp_path / "cinema_cache.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE cinemas (cinema_id TEXT, city_id TEXT, city_name TEXT, cinema_name TEXT);
            CREATE TABLE city_movies (city_id TEXT, day TEXT, movie_id TEXT, source TEXT, sort_order INTEGER, raw_json TEXT, updated_at INTEGER);
        """)
        connection.execute("INSERT INTO cinemas VALUES ('380', 'cz', '常州', '常州新北万达广场店')")
        connection.execute("INSERT INTO cinemas VALUES ('299', 'cz', '常州', '常州武进万达广场店')")
    catalog = LocalWandaCatalog(path)
    resolution = catalog.resolve(Recognition(city="常州", cinema="万达影城（新北万达广场IMAX店）"))
    assert resolution.matched is True
    assert resolution.cinema_id == "380"
    assert resolution.recognition.cinema == "常州新北万达广场店"
    without_city = catalog.resolve(Recognition(cinema="万达影城（新北万达广场IMAX店）"))
    assert without_city.matched is True
    assert without_city.cinema_id == "380"


def test_catalog_uses_branch_and_format_together_instead_of_matching_a_generic_city_cinema(tmp_path):
    path = tmp_path / "cinema_cache.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE cinemas (cinema_id TEXT, city_id TEXT, city_name TEXT, cinema_name TEXT);
            CREATE TABLE city_movies (city_id TEXT, day TEXT, movie_id TEXT, source TEXT, sort_order INTEGER, raw_json TEXT, updated_at INTEGER);
        """)
        connection.execute("INSERT INTO cinemas VALUES ('307', 'xz', '徐州', '徐州云龙万达广场店（IMAX激光）')")
        connection.execute("INSERT INTO cinemas VALUES ('6729', 'zz', '株洲', '万达影城（株洲云龙万达广场PRIME店）')")
        connection.execute("INSERT INTO cinemas VALUES ('375', 'qz', '泉州', '泉州万达广场激光IMAX店')")
    resolution = LocalWandaCatalog(path).resolve(Recognition(cinema="万达影城（云龙万达广场激光IMAX店）"))
    assert resolution.matched is True
    assert resolution.cinema_id == "307"
    assert resolution.recognition.cinema == "徐州云龙万达广场店（IMAX激光）"


def test_catalog_does_not_turn_a_branchless_wanda_format_header_into_quanzhou(tmp_path):
    path = tmp_path / "cinema_cache.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE cinemas (cinema_id TEXT, city_id TEXT, city_name TEXT, cinema_name TEXT);
            CREATE TABLE city_movies (city_id TEXT, day TEXT, movie_id TEXT, source TEXT, sort_order INTEGER, raw_json TEXT, updated_at INTEGER);
        """)
        connection.execute("INSERT INTO cinemas VALUES ('375', 'qz', '泉州', '泉州万达广场激光IMAX店')")
    resolution = LocalWandaCatalog(path).resolve(Recognition(cinema="万达影城（万达广场IMAX激光店）"))
    assert resolution.matched is False
    assert resolution.cinema_id is None
    assert resolution.recognition.cinema == "万达影城（万达广场IMAX激光店）"


def test_catalog_resolves_a_unique_county_branch_when_wanda_and_imax_words_are_reordered(tmp_path):
    path = tmp_path / "cinema_cache.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE cinemas (cinema_id TEXT, city_id TEXT, city_name TEXT, cinema_name TEXT);
            CREATE TABLE city_movies (city_id TEXT, day TEXT, movie_id TEXT, source TEXT, sort_order INTEGER, raw_json TEXT, updated_at INTEGER);
        """)
        connection.execute("INSERT INTO cinemas VALUES ('613', '279', '苏州', '常熟万达广场店')")
        connection.execute("INSERT INTO cinemas VALUES ('299', 'cz', '常州', '常州武进万达广场店')")
    resolution = LocalWandaCatalog(path).resolve(Recognition(cinema="万达影城（常熟IMAX店）"))
    assert resolution.matched is True
    assert resolution.cinema_id == "613"
    assert resolution.recognition.cinema == "常熟万达广场店"


def test_catalog_does_not_guess_a_short_wanda_branch_when_it_is_not_unique(tmp_path):
    path = tmp_path / "cinema_cache.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE cinemas (cinema_id TEXT, city_id TEXT, city_name TEXT, cinema_name TEXT);
            CREATE TABLE city_movies (city_id TEXT, day TEXT, movie_id TEXT, source TEXT, sort_order INTEGER, raw_json TEXT, updated_at INTEGER);
        """)
        connection.execute("INSERT INTO cinemas VALUES ('1', 'a', '甲市', '常熟万达广场店')")
        connection.execute("INSERT INTO cinemas VALUES ('2', 'b', '乙市', '常熟万达影城店')")
    resolution = LocalWandaCatalog(path).resolve(Recognition(cinema="万达影城（常熟IMAX店）"))
    assert resolution.matched is False
    assert resolution.cinema_id is None


def test_catalog_resolves_a_unique_distinctive_branch_keyword_like_the_ticket_search(tmp_path):
    path = tmp_path / "cinema_cache.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE cinemas (cinema_id TEXT, city_id TEXT, city_name TEXT, cinema_name TEXT);
            CREATE TABLE city_movies (city_id TEXT, day TEXT, movie_id TEXT, source TEXT, sort_order INTEGER, raw_json TEXT, updated_at INTEGER);
        """)
        connection.execute("INSERT INTO cinemas VALUES ('7128', 'sh', '上海', '上海寰映影城陆悦天地店')")
        connection.execute("INSERT INTO cinemas VALUES ('2', 'sh', '上海', '上海寰映影城天空中心店')")
    resolution = LocalWandaCatalog(path).resolve(Recognition(cinema="浦东陆悦天地"))
    assert resolution.matched is True
    assert resolution.cinema_id == "7128"
    assert resolution.recognition.cinema == "上海寰映影城陆悦天地店"


def test_catalog_uses_explicit_city_hint_only_to_make_a_cinema_match_unique(tmp_path):
    path = tmp_path / "cinema_cache.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE cinemas (cinema_id TEXT, city_id TEXT, city_name TEXT, cinema_name TEXT);
            CREATE TABLE city_movies (city_id TEXT, day TEXT, movie_id TEXT, source TEXT, sort_order INTEGER, raw_json TEXT, updated_at INTEGER);
        """)
        connection.execute("INSERT INTO cinemas VALUES ('1', 'gz', '广州', '广州中都荟万达影城')")
        connection.execute("INSERT INTO cinemas VALUES ('2', 'other', '桂林', '桂林中都荟万达影城')")
    unresolved = LocalWandaCatalog(path).canonicalize(Recognition(cinema="中都荟万达影城"))
    assert unresolved.cinema == "中都荟万达影城"
    resolved = LocalWandaCatalog(path).canonicalize(Recognition(city="广州", cinema="中都荟万达影城"))
    assert resolved.cinema == "广州中都荟万达影城"
    assert resolved.city == "广州"
