"""selcukflix adapter'ı (ağsız): AES çözme katmanı + ortak Dizipal-Next zinciri."""
import base64
import json
from types import SimpleNamespace

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from hayalet.core.models import Episode, Series
from hayalet.sites.selcukflix_adapter import SelcukflixAdapter, _KEY, _IV

BASE = "https://selcukflix.app"
SESSION = SimpleNamespace(base_url=BASE, referer=BASE)


def _enc(obj) -> str:
    """Testte sitenin şifrelemesini taklit et: JSON -> AES-256-CBC -> base64."""
    raw = json.dumps(obj).encode()
    ct = AES.new(_KEY, AES.MODE_CBC, _IV).encrypt(pad(raw, AES.block_size))
    return base64.b64encode(ct).decode()


def _next_data_html(secure: dict) -> str:
    payload = {"props": {"pageProps": {"secureData": _enc(secure)}}}
    return ('<html><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload) + "</script></html>")


SERIES_SECURE = {"content": {"result": {"RelatedResults": {
    "getSerieSeasonAndEpisodes": {"result": [
        {"season_no": 1, "episodes": [
            {"season_no": 1, "episode_no": 2, "episode_text": "2. Bölüm"},
            {"season_no": 1, "episode_no": 1, "episode_text": "1. Bölüm"},
        ]},
    ]}}}}}

NP_CONFIG = {"sources": [{"title": "Orijinal", "kind": "orig",
                          "master": "//four.pichive.online/nm.php?v=TOK"}],
             "subtitles": [{"label": "Türkçe", "lang": "tr", "tok": "TR"}]}
IFRAME_HTML = "<script>window.NP_CONFIG = " + json.dumps(NP_CONFIG) + ";</script>"
EP_SECURE = {"content": {"result": {"RelatedResults": {
    "getEpisodeSources": {"result": [
        {"source_content": '<iframe src="//four.pichive.online/iframe.php?v=H"></iframe>'},
    ]}}}}}
MASTER = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n"


class FakeNet:
    def __init__(self, answers, search=None):
        self.answers = answers
        self.search = search or []

    def get(self, url, referer=None, **kw):
        if url not in self.answers:
            return SimpleNamespace(status_code=404, text="yok", json=lambda: {})
        body = self.answers[url]
        if isinstance(body, Exception):
            raise body
        return SimpleNamespace(status_code=200, text=body, json=lambda: {})

    def post(self, url, referer=None, params=None, **kw):
        return SimpleNamespace(status_code=200, text="",
                               json=lambda: {"response": _enc({"result": self.search})})


def test_arama_sifreli_yaniti_cozer():
    net = FakeNet({}, search=[
        {"used_slug": "dizi/spooks", "used_type": "Series", "object_name": "Spooks",
         "object_release_year": 2002},
        {"used_slug": "film/matrix", "used_type": "Movie", "object_name": "Matrix"},
    ])
    res = SelcukflixAdapter().search(net, SESSION, "x")
    assert [(s.name, s.slug, s.type) for s in res] == [
        ("Spooks", "dizi/spooks", "Dizi"), ("Matrix", "film/matrix", "Film")]


def test_bolumler_sifreli_secureData_uzerinden():
    series = Series(name="Spooks", slug="dizi/spooks", type="Dizi", site="selcukflix")
    net = FakeNet({series.url(BASE): _next_data_html(SERIES_SECURE)})
    eps = SelcukflixAdapter().get_episodes(net, SESSION, series)
    assert [(e.season, e.number) for e in eps] == [(1, 1), (1, 2)]


def test_build_stream_pichive_master_verir():
    ep = Episode(season=1, number=1, url=f"{BASE}/dizi/spooks/sezon-1/bolum-1")
    series = Series(name="Spooks", slug="dizi/spooks", type="Dizi", site="selcukflix")
    net = FakeNet({
        ep.url: _next_data_html(EP_SECURE),
        "https://four.pichive.online/iframe.php?v=H": IFRAME_HTML,
        "https://four.pichive.online/nm.php?v=TOK": MASTER,
    })
    stream = SelcukflixAdapter().build_stream(net, SESSION, ep, series)
    assert stream.video_master_url == "https://four.pichive.online/nm.php?v=TOK"
    assert stream.subtitle_url == "https://four.pichive.online/nsub.php?v=TR"
