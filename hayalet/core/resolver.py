"""Dynamic Domain Resolver — güncel dizipalXXXX.com adresini bulur.

Sıra: önbellek -> bilinen domain -> artan sayı taraması. Bulunan domain
SessionState.base_url'e yazılır ve .cache/domain.json'a kaydedilir.
"""
from __future__ import annotations

import concurrent.futures as futures
import json
import re
import time
from dataclasses import replace
from pathlib import Path

from hayalet import config
from hayalet.core.network import Network
from hayalet.core.session import SessionState

_CACHE = config.CACHE_DIR / "domain.json"
_CACHE_TTL = 6 * 3600   # 6 saat

# KNOWN_DOMAIN'den itibaren kaç adres taranacak. Geniş tutuluyor: gözlenen bir
# rollover'da site 1561'den 1574'e atladı (13 adım) ve dar aralık (8) domaini
# tamamen ıskaladı — kullanıcıya "domain bulunamadı" diye düşüyordu. Aralığı
# genişletmenin bedeli _scan_reachable'ın paralel + tek denemelik olmasıyla
# ödendiği için burayı cimri tutmanın bir kazancı yok.
_SCAN = 24
_SCAN_WORKERS = 12
_SCAN_TIMEOUT = 8     # sn; tarama yoklaması için (normal istekler 20 sn'de kalır)


class ResolverError(Exception):
    """Hiçbir domain adayı doğrulanamadı — kullanıcı --domain ile elle vermeli."""


def _looks_real(html: str) -> bool:
    low = html.lower()
    return "dizipal" in low and any(w in low for w in ("dizi", "bolum", "film"))


def _canonical(html: str) -> str | None:
    """Sayfanın kendi işaret ettiği güncel domaini döndürür.

    Eski bir ayna (ör. dizipal1560) hâlâ ayakta olabilir ama servis ettiği
    sayfanın canonical/og:url'i güncel domaine (dizipal1561) işaret eder —
    aramanın çalıştığı gerçek domain budur. Bu sinyali takip etmek, resolver'ın
    'erişilebilir ama arama backend'i ölü' bir aynada takılmasını önler.
    """
    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
                  html, re.I) or \
        re.search(r'property=["\']og:url["\']\s+content=["\']([^"\']+)["\']', html, re.I)
    if m:
        cm = re.match(r"https?://dizipal\d+\.com", m.group(1).rstrip("/"))
        if cm:
            return cm.group(0)
    return None


def _mirror_origin(html: str) -> str | None:
    """Sayfa bir AYNA/ön-yüz mü? Öyleyse işaret ettiği asıl domaini döndürür.

    Erişim engeli olan bölgelerde asıl domainin önüne, tüm istekleri asıl siteye
    taşıyan aynalar konuyor. Bu sayfalar kendi JS'lerinde asıl domaini
    `var ORIGIN="https://dizipalNNNN.com"` olarak taşır. Sorun şu ki bu aynaların
    bir kısmı SADECE anasayfayı servis ediyor (her yola aynı HTML, arama POST'una
    JSON yerine anasayfa) — yani HTTP 200 verir, "gerçek dizipal sayfası"na
    benzer, ama üzerinde hiçbir şey çalışmaz. Bu yüzden 200 + benzerlik yetmez.
    """
    m = re.search(r'\bORIGIN\s*=\s*["\'](https?://dizipal\d+\.com)/?["\']', html)
    return m.group(1).rstrip("/") if m else None


def _search_works(net: Network, url: str, html: str) -> bool:
    """Domain'i kabul etmeden önce ARAMANIN GERÇEKTEN çalıştığını doğrular.

    Tek gerçek kriter bu: anasayfanın 200 dönmesi bir şey ifade etmiyor (ayna ve
    park sayfaları da 200 döner). Arama endpoint'i JSON döndürüyorsa domain
    canlıdır; HTML döndürüyorsa (ayna anasayfayı geri veriyor) ölüdür.
    """
    m = re.search(r'name="cValue"\s+value="([^"]+)"', html)
    if not m:
        return False                     # arama formu yoksa katalog gezilemez
    payload = {"searchterm": "a", "cValue": m.group(1)}
    k = re.search(r'name="cKey"\s+value="([^"]+)"', html)
    if k:
        payload["cKey"] = k.group(1)
    try:
        r = net.post(url + config.SEARCH_ENDPOINT, data=payload, referer=url,
                     headers={"X-Requested-With": "XMLHttpRequest"})
        r.json()
    except Exception:
        return False
    return True


