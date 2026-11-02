"""Dizipal'in Next.js'e geçişi (2026-11): sayfa verisi, oynatıcı ayarı, arama.

Ağsız; biçimler canlı sayfalardan alındı.
"""
import base64
import hashlib
import json
from types import SimpleNamespace

from Crypto.Cipher import AES

from hayalet.core import catalog, extractor, resolver
from hayalet.core.models import Series


def _flight(text: str) -> str:
    return "<script>self.__next_f.push([1,%s])</script>" % json.dumps(text)


def _enc(plain: str, key: str) -> dict:
    salt, iv = b"s" * 16, b"i" * 16
    k = hashlib.pbkdf2_hmac("sha512", key.encode(), salt, 999, 32)
    pad = 16 - len(plain) % 16
    ct = AES.new(k, AES.MODE_CBC, iv).encrypt(plain.encode() + bytes([pad]) * pad)
    return {"ciphertext": base64.b64encode(ct).decode(),
            "iv": iv.hex(), "salt": salt.hex()}


class _Net:
    def __init__(self, pages=None, post=None):
        self.pages, self.post_body = pages or {}, post

    def get(self, url, **kw):
        return SimpleNamespace(text=self.pages[url])

    def post(self, url, **kw):
        return SimpleNamespace(json=lambda: self.post_body)


_S = SimpleNamespace(base_url="https://d", cookies={}, referer="https://d")


def test_oynatici_zinciri_kaynak_altyazi_ve_dublaj_sayfasini_verir():
    player = {"enc": _enc("//p/iframe.php?v=1", "gizli"), "k": "gizli", "sources": [
        {"href": "/dizi/x/1-sezon/1-bolum", "on": True, "dub": False},
        {"href": "/dizi/x/1-sezon/1-bolum?player=a", "on": False, "dub": True}]}
    # Parçanın ortadan bölünmesi: nesne ancak birleştirilince okunur.
    data = 'x"player":' + json.dumps(player)
    page = _flight(data[:20]) + _flight(data[20:])
    cfg = {"sources": [{"master": "https://p/nm.php?v=m"}], "subtitles": [
        {"lang": "en", "url": "https://p/en"}, {"lang": "tr", "url": "https://p/tr"}]}
    net = _Net({"https://d/b": page,
                "https://p/iframe.php?v=1":
                    "<script>window.NP_CONFIG = %s;</script>" % json.dumps(cfg)})
    si = extractor.extract_stream(net, _S, "https://d/b")
    assert si.m3u8_url == "https://p/nm.php?v=m"
    assert si.subtitle_url == "https://p/tr"
    assert si.dub_url == "https://d/dizi/x/1-sezon/1-bolum?player=a"


def test_bolumler_sayfa_verisinden_yalniz_bu_diziden_ve_teksiz_gelir():
    page = ('<a href="/dizi/x/2-sezon/1-bolum">'
            + _flight('"/dizi/x/1-sezon/1-bolum" "http://d/dizi/x/1-sezon/1-bolum" '
                      '"/dizi/x/1-sezon/1-bolum?lang=dublaj" "/dizi/baska/1-sezon/5-bolum"'))
    net = _Net({"https://d/dizi/x-dizi-izle": page})
    eps = catalog.get_episodes(net, _S, Series(name="X", slug="dizi/x-dizi-izle"))
    assert [(e.season, e.number, e.url) for e in eps] == [
        (1, 1, "https://d/dizi/x/1-sezon/1-bolum"),
        (2, 1, "https://d/dizi/x/2-sezon/1-bolum")]


def test_arama_oyuncu_kayitlarini_atar_film_dizi_ayrimini_korur():
    net = _Net(post={"ok": True, "result": [
        {"used_type": "Series", "used_slug": "dizi/a-dizi-izle", "object_name": "A"},
        {"used_type": "MovieSeries", "used_slug": "film/b-film-izle", "object_name": "B"},
        {"used_type": "Cast", "used_slug": "oyuncu/c", "object_name": "C"}]})
    got = catalog.search(net, _S, "abc")
    assert [r.name for r in got] == ["A", "B"]
    assert catalog.is_movie(got[1]) and not catalog.is_movie(got[0])


def test_kisa_sorgu_reddi_bos_sonuctur_eski_yola_dusmez():
    assert catalog.search(_Net(post={"ok": False, "code": 400}), _S, "a") == []


def test_ayna_html_donerse_alan_adi_kabul_edilmez():
    def bozuk():
        raise ValueError("JSON değil")
    net = SimpleNamespace(post=lambda *a, **k: SimpleNamespace(json=bozuk))
    assert resolver._search_works(net, "https://d", "<html>form yok</html>") is False
    assert resolver._search_works(_Net(post={"ok": True}), "https://d", "<html></html>") is True
