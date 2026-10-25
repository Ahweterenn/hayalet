"""--doctor: dizi bölüm listesi bozukken "sağlam" raporlamamalı.

Canlı yaşandı (2026-10): Dizipal bölüm linklerinin şemasını değiştirdi, her
dizi 0 bölüm döndürüyordu — ama doktor ilk adaya bakıyordu, o da bir FİLMDİ
(film tek sentetik bölümle her zaman "listelenir") ve her şeyi yeşil gösterdi.
Burada sahte bir site ile, ağsız sınanır.
"""
from types import SimpleNamespace

from hayalet import cli
from hayalet.core.models import Episode, Series

FILM = Series(name="Bir Film", slug="film/x", type="Movies", site="sahte")
DIZI = Series(name="Bir Dizi", slug="dizi/y", type="Series", site="sahte")


class _Adapter:
    def __init__(self, sonuc, bolumler):
        self._sonuc, self._bolumler = sonuc, bolumler

    def search(self, net, session, term):
        return list(self._sonuc)

    def get_episodes(self, net, session, series):
        return list(self._bolumler.get(series.slug, []))

    def build_stream(self, net, session, ep, series):
        return SimpleNamespace(video_master_url="https://cdn/x.m3u8")


def _calistir(monkeypatch, adapter):
    sess = SimpleNamespace(base_url="https://site")
    net = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(cli, "_connect",
                        lambda *a, **k: (net, sess, {"label": "test"}))
    args = SimpleNamespace(domain=None, site="sahte", no_cache=True,
                           cf_cookie=None, user_agent=None)
    return {ad: (ok, detay) for ad, ok, detay in cli._doctor_site(adapter, args, None)}


def _ep(n):
    return Episode(season=1, number=n, url=f"https://site/b{n}")


def test_broken_series_listing_is_red_even_if_a_movie_works(monkeypatch):
    # Film önce geliyor ve çalışıyor; dizi 0 bölüm. Eskiden tamamen yeşildi.
    ad = _Adapter([FILM, DIZI], {"film/x": [_ep(1)], "dizi/y": []})
    sonuc = _calistir(monkeypatch, ad)
    ok, detay = sonuc["Bölüm/kaynak listesi"]
    assert not ok
    assert "dizi" in detay.lower()


def test_healthy_series_is_green(monkeypatch):
    ad = _Adapter([FILM, DIZI], {"film/x": [_ep(1)], "dizi/y": [_ep(1), _ep(2)]})
    sonuc = _calistir(monkeypatch, ad)
    assert sonuc["Bölüm/kaynak listesi"][0]
    assert sonuc["Uçtan uca extract"][0]
    assert "Bir Dizi" in sonuc["Bölüm/kaynak listesi"][1]   # dizi üzerinden doğrulandı


def test_movie_only_site_still_green_and_says_so(monkeypatch):
    # hdfilmcehennemi gibi: aramada dizi çıkmazsa film yeterli, ama raporda belirtilir.
    ad = _Adapter([FILM], {"film/x": [_ep(1)]})
    sonuc = _calistir(monkeypatch, ad)
    ok, detay = sonuc["Bölüm/kaynak listesi"]
    assert ok
    assert "dizi" in detay.lower()