def _fetch(net: Network, url: str, retries: int | None = None,
           timeout: int | None = None) -> str | None:
    """Domain gerçek bir dizipal anasayfası servis ediyorsa HTML'i, değilse None."""
    try:
        r = net.get(url, referer=url, retries=retries, timeout=timeout)
        if r.status_code == 200 and _looks_real(r.text):
            return r.text
    except Exception:          # BlockedError dahil: aday elenir, tarama sürer
        pass
    return None


def _scan_reachable(session: SessionState, urls: list[str]) -> dict[str, str]:
    """Aday adresleri PARALEL yoklar; ulaşılabilenlerin HTML'ini döndürür.

    İki nokta önemli:
      * Her thread KENDİ Network'ünü (dolayısıyla kendi curl-cffi Session'ını)
        kullanır — tek bir client'ı thread'ler arasında paylaşmak güvenli değil.
        Kimlik (UA/impersonate/proxy) korunsun ama ölü adaylardan gelen
        cookie'ler paylaşılan SessionState'e sızmasın diye kopya state veriliyor.
      * Tek denemelik ve kısa timeout'lu: var olmayan bir domaini 3 kez, 20 sn
        bekleyerek denemek aday başına saniyeler yakıyor ve hiçbir şey
        kazandırmıyor — anasayfasını _SCAN_TIMEOUT içinde veremeyen bir adres
        zaten aradığımız domain değil.
    """
    def probe(url: str) -> tuple[str, str | None]:
        scratch = replace(session, cookies=dict(session.cookies))
        net = Network(scratch)
        try:
            return url, _fetch(net, url, retries=1, timeout=_SCAN_TIMEOUT)
        finally:
            net.close()

    hits: dict[str, str] = {}
    if not urls:
        return hits
    with futures.ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as ex:
        for url, html in ex.map(probe, urls):
            if html is not None:
                hits[url] = html
    return hits


def _follow_canonical(net: Network, url: str, html: str,
                      max_hops: int = 5) -> tuple[str, str]:
    """Sayfanın canonical'ı daha güncel bir domaine işaret ettiği sürece oraya
    atlar (ör. 1560 -> 1561 -> 1562). Zincir kilitlenmesin diye adım sayısı sınırlı
    ve ziyaret edilenler takip edilir; ulaşılabilir son domaini VE onun HTML'ini
    döndürür — HTML'i de vermesi, çağıranın aynı sayfayı (yüz KB'lar) ikinci kez
    indirmesini önlüyor."""
    visited = {url}
    for _ in range(max_hops):
        canon = _canonical(html)
        if not canon or canon == url or canon in visited:
            break
        nxt = _fetch(net, canon)
        if nxt is None:          # canonical erişilemiyorsa mevcut domainde kal
            break
        url, html = canon, nxt   # güncel domaine geç, onun da canonical'ına bak
        visited.add(url)
    return url, html


def _cached() -> str | None:
    try:
        d = json.loads(_CACHE.read_text(encoding="utf-8"))
        if time.time() - d.get("ts", 0) < _CACHE_TTL:
            return d.get("domain")
    except Exception:
        return None
    return None


def _save(domain: str) -> None:
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps({"domain": domain, "ts": time.time()}),
                          encoding="utf-8")
    except Exception:
        pass


