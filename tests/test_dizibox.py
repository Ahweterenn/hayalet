"""dizibox adapter'ı + molystream çözücüsü (ağsız): arşiv süzme, kapsam, zincir."""
from types import SimpleNamespace

import pytest

from hayalet.core.extractor import ExtractError
from hayalet.core.models import Episode, Series
from hayalet.sites import _molystream
from hayalet.sites.dizibox_adapter import DiziboxAdapter

BASE = "https://www.dizibox.live"
SESSION = SimpleNamespace(base_url=BASE, referer=BASE)

ARCHIVE = (
    '<a title="Lost izle" href="https://www.dizibox.live/diziler/lost-izle-1/">Lost</a>'
    '<a title="Lost in Space izle" href="https://www.dizibox.live/diziler/lost-in-space/">x</a>'
    '<a title="Breaking Bad izle" href="https://www.dizibox.live/diziler/breaking-bad/">y</a>'
)
# Lost dizi sayfası: kendi bölümleri + kenar çubuğunda başka dizi (elenmeli)
SERIES_HTML = (
    '<a href="https://www.dizibox.live/lost-1-sezon-2-bolum-izle/">2</a>'
    '<a href="https://www.dizibox.live/lost-1-sezon-1-bolum-izle/">1</a>'
    '<a href="https://www.dizibox.live/lost-in-space-1-sezon-5-bolum-izle/">yan</a>'  # elenecek
)
EP_HTML = "<iframe src='https://www.dizibox.live/player/king/king.php?v=HASH'></iframe>"
KING_HTML = "<iframe src='https://dbx.molystream.org/embed/1543-abc'></iframe>"
MASTER = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n"


class FakeNet:
    def __init__(self, answers):
        self.answers = answers

    def get(self, url, referer=None, params=None, **kw):
        key = url
        if params:
            key = url + "?" + "&".join(f"{k}={v}" for k, v in params.items())
        body = self.answers.get(key, self.answers.get(url))
        if body is None:
            return SimpleNamespace(status_code=404, text="yok", json=lambda: {})
        if isinstance(body, Exception):
            raise body
        return SimpleNamespace(status_code=200, text=body, json=lambda: {})


def test_arama_arsivi_suzer():
    net = FakeNet({f"{BASE}/arama/": ARCHIVE})
    res = DiziboxAdapter().search(net, SESSION, "lost")
    assert [s.name for s in res] == ["Lost", "Lost in Space"]  # Breaking Bad elenir
    assert res[0].slug == "diziler/lost-izle-1"


def test_bolumler_izle_ekini_soyar_ve_slug_kapsar():
    series = Series(name="Lost", slug="diziler/lost-izle-1", type="Dizi", site="dizibox")
    net = FakeNet({series.url(BASE): SERIES_HTML})
    eps = DiziboxAdapter().get_episodes(net, SESSION, series)
    # lost-in-space (kenar çubuğu) ELENMELİ, sadece lost-N-sezon kalır
    assert [(e.season, e.number) for e in eps] == [(1, 1), (1, 2)]
    assert all("/lost-" in e.url and "space" not in e.url for e in eps)


def test_molystream_sheila_master_kurar():
    net = FakeNet({"https://dbx.molystream.org/embed/sheila/1543-abc": MASTER})
    master, ref = _molystream.resolve(net, "https://dbx.molystream.org/embed/1543-abc", BASE)
    assert master == "https://dbx.molystream.org/embed/sheila/1543-abc"
    assert ref == "https://dbx.molystream.org/embed/1543-abc"


def test_build_stream_king_molystream_zinciri():
    ep = Episode(season=1, number=1, url=f"{BASE}/lost-1-sezon-1-bolum-izle/")
    series = Series(name="Lost", slug="diziler/lost-izle-1", type="Dizi", site="dizibox")
    net = FakeNet({
        ep.url: EP_HTML,
        "https://www.dizibox.live/player/king/king.php?v=HASH": KING_HTML,
        "https://dbx.molystream.org/embed/sheila/1543-abc": MASTER,
    })
    stream = DiziboxAdapter().build_stream(net, SESSION, ep, series)
    assert stream.video_master_url == "https://dbx.molystream.org/embed/sheila/1543-abc"


def test_build_stream_youtube_atlanir_kaynak_yoksa_hata():
    ep = Episode(season=1, number=1, url=f"{BASE}/x-1-sezon-1-bolum-izle/")
    series = Series(name="X", slug="diziler/x", type="Dizi", site="dizibox")
    net = FakeNet({ep.url: "<iframe src='https://www.youtube.com/embed/abc'></iframe>"})
    with pytest.raises(ExtractError, match="oynatıcı iframe'i bulunamadı"):
        DiziboxAdapter().build_stream(net, SESSION, ep, series)
