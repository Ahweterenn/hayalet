"""m3u8 ayrıştırma saf-mantık testleri (ağsız — sahte Network ile).

En kritik garanti: list_variants HER ZAMAN en yüksek kaliteyi ilk sıraya koymalı.
actions.download bu sıralamaya güveniyor (ffmpeg'in kendi varsayılan varyant
seçimi bazı sitelerde EN DÜŞÜK kaliteyi seçtiğinden — bkz. CLAUDE.md).
"""
from __future__ import annotations

from hayalet.core.m3u8_parser import get_av_urls, list_variants

_MASTER_URL = "https://cdn.test/master.m3u8"

_MASTER_TEXT = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="Turkce",LANGUAGE="tr",DEFAULT=YES,AUTOSELECT=YES,URI="audio_tr.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",LANGUAGE="en",DEFAULT=NO,AUTOSELECT=YES,URI="audio_en.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=1280x720,AUDIO="aud"
video_720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080,AUDIO="aud"
video_1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=640x360,AUDIO="aud"
video_360.m3u8
"""

_MEDIA_ONLY_TEXT = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
seg1.ts
#EXTINF:10.0,
seg2.ts
#EXT-X-ENDLIST
"""


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text


class _FakeNetwork:
    """net.get(url, referer=...).text sözleşmesini taklit eder — gerçek HTTP yok."""

    def __init__(self, mapping: dict[str, str]):
        self._mapping = mapping

    def get(self, url: str, referer: str | None = None, **kwargs):
        return _FakeResponse(self._mapping[url])


# --- list_variants ------------------------------------------------------------
def test_list_variants_sorted_best_first():
    net = _FakeNetwork({_MASTER_URL: _MASTER_TEXT})
    variants = list_variants(net, session=None, m3u8_url=_MASTER_URL, referer="r")
    assert [v.height for v in variants] == [1080, 720, 360]
    assert variants[0].bandwidth == 3_000_000


def test_list_variants_resolves_relative_uris():
    net = _FakeNetwork({_MASTER_URL: _MASTER_TEXT})
    variants = list_variants(net, session=None, m3u8_url=_MASTER_URL, referer="r")
    best = variants[0]
    assert best.url == "https://cdn.test/video_1080.m3u8"


def test_list_variants_empty_for_media_playlist():
    # Tek kaliteli (media) playlist'te #EXT-X-STREAM-INF yok -> boş liste döner
    # (çağıran bunu "tek varyant, seçime gerek yok" olarak yorumlar).
    net = _FakeNetwork({_MASTER_URL: _MEDIA_ONLY_TEXT})
    assert list_variants(net, session=None, m3u8_url=_MASTER_URL, referer="r") == []


# --- get_av_urls ---------------------------------------------------------------
def test_get_av_urls_picks_best_video():
    net = _FakeNetwork({_MASTER_URL: _MASTER_TEXT})
    video_url, _ = get_av_urls(net, session=None, master_url=_MASTER_URL,
                               referer="r", quality="best")
    assert video_url == "https://cdn.test/video_1080.m3u8"


def test_get_av_urls_turkish_audio_first():
    net = _FakeNetwork({_MASTER_URL: _MASTER_TEXT})
    _, tracks = get_av_urls(net, session=None, master_url=_MASTER_URL, referer="r")
    assert len(tracks) == 2
    assert tracks[0].is_turkish is True
    assert tracks[0].name == "Turkce"
    assert tracks[1].is_turkish is False


def test_get_av_urls_falls_back_to_master_when_no_variants():
    net = _FakeNetwork({_MASTER_URL: _MEDIA_ONLY_TEXT})
    video_url, tracks = get_av_urls(net, session=None, master_url=_MASTER_URL, referer="r")
    assert video_url == _MASTER_URL
    assert tracks == []


def test_get_av_urls_specific_quality():
    net = _FakeNetwork({_MASTER_URL: _MASTER_TEXT})
    video_url, _ = get_av_urls(net, session=None, master_url=_MASTER_URL,
                               referer="r", quality="720")
    assert video_url == "https://cdn.test/video_720.m3u8"
