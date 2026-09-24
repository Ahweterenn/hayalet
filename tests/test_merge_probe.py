"""Kaynak sınaması: "çözülebildi" ile "segment veriyor" aynı şey değil.

Site oynatıcı sunucularını döndürüyor ve bazı örneklerinin segment CDN'i ölü
çıkıyor: playlist 200 geliyor, segment hiç cevap vermiyor. Oynatıcıya böyle bir
kaynak verilince süre çubuğu doluyor ama görüntü gelmiyor — 0:00'da donuyor ve
sebep hiçbir yerde görünmüyor (telefonda ölçüldü, 2026-09-23).

Buradaki testler ağa çıkmaz; sahte Network ile "o adres ne döndürüyor" senaryosu
kurulur.
"""
from types import SimpleNamespace

from hayalet.core import merge


class _Yanit:
    def __init__(self, text="", status=200, content=None, ctype=""):
        self.text = text
        self.status_code = status
        self.content = content if content is not None else text.encode()
        self.headers = {"content-type": ctype}


class _SahteNet:
    """`net.get(url, referer=..., headers=...)` sözleşmesini taklit eder."""

    def __init__(self, harita):
        self.harita = harita
        self.istekler = []

    def get(self, url, referer=None, retries=None, timeout=None, **kw):
        self.istekler.append(url)
        cevap = self.harita.get(url)
        if cevap is None:
            raise AssertionError("beklenmeyen adres: %s" % url)
        if isinstance(cevap, Exception):
            raise cevap
        return cevap


_MASTER = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nvaryant.m3u8\n"
_VARYANT = "#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXTINF:3.0,\nseg1.ts\n"


def test_canli_kaynak_segment_veriyor():
    net = _SahteNet({
        "https://k/master.m3u8": _Yanit(_MASTER),
        "https://k/varyant.m3u8": _Yanit(_VARYANT),
        "https://k/seg1.ts": _Yanit("", content=b"\x47" * 65536,
                                    ctype="video/mp2t"),
    })
    ok, neden = merge.probe_video(net, "https://k/master.m3u8", "https://k/")
    assert ok, neden
    assert "bayt" in neden


def test_olu_segment_cdn_yakalanir():
    """Gerçek vaka: playlist 200, segment CDN'den 522 (origin'e ulaşılamıyor)."""
    net = _SahteNet({
        "https://k/master.m3u8": _Yanit(_MASTER),
        "https://k/varyant.m3u8": _Yanit(_VARYANT),
        "https://k/seg1.ts": _Yanit("hata sayfasi", status=522),
    })
    ok, neden = merge.probe_video(net, "https://k/master.m3u8", "https://k/")
    assert not ok
    assert "522" in neden


def test_bos_segment_de_olu_sayilir():
    net = _SahteNet({
        "https://k/master.m3u8": _Yanit(_MASTER),
        "https://k/varyant.m3u8": _Yanit(_VARYANT),
        "https://k/seg1.ts": _Yanit("", content=b"", ctype="video/mp2t"),
    })
    ok, neden = merge.probe_video(net, "https://k/master.m3u8", "https://k/")
    assert not ok and "bayt" in neden


def test_hata_sayfasi_segment_sayilmaz():
    """Sınamanın ilk hâli `Range` ile gelen HTML hata sayfasını segment sandı:
    telefonda üç TLS profili de "ok, 4096 bayt" dedi ama gerçek zincir yine
    504 veriyordu. Tür ve boyut bakılmazsa ölü kaynak "canlı" görünüyor."""
    hata = b"<!DOCTYPE html><html><body>Cloudflare 522</body></html>" * 200
    net = _SahteNet({
        "https://k/master.m3u8": _Yanit(_MASTER),
        "https://k/varyant.m3u8": _Yanit(_VARYANT),
        "https://k/seg1.ts": _Yanit("", status=200, content=hata,
                                    ctype="text/html; charset=UTF-8"),
    })
    ok, neden = merge.probe_video(net, "https://k/master.m3u8", "https://k/")
    assert not ok and "HTML" in neden