def resolve(net: Network, session: SessionState, override: str | None = None,
            use_cache: bool = True, scan: int = _SCAN) -> str:
    # Öncelikli adaylar: normal koşulda isabet burada olur ve tek istek yeter,
    # o yüzden sırayla ve tam retry ile denenirler.
    priority: list[str] = []
    pinned = override.rstrip("/") if override else None
    if pinned:
        priority.append(pinned)
    if use_cache:
        c = _cached()
        if c:
            priority.append(c)
    priority.append(config.KNOWN_DOMAIN)
    priority = list(dict.fromkeys(priority))     # tekrarları at (cache == known)

    # Artan sayı taraması: dizipalNNNN -> NNNN+scan
    scan_urls: list[str] = []
    m = re.search(r"(dizipal)(\d+)(\.com)", config.KNOWN_DOMAIN)
    if m:
        base_n = int(m.group(2))
        scan_urls = [f"https://{m.group(1)}{n}{m.group(3)}"
                     for n in range(base_n, base_n + scan + 1)]

    seen: set[str] = set()
    mirrors: list[str] = []          # ulaşıldı ama üzerinde arama çalışmıyor

    def candidates():
        """(url, html) çiftlerini TEMBEL üretir.

        Tembellik önemli: öncelikli adaylardan biri tutarsa geniş tarama hiç
        çalışmaz — yani sık yol (önbellek isabeti) tek istekte biter, pahalı
        tarama yalnızca gerçekten gerektiğinde devreye girer.
        """
        for url in priority:
            if url == pinned:
                # --domain ile ELLE pinlenen adres tam retry hak eder: geçici
                # bir hıçkırık yüzünden atlanıp başka bir domaine kaymamalı.
                yield url, _fetch(net, url)
            else:
                # Önbellek / KNOWN_DOMAIN yalnızca HIZLI YOL. Iskalanırlarsa
                # kayıp yok: tarama aralığı zaten KNOWN_DOMAIN'in numarasından
                # başlıyor, ikisi de aşağıda tekrar yoklanıyor. Buna karşılık
                # rollover sonrası ölü bir önbellek kaydını 3 kez, 20 sn
                # timeout'la denemek tek başına ~30 sn yakıyordu.
                yield url, _fetch(net, url, retries=1, timeout=_SCAN_TIMEOUT)
        rest = [u for u in scan_urls if u not in seen]
        hits = _scan_reachable(session, rest)
        for url in rest:
            if url in hits:
                yield url, hits[url]

    for url, html in candidates():
        if url in seen:
            continue
        seen.add(url)
        if html is None:
            continue
        # Sayfa güncel bir domaine işaret ediyorsa oraya atla (rollover takibi):
        # eski ayna ayakta olsa da aramanın çalıştığı gerçek domaini kullanırız.
        # Çok adımlı takip: 1560 -> 1561 -> ... zincirini sonuna kadar izler.
        url, html = _follow_canonical(net, url, html)
        seen.add(url)   # canonical'ın götürdüğü domain aday listesinde de olabilir

        # Ayna ön-yüzü: asıl domaini deneyip ORAYI doğrulamayı tercih et.
        origin = _mirror_origin(html)
        if origin and origin != url and origin not in seen:
            seen.add(origin)
            ohtml = _fetch(net, origin)
            if ohtml is not None and _search_works(net, origin, ohtml):
                url, html = origin, ohtml

        # Asıl kapı: 200 değil, ARAMA çalışıyor mu? (bkz. _search_works)
        if not _search_works(net, url, html):
            mirrors.append(url + (f" (ayna → {origin})" if origin else ""))
            continue

        session.base_url = url
        session.referer = url
        _save(url)   # cache'i her zaman güncel domaine yazar (eski değeri ezer)
        return url

    detail = ""
    if mirrors:
        detail = (" Erişilen ama ARAMASI ÇALIŞMAYAN adresler (ayna/park sayfası): "
                  + ", ".join(mirrors) + ".")
    # seen yalnızca ULAŞILABİLEN adayları içerir (tarama, cevap vermeyenleri
    # döngüye hiç sokmuyor) — kullanıcıya gerçekten denenen sayıyı vermeliyiz.
    tried = len(seen | set(priority) | set(scan_urls))
    raise ResolverError(
        f"Güncel dizipal domaini bulunamadı ({tried} aday denendi).{detail} "
        f"Asıl domainler bölgesel olarak engellenmiş olabilir — VPN ya da --tor ile "
        f"dene, veya çalışan adresi elle ver: --domain https://dizipalXXXX.com"
    )
