"""yabancidizi adapter'ı (ağsız): arama film/dizi ayrımı, ruplay->ok.ru zinciri."""
import html as _html
import json
from types import SimpleNamespace

import pytest

from hayalet.core.extractor import ExtractError
from hayalet.core.models import Episode, Series
from hayalet.sites.yabancidizi_adapter import YabancidiziAdapter

BASE = "https://yabancidizi.news"
SESSION = SimpleNamespace(base_url=BASE, referer=BASE)

SEARCH = {"data": {"result": [
    {"s_id": "1", "s_type": "1", "s_link": "venom-izle", "s_name": "Venom", "s_year": "2018"},
    {"s_id": "2", "s_type": "0", "s_link": "wednesday", "s_name": "Wednesday", "s_year": "2022"},
    {"s_id": "3", "s_type": "1", "s_link": "", "s_name": "Boş"},  # slug yok → atlanır
]}}
FILM_HTML = '<div class="item active" data-hash="h" data-link="CLEANTOK" data-querytype="alternate">src</div>'
RUPLAY_OKRU = '<iframe src="https://ok.ru/videoembed/2786564049542" scrolling="no"></iframe>'
OKRU_META = {"flashvars": {"metadata": {"hlsManifestUrl": "https://vd.okcdn.ru/v.m3u8"}}}
MASTER = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n"


class FakeNet:
    def __init__(self, get_answers, search=None):
        self.get_answers = get_answers
        self.search = search

    def post(self, url, referer=None, data=None, **kw):
        return SimpleNamespace(status_code=200, text="", json=lambda: self.search)

    def get(self, url, referer=None, **kw):
        body = self.get_answers.get(url)
        if body is None:
            return SimpleNamespace(status_code=404, text="yok", json=lambda: {})
        if isinstance(body, Exception):
            raise body
        return SimpleNamespace(status_code=200, text=body, json=lambda: {})


def test_arama_film_dizi_ayirir():
    net = FakeNet({}, search=SEARCH)
    res = YabancidiziAdapter().search(net, SESSION, "x")
    assert [(s.name, s.slug, s.type) for s in res] == [
        ("Venom", "film/venom-izle", "Film"),
        ("Wednesday", "dizi/wednesday", "Dizi")]


def test_film_tek_sentetik_bolum():
    film = Series(name="Venom", slug="film/venom-izle", type="Film", site="yabancidizi")
    eps = YabancidiziAdapter().get_episodes(FakeNet({}), SESSION, film)
    assert len(eps) == 1 and eps[0].url == f"{BASE}/film/venom-izle"


def test_build_stream_ruplay_okru_zinciri():
    film = Series(name="Venom", slug="film/venom-izle", type="Film", site="yabancidizi")
    ep = Episode(season=1, number=1, url=f"{BASE}/film/venom-izle")
    okru_html = '<div data-options="' + _html.escape(json.dumps(OKRU_META)) + '"></div>'
    net = FakeNet({
        ep.url: FILM_HTML,
        f"{BASE}/api/ruplay/CLEANTOK": RUPLAY_OKRU,
        "https://ok.ru/videoembed/2786564049542": okru_html,
        "https://vd.okcdn.ru/v.m3u8": MASTER,
    })
    stream = YabancidiziAdapter().build_stream(net, SESSION, ep, film)
    assert stream.video_master_url == "https://vd.okcdn.ru/v.m3u8"
    assert stream.video_referer == "https://ok.ru/"


def test_build_stream_kaynak_yoksa_hata():
    film = Series(name="X", slug="film/x", type="Film", site="yabancidizi")
    ep = Episode(season=1, number=1, url=f"{BASE}/film/x")
    net = FakeNet({ep.url: "<p>kaynak yok</p>"})
    with pytest.raises(ExtractError, match="data-link.*bulunamadı"):
        YabancidiziAdapter().build_stream(net, SESSION, ep, film)
