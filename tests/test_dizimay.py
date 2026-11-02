"""dizimay adapter'ı + ok.ru çözücüsü (ağsız): kart, bölüm, ok.ru master."""
import html
import json
from types import SimpleNamespace

import pytest

from hayalet.core.extractor import ExtractError
from hayalet.core.models import Episode, Series
from hayalet.sites import _okru
from hayalet.sites.dizimay_adapter import DizimayAdapter

BASE = "https://dizimay.pro"
SESSION = SimpleNamespace(base_url=BASE, referer=BASE)

SEARCH_HTML = (
    '<a href="https://dizimay.pro/dizi/stealth/" title="Stealth"><h3>Stealth</h3></a>'
    '<a href="https://dizimay.pro/dizi/stealth/"><span>Stealth</span></a>'  # tekrar → tek
    '<a href="https://dizimay.pro/dizi/the-glory/"><h3>The Glory</h3></a>'
)
SERIES_HTML = (
    '<a href="https://dizimay.pro/bolum/stealth-2-bolum/">2</a>'
    '<a href="https://dizimay.pro/bolum/stealth-1-bolum/">1</a>'
)
OKRU_META = {"flashvars": {"metadata": {"hlsManifestUrl": "https://vd1.okcdn.ru/video.m3u8?x=1"}}}
OKRU_HTML = '<div data-options="' + html.escape(json.dumps(OKRU_META)) + '"></div>'
EP_HTML = '<iframe src="//ok.ru/videoembed/123?nochat=1" width="560"></iframe>'
MASTER = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n"


class FakeNet:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def get(self, url, referer=None, **kw):
        self.calls.append(url)
        if url not in self.answers:
            return SimpleNamespace(status_code=404, text="yok", json=lambda: {})
        body = self.answers[url]
        if isinstance(body, Exception):
            raise body
        return SimpleNamespace(status_code=200, text=body, json=lambda: {})


def test_arama_dizi_kartlari_tekille_ve_isim_okur():
    net = FakeNet({f"{BASE}/?s=x": SEARCH_HTML})
    res = DizimayAdapter().search(net, SESSION, "x")
    assert [(s.name, s.slug) for s in res] == [
        ("Stealth", "dizi/stealth"), ("The Glory", "dizi/the-glory")]


def test_bolumler_numara_ayiklar_ve_siralar():
    series = Series(name="Stealth", slug="dizi/stealth", type="Dizi", site="dizimay")
    net = FakeNet({series.url(BASE): SERIES_HTML})
    eps = DizimayAdapter().get_episodes(net, SESSION, series)
    assert [(e.season, e.number) for e in eps] == [(1, 1), (1, 2)]


def test_okru_cozucu_hls_master_dondurur():
    net = FakeNet({"https://ok.ru/videoembed/123?nochat=1": OKRU_HTML})
    master, ref = _okru.resolve(net, "//ok.ru/videoembed/123?nochat=1", BASE)
    assert master == "https://vd1.okcdn.ru/video.m3u8?x=1"
    assert ref == "https://ok.ru/"


def test_build_stream_okru_master_verir():
    ep = Episode(season=1, number=1, url=f"{BASE}/bolum/stealth-1-bolum/")
    series = Series(name="Stealth", slug="dizi/stealth", type="Dizi", site="dizimay")
    net = FakeNet({
        ep.url: EP_HTML,
        "https://ok.ru/videoembed/123?nochat=1": OKRU_HTML,
        "https://vd1.okcdn.ru/video.m3u8?x=1": MASTER,
    })
    stream = DizimayAdapter().build_stream(net, SESSION, ep, series)
    assert stream.video_master_url == "https://vd1.okcdn.ru/video.m3u8?x=1"
    assert stream.video_referer == "https://ok.ru/"


def test_build_stream_okru_yoksa_acik_hata():
    ep = Episode(season=1, number=1, url=f"{BASE}/bolum/x-1-bolum/")
    series = Series(name="X", slug="dizi/x", type="Dizi", site="dizimay")
    net = FakeNet({ep.url: "<p>oynatıcı kaldırıldı</p>"})
    with pytest.raises(ExtractError, match="ok.ru oynatıcısı yok"):
        DizimayAdapter().build_stream(net, SESSION, ep, series)
