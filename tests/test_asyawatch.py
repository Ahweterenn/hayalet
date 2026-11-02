"""asyawatch adapter'ı + pichive çözücüsü (ağsız): kart, bölüm, kaynak, altyazı."""
import base64
import json
from types import SimpleNamespace

import pytest

from hayalet.core.extractor import ExtractError
from hayalet.core.models import Episode, Series
from hayalet.sites import _pichive
from hayalet.sites.asyawatch_adapter import AsyawatchAdapter

BASE = "https://asyawatch.com"
SESSION = SimpleNamespace(base_url=BASE, referer=BASE)


def _b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def _next_data_html(secure: dict) -> str:
    payload = {"props": {"pageProps": {"secureData": _b64(secure)}}}
    return ('<html><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload) + "</script></html>")


# --- Gerçek yapıya birebir sadık sahte veriler ---------------------------
SERIES_SECURE = {"content": {"result": {"RelatedResults": {
    "getSerieSeasonAndEpisodes": {"result": [
        {"season_no": 1, "episodes": [
            {"season_no": 1, "episode_no": 2, "episode_text": "2. Bölüm"},
            {"season_no": 1, "episode_no": 1, "episode_text": "1. Bölüm"},
            {"season_no": 0, "episode_no": 0, "episode_text": "Fragman"},  # atlanmalı
        ]},
        {"season_no": 2, "episodes": [
            {"season_no": 2, "episode_no": 1, "episode_text": "1. Bölüm"},
        ]},
    ]}}}}}

EP_SECURE = {"content": {"result": {"RelatedResults": {
    "getEpisodeSources": {"result": [
        {"source_content": '<iframe src="//four.pichive.online/iframe.php?v=HASH1" frameborder="0"></iframe>'},
        {"source_content": '<iframe src="//four.pichive.online/iframe.php?v=HASH2"></iframe>'},
    ]}}}}}

NP_CONFIG = {"sources": [{"title": "Orijinal", "kind": "orig",
                          "master": "//four.pichive.online/nm.php?v=TOK"}],
             "subtitles": [{"label": "İngilizce", "lang": "en", "tok": "EN"},
                           {"label": "Türkçe", "lang": "tr", "tok": "TR"}]}
IFRAME_HTML = "<script>window.NP_CONFIG = " + json.dumps(NP_CONFIG) + ";</script>"
MASTER = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n"


class FakeNet:
    def __init__(self, answers, search=None):
        self.answers = answers
        self.search = search or []
        self.calls = []

    def _resp(self, body):
        if isinstance(body, Exception):
            raise body
        return SimpleNamespace(status_code=200, text=body,
                               json=lambda: json.loads(body) if body.strip().startswith(("{", "[")) else {})

    def get(self, url, referer=None, **kw):
        self.calls.append(url)
        if url not in self.answers:
            return SimpleNamespace(status_code=404, text="yok", json=lambda: {})
        return self._resp(self.answers[url])

    def post(self, url, referer=None, params=None, **kw):
        self.calls.append((url, params))
        return SimpleNamespace(status_code=200, text="",
                               json=lambda: {"response": _b64({"result": self.search})})


def test_arama_karti_dizi_ve_film_ayirir():
    a = AsyawatchAdapter()
    net = FakeNet({}, search=[
        {"used_slug": "dizi/spooks", "used_type": "Series", "object_name": "Spooks",
         "object_release_year": 2002, "object_related_imdb_point": 8.3},
        {"used_slug": "film/matrix", "used_type": "Movie", "object_name": "Matrix",
         "object_release_year": 1999},
        {"used_slug": "", "object_name": "Boş"},  # slug yok → atlanır
    ])
    res = a.search(net, SESSION, "x")
    assert [(s.name, s.slug, s.type, s.year) for s in res] == [
        ("Spooks", "dizi/spooks", "Dizi", "2002"),
        ("Matrix", "film/matrix", "Film", "1999"),
    ]


def test_bolumler_sezon_bolum_ayiklar_ve_url_kurar():
    a = AsyawatchAdapter()
    series = Series(name="Spooks", slug="dizi/spooks", type="Dizi", site="asyawatch")
    net = FakeNet({f"{BASE}/dizi/spooks": _next_data_html(SERIES_SECURE)})
    eps = a.get_episodes(net, SESSION, series)
    assert [(e.season, e.number) for e in eps] == [(1, 1), (1, 2), (2, 1)]
    assert eps[0].url == f"{BASE}/dizi/spooks/sezon-1/bolum-1"


def test_film_tek_sentetik_bolum_ag_istegi_yok():
    a = AsyawatchAdapter()
    film = Series(name="Matrix", slug="film/matrix", type="Film", site="asyawatch")
    net = FakeNet({})
    eps = a.get_episodes(net, SESSION, film)
    assert len(eps) == 1 and eps[0].url == f"{BASE}/film/matrix"
    assert net.calls == []


def test_pichive_cozucu_turkce_altyaziyi_ve_masteri_secer():
    net = FakeNet({"https://four.pichive.online/iframe.php?v=HASH1": IFRAME_HTML})
    master, ref, sub = _pichive.resolve(net, "//four.pichive.online/iframe.php?v=HASH1", BASE)
    assert master == "https://four.pichive.online/nm.php?v=TOK"
    assert ref == "https://four.pichive.online/"
    assert sub == "https://four.pichive.online/nsub.php?v=TR"   # Türkçe tercih edildi


def test_build_stream_ilk_calisan_kaynagi_alir():
    ep = Episode(season=1, number=1, url=f"{BASE}/dizi/spooks/sezon-1/bolum-1")
    series = Series(name="Spooks", slug="dizi/spooks", type="Dizi", site="asyawatch")
    net = FakeNet({
        ep.url: _next_data_html(EP_SECURE),
        "https://four.pichive.online/iframe.php?v=HASH1": IFRAME_HTML,
        "https://four.pichive.online/nm.php?v=TOK": MASTER,
    })
    stream = AsyawatchAdapter().build_stream(net, SESSION, ep, series)
    assert stream.video_master_url == "https://four.pichive.online/nm.php?v=TOK"
    assert stream.subtitle_url == "https://four.pichive.online/nsub.php?v=TR"


def test_build_stream_kaynak_yoksa_acik_hata():
    ep = Episode(season=1, number=1, url=f"{BASE}/dizi/x/sezon-1/bolum-1")
    series = Series(name="X", slug="dizi/x", type="Dizi", site="asyawatch")
    bos = {"content": {"result": {"RelatedResults": {"getEpisodeSources": {"result": []}}}}}
    net = FakeNet({ep.url: _next_data_html(bos)})
    with pytest.raises(ExtractError, match="oynatılacak kaynak yok"):
        AsyawatchAdapter().build_stream(net, SESSION, ep, series)
