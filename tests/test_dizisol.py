"""DiziSol adapter'ı: kaynak sırası, yedeğe geçiş, kart okuma (ağsız)."""
from types import SimpleNamespace

import pytest

from hayalet.core.extractor import ExtractError
from hayalet.core.models import Episode, Series
from hayalet.sites.dizisol_adapter import DizisolAdapter, _source_target

BASE = "https://dizisol.com"
MASTER = "#EXTM3U\n#EXTINF:10,\nseg.ts\n"


class FakeNet:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def get(self, url, referer=None, **kw):
        self.calls.append((url, referer))
        answer = self.answers.get(url)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, dict) or isinstance(answer, list):
            return SimpleNamespace(status_code=200, text="", json=lambda: answer)
        if answer is None:
            return SimpleNamespace(status_code=404, text="yok", json=lambda: {})
        return SimpleNamespace(status_code=200, text=answer, json=lambda: {})


SESSION = SimpleNamespace(base_url=BASE, referer=BASE)
SERIES = Series(name="X", slug="tv/1", type="Dizi", site="dizisol")
EP = Episode(season=1, number=1, url=f"{BASE}/api/x")


def test_source_target_bilinmeyen_ic_kodu_atlar():
    assert _source_target("setfilmizle::abc::", BASE) is None
    assert _source_target("", BASE) is None
    assert _source_target(None, BASE) is None


def test_source_target_goreli_ve_diziyou():
    assert _source_target("/api/p/1", BASE) == (f"{BASE}/api/p/1", f"{BASE}/")
    url, ref = _source_target("diziyou::10251::tr-sub", BASE)
    assert url == "https://storage.diziyou.one/episodes/10251/play.m3u8"
    assert ref == "https://www.diziyou.one/player/10251.html"


def test_olu_kaynaktan_sonrakine_gecer_ve_altyaziyi_o_kaynaktan_alir():
    net = FakeNet({
        EP.url: {"sources": [
            {"priority": 2, "m3u8Url": "https://b/play", "subtitleTr": "/sub/b"},
            {"priority": 1, "m3u8Url": "https://a/play", "subtitleTr": "/sub/a"},
        ], "subtitleTr": "/sub/ust"},
        "https://a/play": RuntimeError("zaman aşımı"),
        "https://b/play": MASTER,
    })
    stream = DizisolAdapter().build_stream(net, SESSION, EP, SERIES)
    assert stream.video_master_url == "https://b/play"
    assert stream.subtitle_url == f"{BASE}/sub/b"
    assert stream.video_referer == f"{BASE}/"


def test_hic_kaynak_calismazsa_acik_hata():
    net = FakeNet({EP.url: {"sources": [{"m3u8Url": "https://a/play"}]},
                   "https://a/play": "<html>403</html>"})
    with pytest.raises(ExtractError, match="çalışan kaynak yok"):
        DizisolAdapter().build_stream(net, SESSION, EP, SERIES)


def test_uyelere_ozel_icerige_kaynak_denenmez():
    net = FakeNet({EP.url: {"isPremium": True, "sources": [{"m3u8Url": "https://a/play"}]}})
    with pytest.raises(ExtractError, match="üyelere özel"):
        DizisolAdapter().build_stream(net, SESSION, EP, SERIES)
    assert [c[0] for c in net.calls] == [EP.url]


def test_bolumler_dizi_kartini_ayiklar_ve_siralar():
    items = [{"season": 0, "episode": 0}, {"season": 1, "episode": 2},
             {"season": 1, "episode": 1}, {"season": 2, "episode": 1, "isActive": False}]
    net = FakeNet({f"{BASE}/api/movies/by-tmdb/1/episodes": items})
    eps = DizisolAdapter().get_episodes(net, SESSION, SERIES)
    assert [(e.season, e.number) for e in eps] == [(1, 1), (1, 2)]


def test_film_tek_sentetik_bolum_ve_ag_istegi_yok():
    net = FakeNet({})
    film = Series(name="F", slug="movie/603", type="Film", site="dizisol")
    eps = DizisolAdapter().get_episodes(net, SESSION, film)
    assert eps[0].url == f"{BASE}/api/movies/by-tmdb/603?type=movie"
    assert net.calls == []


def test_kartlar_iki_bicimi_de_okur_ve_bolum_ekini_siler():
    a = DizisolAdapter()
    arama = a._series({"tmdbId": 1396, "title": "Breaking Bad", "year": 2008,
                       "poster": "/p.jpg", "type": "tv"})
    assert (arama.slug, arama.type, arama.year) == ("tv/1396", "Dizi", "2008")
    assert arama.poster_url == "https://image.tmdb.org/t/p/w500/p.jpg"
    katalog = a._series({"id": 5, "media_type": "movie", "name": "Y - 2. Bölüm",
                         "release_date": "2026-01-01", "vote_average": 6.5})
    assert (katalog.slug, katalog.name, katalog.year) == ("movie/5", "Y", "2026")
    assert a._series({"id": 5, "media_type": "person", "name": "Z"}) is None
