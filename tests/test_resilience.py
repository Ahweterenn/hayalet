"""Erişim/teşhis katmanının saf-mantık testleri (ağsız).

Bu fonksiyonlar, "site mi bozuldu, biz mi" sorusunun yanlış cevaplanmasına yol
açan somut olaylardan doğdu (hepsi canlı doğrulandı):
  * resolver, HTTP 200 döndüğü için bir AYNA ön-yüzünü çalışan domain sanıp
    önbelleğe yazdı; arama JSON yerine anasayfa HTML'i döndürdü.
  * Cloudflare JS challenge'ı düz 403 gibi görünüp boşuna 3 kez denendi ve
    "TLS bloğu" gibi raporlandı.
  * --cf-cookie kullanıcıdan çok farklı biçimlerde gelebiliyor.
  * Site 1561'den 1574'e rollover yaptı; tarama penceresi 8 adresle sınırlı
    olduğu için domain tamamen ıskalandı ve araç "domain bulunamadı" dedi.
"""
from __future__ import annotations

import json

import pytest

from hayalet import config
from hayalet.cli import _parse_cf_cookie
from hayalet.core import resolver
from hayalet.core.network import _is_challenge
from hayalet.core.resolver import _mirror_origin
from hayalet.core.session import SessionState


class _Resp:
    """curl-cffi yanıtının _is_challenge'ın dokunduğu minimal taklidi."""

    def __init__(self, status_code=200, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text

    def json(self):
        return json.loads(self.text)


# --- ayna tespiti ---------------------------------------------------------
def test_mirror_origin_detects_front_page_mirror():
    html = '<script>(function(){var ORIGIN="https://dizipal1562.com";var MIRROR=location.origin;'
    assert _mirror_origin(html) == "https://dizipal1562.com"


def test_mirror_origin_ignores_real_site():
    assert _mirror_origin("<html><body>dizipal anasayfa</body></html>") is None


def test_mirror_origin_strips_trailing_slash():
    assert _mirror_origin("var ORIGIN='https://dizipal1562.com/'") == "https://dizipal1562.com"


# --- Cloudflare challenge tespiti ----------------------------------------
def test_challenge_detected_from_cf_mitigated_header():
    assert _is_challenge(_Resp(403, {"cf-mitigated": "challenge"}))


def test_challenge_detected_from_body_marker():
    assert _is_challenge(_Resp(403, {}, "<title>Just a moment...</title>"
                                       "<script>window._cf_chl_opt = {};</script>"))


def test_plain_403_is_not_a_challenge():
    """TLS parmak izi reddi challenge DEĞİL — retry'ı hak eder."""
    assert not _is_challenge(_Resp(403, {}, "<html>Forbidden</html>"))


def test_success_response_is_not_a_challenge():
    assert not _is_challenge(_Resp(200, {}, "<html>ok</html>"))


# --- cf_clearance cookie ayrıştırma --------------------------------------
def test_cf_cookie_bare_value_assumed_to_be_cf_clearance():
    assert _parse_cf_cookie("abc123") == {"cf_clearance": "abc123"}


def test_cf_cookie_named_form():
    assert _parse_cf_cookie("cf_clearance=abc123") == {"cf_clearance": "abc123"}


def test_cf_cookie_full_header_keeps_every_pair():
    assert _parse_cf_cookie("cf_clearance=abc; __cf_bm=xyz") == {
        "cf_clearance": "abc", "__cf_bm": "xyz"}


def test_cf_cookie_strips_surrounding_quotes():
    assert _parse_cf_cookie('"cf_clearance=abc123"') == {"cf_clearance": "abc123"}


def test_cf_cookie_empty_input_yields_nothing():
    assert _parse_cf_cookie("") == {}


# --- domain çözümleme (sahte internet, ağsız) ----------------------------
def _home(canonical: str | None = None, origin: str | None = None) -> str:
    """_looks_real + _search_works'ün aradığı işaretleri taşıyan anasayfa."""
    html = '<html>dizipal dizi film <input name="cValue" value="tok">'
    if canonical:
        html += f'<link rel="canonical" href="{canonical}"/>'
    if origin:
        html += f'<script>var ORIGIN="{origin}";</script>'
    return html + "</html>"


def _fake_net(pages: dict[str, str], search_ok: set[str]):
    """`pages`teki adresleri servis eden, gerisine 'erişilemez' diyen sahte ağ.

    `search_ok`: arama POST'una JSON dönen (yani GERÇEKTEN canlı) originler.
    Listede olmayan bir origin, aynaların yaptığı gibi anasayfa HTML'i döner.
    """
    class FakeNet:
        def __init__(self, session=None):
            self.session = session

        def get(self, url, referer=None, retries=None, timeout=None, **kw):
            html = pages.get(url.rstrip("/"))
            if html is None:
                raise ConnectionError(f"unreachable: {url}")
            return _Resp(200, {}, html)

        def post(self, url, referer=None, retries=None, timeout=None, **kw):
            origin = url.split(config.SEARCH_ENDPOINT)[0].rstrip("/")
            if origin in search_ok:
                return _Resp(200, {}, '{"data": []}')
            return _Resp(200, {}, _home())      # ayna: JSON yerine anasayfa

        def close(self):
            pass

    return FakeNet


@pytest.fixture
def world(monkeypatch):
    """resolve()'u sahte internete bağlar; önbellek/diske hiç dokunmaz."""
    monkeypatch.setattr(resolver, "_save", lambda domain: None)
    monkeypatch.setattr(resolver, "_cached", lambda: None)
    monkeypatch.setattr(config, "KNOWN_DOMAIN", "https://dizipal1561.com")

    def build(pages, search_ok):
        fake = _fake_net(pages, set(search_ok))
        monkeypatch.setattr(resolver, "Network", fake)   # _scan_reachable içindeki
        return fake(SessionState()), SessionState()

    return build


def test_resolve_finds_rollover_beyond_old_scan_window(world):
    """1561 -> 1574: eski 8'lik pencere bunu ıskalıyordu (canlı yaşandı)."""
    live = "https://dizipal1574.com"
    net, session = world({live: _home(canonical=live)}, {live})
    assert resolver.resolve(net, session, use_cache=False) == live
    assert session.base_url == live


def test_resolve_follows_canonical_from_stale_mirror(world):
    """Eski adres ayakta ama canonical'ı güncel domaini gösteriyor."""
    old, live = "https://dizipal1566.com", "https://dizipal1574.com"
    net, session = world(
        {old: _home(canonical=live), live: _home(canonical=live)}, {live})
    assert resolver.resolve(net, session, use_cache=False) == live


def test_resolve_rejects_reachable_domain_whose_search_is_dead(world):
    """200 dönmek yetmez: araması çalışmayan adres KABUL EDİLMEMELİ."""
    dead = "https://dizipal1566.com"
    net, session = world({dead: _home()}, search_ok=set())
    with pytest.raises(resolver.ResolverError) as e:
        resolver.resolve(net, session, use_cache=False)
    assert dead in str(e.value)          # kullanıcıya hangi adres elendi denir


def test_resolve_prefers_mirror_origin_over_mirror_itself(world):
    """Ayna ön-yüzü yerine işaret ettiği ASIL domain seçilmeli."""
    mirror, real = "https://dizipal1563.com", "https://dizipal1590.com"
    net, session = world(
        {mirror: _home(origin=real), real: _home()}, {real})
    assert resolver.resolve(net, session, use_cache=False) == real


def test_resolve_uses_cache_without_scanning(monkeypatch, world):
    """Önbellek isabetinde geniş tarama HİÇ çalışmamalı (sık yol tek istek)."""
    cached = "https://dizipal1574.com"
    net, session = world({cached: _home()}, {cached})
    monkeypatch.setattr(resolver, "_cached", lambda: cached)

    def boom(*a, **kw):
        raise AssertionError("önbellek tuttuğu hâlde tarama çalıştı")

    monkeypatch.setattr(resolver, "_scan_reachable", boom)
    assert resolver.resolve(net, session) == cached
