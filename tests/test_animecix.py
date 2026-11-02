"""animecix adapter'ı (ağsız): arama anime/film ayrımı, sınır-aramayla bölüm
sayımı, tau-video → düz MP4 (kind=file) zinciri, kaynak yoksa hata."""
from types import SimpleNamespace

import pytest

from hayalet.core.extractor import ExtractError
from hayalet.core.models import Episode, Series
from hayalet.sites import _tauvideo
from hayalet.sites.animecix_adapter import AnimecixAdapter

BASE = "https://animecix.tv"
SESSION = SimpleNamespace(base_url=BASE, referer=BASE)


class FakeNet:
    """URL -> python nesnesi (json) ya da str (text). Bilinmeyen URL = 404."""
    def __init__(self, answers):
        self.answers = answers

    def get(self, url, referer=None, **kw):
        if url not in self.answers:
            return SimpleNamespace(status_code=404, text="", json=lambda: {})
        v = self.answers[url]
        if isinstance(v, Exception):
            raise v
        if isinstance(v, str):
            return SimpleNamespace(status_code=200, text=v, json=lambda: {})
        return SimpleNamespace(status_code=200, text="", json=lambda v=v: v)


def _ep(tid, e, s):
    return f"{BASE}/secure/episode-videos?titleId={tid}&episode={e}&season={s}"


def test_arama_anime_film_ayirir():
    net = FakeNet({f"{BASE}/secure/search/x?limit=20": {"results": [
        {"id": 66, "name": "One Piece", "title_type": "anime", "year": 1999},
        {"id": 7, "name": "Film X", "title_type": "movie", "year": 2020},
        {"id": None, "name": "Boş"},            # id yok → atlanır
    ]}})
    res = AnimecixAdapter().search(net, SESSION, "x")
    assert [(s.name, s.slug, s.type) for s in res] == [
        ("One Piece", "t/66", "Dizi"),
        ("Film X", "t/7", "Film")]


def test_bolum_sayimi_sinir_aramasi():
    # S1: 1,2,3 dolu, 4 boş → 3 bölüm. S2: boş → dizi 3 bölümde biter.
    full = [{"type": "embed", "url": "https://tau-video.xyz/embed/x"}]
    ans = {_ep("100", e, 1): (full if e <= 3 else []) for e in (1, 2, 3, 4)}
    ans[_ep("100", 1, 2)] = []
    s = Series(name="A", slug="t/100", type="Dizi", site="animecix")
    eps = AnimecixAdapter().get_episodes(FakeNet(ans), SESSION, s)
    assert [(e.season, e.number) for e in eps] == [(1, 1), (1, 2), (1, 3)]


def test_film_tek_bolum():
    s = Series(name="Film X", slug="t/7", type="Film", site="animecix")
    eps = AnimecixAdapter().get_episodes(FakeNet({}), SESSION, s)
    assert len(eps) == 1 and eps[0].url == _ep("7", 1, 1)


def test_tauvideo_en_yuksek_kalite_secer():
    net = FakeNet({"https://tau-video.xyz/api/video/abc": {"urls": [
        {"label": "480p", "url": "https://cdn/480.mp4"},
        {"label": "1080p", "url": "https://cdn/1080.mp4"},
        {"label": "720p", "url": "https://cdn/720.mp4"},
    ]}})
    assert _tauvideo.resolve(net, "https://tau-video.xyz/embed/abc") == "https://cdn/1080.mp4"


def test_build_stream_tauvideo_duz_mp4():
    s = Series(name="A", slug="t/100", type="Dizi", site="animecix")
    ep = Episode(season=1, number=1, url=_ep("100", 1, 1))
    net = FakeNet({
        ep.url: [{"type": "embed", "name": "Tau Video",
                  "url": "https://tau-video.xyz/embed/abc"}],
        "https://tau-video.xyz/api/video/abc": {"urls": [
            {"label": "1080p", "url": "https://cdn/1080.mp4"}]},
    })
    st = AnimecixAdapter().build_stream(net, SESSION, ep, s)
    assert st.kind == "file"
    assert st.video_master_url == "https://cdn/1080.mp4"
    assert st.audios == [] and st.subtitle_url is None


def test_build_stream_desteklenmeyen_host_hata():
    s = Series(name="A", slug="t/100", type="Dizi", site="animecix")
    ep = Episode(season=1, number=1, url=_ep("100", 1, 1))
    net = FakeNet({ep.url: [{"type": "embed",
                             "url": "https://doodstream.com/e/zzz"}]})
    with pytest.raises(ExtractError, match="desteklenmeyen|tau-video"):
        AnimecixAdapter().build_stream(net, SESSION, ep, s)