def test_kucuk_govde_segment_sayilmaz():
    """Cloudflare'in 522 sayfası 7,3 KB geliyordu; gerçek segment yüz KB'lar."""
    net = _SahteNet({
        "https://k/master.m3u8": _Yanit(_MASTER),
        "https://k/varyant.m3u8": _Yanit(_VARYANT),
        "https://k/seg1.ts": _Yanit("", status=200, content=b"x" * 4096,
                                    ctype="video/mp2t"),
    })
    ok, neden = merge.probe_video(net, "https://k/master.m3u8", "https://k/")
    assert not ok and "küçük" in neden


def test_master_playlist_degilse_elenir():
    net = _SahteNet({"https://k/master.m3u8": _Yanit("<html>engellendi</html>")})
    ok, neden = merge.probe_video(net, "https://k/master.m3u8", "https://k/")
    assert not ok and "playlist değil" in neden


def test_segment_zaman_asimi_sebebiyle_atlanir():
    net = _SahteNet({
        "https://k/master.m3u8": _Yanit(_MASTER),
        "https://k/varyant.m3u8": _Yanit(_VARYANT),
        "https://k/seg1.ts": TimeoutError("cevap yok"),
    })
    ok, neden = merge.probe_video(net, "https://k/master.m3u8", "https://k/")
    assert not ok and "alınamadı" in neden


def _kaynak(ad):
    si = SimpleNamespace(m3u8_url="https://%s/master.m3u8" % ad,
                         referer="https://%s/iframe" % ad,
                         subtitle_url=None)
    return (si, si.m3u8_url, [])


def test_olu_orijinal_yerine_dublaj_videosu_kullanilir(monkeypatch):
    """Eskiden video HER ZAMAN orijinalden alınıyordu; orijinalin CDN'i ölüyse
    oynatma donuyordu, oysa dublaj sürümü çalışıyor olabilir."""
    orig, dub = _kaynak("olu"), _kaynak("canli")

    monkeypatch.setattr(merge.catalog, "is_dubbed", lambda s: False)
    monkeypatch.setattr(merge.catalog, "find_counterpart",
                        lambda *a, **k: SimpleNamespace(name="X Türkçe Dublaj"))
    monkeypatch.setattr(merge, "_counterpart_episode",
                        lambda *a, **k: SimpleNamespace(url="https://site/dub-bolum"))
    monkeypatch.setattr(merge, "_resolve_both", lambda *a, **k: (orig, dub))
    monkeypatch.setattr(merge, "probe_video",
                        lambda net, url, ref, timeout=8:
                        (False, "522") if "olu" in url else (True, "4096 bayt"))

    ms = merge.build_merged(None, None,
                            SimpleNamespace(url="https://site/bolum", season=1, number=1),
                            SimpleNamespace(name="X"))
    assert ms.video_master_url == "https://canli/master.m3u8"


def test_iki_kaynak_da_olu_ise_secim_degismez(monkeypatch):
    """Hiçbiri vermiyorsa davranış eskisi gibi kalsın — sessizce kaynak
    değiştirip kullanıcıyı yanıltmayalım."""
    orig, dub = _kaynak("olu1"), _kaynak("olu2")
    monkeypatch.setattr(merge.catalog, "is_dubbed", lambda s: False)
    monkeypatch.setattr(merge.catalog, "find_counterpart",
                        lambda *a, **k: SimpleNamespace(name="X Türkçe Dublaj"))
    monkeypatch.setattr(merge, "_counterpart_episode",
                        lambda *a, **k: SimpleNamespace(url="https://site/dub-bolum"))
    monkeypatch.setattr(merge, "_resolve_both", lambda *a, **k: (orig, dub))
    monkeypatch.setattr(merge, "probe_video",
                        lambda net, url, ref, timeout=8: (False, "504"))
    ms = merge.build_merged(None, None,
                            SimpleNamespace(url="https://site/bolum", season=1, number=1),
                            SimpleNamespace(name="X"))
    assert ms.video_master_url == "https://olu1/master.m3u8"
