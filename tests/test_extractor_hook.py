"""source2.php adımını gerçek tarayıcıya devretme kancası.

Neden bu kanca var: bu tek istek oynatılacak CDN düğümünü seçiyor ve Android
arm64'te curl-cffi ile istenince ölü bir adres dönüyor (telefonda ölçüldü:
segmentler 504/522; aynı zincir PC'de istendiğinde dönen adres telefonun
proxy'sinden bile 200 veriyor). Eski birlikte izleme sayfasının çalışan Node sürümü de bu adımı
taklitle değil gerçek tarayıcıyla yapıyor.

Testler ağa çıkmaz: sahte Network + sahte kanca ile hangi yolun seçildiğine
bakılır. Asıl kural: kanca BAŞARISIZSA eski davranış aynen sürmeli — yoksa
masaüstü sürümünü ve telefonun yedek yolunu kırarız.
"""
import json

import pytest

from hayalet.core import extractor


class _Yanit:
    def __init__(self, govde):
        self.text = govde

    def json(self):
        return json.loads(self.text)


class _SahteNet:
    def __init__(self, govde):
        self._govde = govde
        self.istekler = []

    def get(self, url, referer=None, headers=None, **kw):
        self.istekler.append(url)
        return _Yanit(self._govde)


_CURL = json.dumps({"playlist": [{"sources": [{"file": "https://olu/m.php"}]}]})
_TARAYICI = json.dumps({"playlist": [{"sources": [{"file": "https://canli/m.php"}]}]})


@pytest.fixture(autouse=True)
def _kancayi_temizle():
    # Kanca modül düzeyinde: bir test onu kurup bırakırsa ötekiler kirlenir.
    onceki = extractor.browser_fetch
    yield
    extractor.browser_fetch = onceki


def _cagir(net):
    return extractor._source2_json(net, "https://p/source2.php?v=T",
                                   "https://p/iframe")


def test_kanca_yoksa_curl_kullanilir():
    net = _SahteNet(_CURL)
    assert _cagir(net)["playlist"][0]["sources"][0]["file"] == "https://olu/m.php"
    assert net.istekler == ["https://p/source2.php?v=T"]


def test_kanca_varsa_tarayici_yaniti_kazanir():
    net = _SahteNet(_CURL)
    cagrilar = []

    def kanca(page_url, fetch_url):
        cagrilar.append((page_url, fetch_url))
        return _TARAYICI

    extractor.browser_fetch = kanca
    assert _cagir(net)["playlist"][0]["sources"][0]["file"] == "https://canli/m.php"
    # Tarayıcı cevap verdiyse yukarı akışa İKİNCİ bir istek gitmemeli.
    assert net.istekler == []
    assert cagrilar == [("https://p/iframe", "https://p/source2.php?v=T")]


@pytest.mark.parametrize("donen", [None, "", "<html>engellendi</html>"])
def test_kanca_basarisizsa_curl_yedegine_dusulur(donen):
    """Zaman aşımı, boş gövde ve HTML hata sayfası — üçünde de akış sürmeli."""
    net = _SahteNet(_CURL)
    extractor.browser_fetch = lambda p, f: donen
    assert _cagir(net)["playlist"][0]["sources"][0]["file"] == "https://olu/m.php"
    assert net.istekler == ["https://p/source2.php?v=T"]


def test_kanca_patlarsa_akis_kirilmaz():
    net = _SahteNet(_CURL)

    def kanca(page_url, fetch_url):
        raise RuntimeError("WebView yok")

    extractor.browser_fetch = kanca
    assert _cagir(net)["playlist"][0]["sources"][0]["file"] == "https://olu/m.php"
