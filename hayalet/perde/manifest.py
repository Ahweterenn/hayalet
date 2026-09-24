"""m3u8 manifest'ini kendi proxy'mize göre yeniden yazar + önbellek.

Saf metin işi — ağ yok, pytest ile denenebilir. Perde'nin Node sürümündeki
`rewriteManifestBody` / `makeBuildProxyUrl` / `manifestTtlMs` karşılığı.

Neden gerekli: tarayıcı segmentleri doğrudan CDN'den çekemez (CORS + çoğu
kaynakta Referer zorunlu — ölçüldü: referersiz Dizipal 403, hdfilmcehennemi
404 veriyor). Bu yüzden manifest'teki her adres bizim üstümüze yönlendirilir.
"""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import (parse_qsl, quote, unquote, urlencode, urljoin,
                          urlparse, urlunparse)

_URI_ATTR = re.compile(r'URI=("([^"]+)"|\'([^\']+)\')', re.I)


def to_absolute(raw: str, base_url: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if re.match(r"^https?://", raw, re.I):
        return raw
    return urljoin(base_url, raw)


def inject_master_params(url: str, base_url: str, master_params: list) -> str:
    """Master URL'in query parametrelerini alt adreslere taşır.

    Bazı CDN'ler yetkilendirme belirtecini master'ın query'sinde veriyor ve
    alt playlist/segment adreslerinde tekrarlamıyor; taşımazsak 403 geliyor.
    Var olan parametrenin üstüne YAZILMAZ.

    Yalnız AYNI hosttaki adreslere taşınır: belirteç o CDN'e ait, başka bir
    hosta gitmemeli. Ölçüldü (2026-09-24, Dizipal Behzat Ç.): playlist
    `org.dplayer82.site/l.php?v=<~2 KB>`, segmentler `lkm-cahiu1.*.cfd`'de;
    `v` segment adresine eklenince segment CDN'i 522 döndürüyor, ham adres
    200. Odada video 0:00'da kalıyordu, uygulamanın kendi oynatıcısı ise
    (bu taşımayı yapmadığı için) aynı bölümü oynatıyordu.
    """
    absolute = to_absolute(url, base_url)
    if not absolute or not master_params:
        return absolute or url
    try:
        p = urlparse(absolute)
        if p.netloc.lower() != urlparse(base_url).netloc.lower():
            return absolute
        existing = dict(parse_qsl(p.query, keep_blank_values=True))
        for k, v in master_params:
            existing.setdefault(k, v)
        return urlunparse(p._replace(query=urlencode(existing)))
    except Exception:
        return absolute


def make_proxy_url_builder(target_url: str, referer: str | None,
                           room_id: str, proxy_base: str = "/api/proxy"):
    """Verilen manifest için "alt adresi proxy adresine çevir" fonksiyonu."""
    master_params = parse_qsl(urlparse(target_url).query, keep_blank_values=True)

    def build(raw_uri: str) -> str:
        absolute = inject_master_params(raw_uri, target_url, master_params)
        # Çift kodlamayı önle: adres zaten kodlanmışsa bir kez çöz.
        try:
            decoded = unquote(absolute)
            if urlparse(decoded).scheme in ("http", "https"):
                absolute = decoded
        except Exception:
            pass
        return (f"{proxy_base}?url={quote(absolute, safe='')}"
                f"&ref={quote(referer or '', safe='')}"
                f"&roomId={quote(room_id, safe='')}")

    return build


def rewrite_body(body: str, build) -> str:
    """Manifest gövdesindeki her adresi proxy'ye yönlendirir.

    Yorum satırlarında (# ile başlayan) yalnızca URI="..." niteliği değişir —
    altyazı, şifre anahtarı ve ses kanalı adresleri oradadır.
    """
    out = []
    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if "URI=" in stripped:
                out.append(_rewrite_uri_attrs(line, build))
            else:
                out.append(line)
        else:
            out.append(build(stripped))
    return "\n".join(out)


def _rewrite_uri_attrs(line: str, build) -> str:
    def repl(m):
        value = m.group(2) or m.group(3) or ""
        if not value:
            return m.group(0)
        q = "'" if m.group(1).startswith("'") else '"'
        return f"URI={q}{build(value)}{q}"
    return _URI_ATTR.sub(repl, line)


def ttl_seconds(body: str) -> float:
    """Manifest türüne göre önbellek ömrü.

    Master (#EXT-X-STREAM-INF) durağandır, uzun tutulur — sık yeniden çekmek
    bazı CDN'lerde bağlantı sıfırlanmasına yol açıyor. Canlı yayın playlist'i
    sürekli değiştiği için çok kısa.
    """
    if re.search(r"#EXT-X-STREAM-INF", body, re.I):
        return 300.0
    if "#EXT-X-ENDLIST" in body:
        return 120.0
    return 4.0


class ManifestCache:
    """Süreli önbellek + "bayat" yedek.

    Bayat kopya neden var: kaynak CDN'ler manifest isteğini ara sıra sebepsiz
    RESET ediyor (kısa ömürlü linkler). Elde son iyi kopya varsa oynatma
    kırılmasın diye o sunulur.
    """

    def __init__(self, limit: int = 120):
        self._items: dict[str, tuple[str, float]] = {}
        self._limit = limit
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            hit = self._items.get(key)
            if not hit:
                return None
            body, expires = hit
            return body if expires > time.time() else None

    def get_stale(self, key: str) -> str | None:
        with self._lock:
            hit = self._items.get(key)
            return hit[0] if hit else None

    def put(self, key: str, body: str, ttl: float) -> None:
        with self._lock:
            if len(self._items) >= self._limit:
                oldest = next(iter(self._items), None)
                if oldest is not None:
                    self._items.pop(oldest, None)
            self._items[key] = (body, time.time() + ttl)


def looks_like_manifest(text: str) -> bool:
    """Bazı CDN'ler manifest'i text/plain ya da octet-stream ile sunuyor;
    içeriğe bakmazsak yeniden yazılmadan geçer ve oynatma sessizce ölür."""
    t = text.lstrip()[:400]
    return t.startswith("#EXTM3U") or ("#EXTINF" in t and "." in t)


def to_webvtt(text: str) -> str:
    """SRT'yi ve başlıksız VTT'yi tarayıcının kabul ettiği biçime getirir."""
    stripped = text.lstrip()
    if stripped.startswith("WEBVTT"):
        return text
    if "-->" in text:
        # SRT zaman damgası virgül kullanır, VTT nokta ister.
        text = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text)
    return "WEBVTT\n\n" + text


class SegmentCache:
    """Segment (bayt) önbelleği + **tek uçuş**.

    Neden gerekti: odadaki HER izleyici aynı segmenti telefonun proxy'sinden
    istiyor ve her istek AYRI bir yukarı akış indirmesi başlatıyordu. Üç
    izleyici = telefonun aynı ~1 MB'ı üç kez indirmesi; mobil veride bu hem
    bant genişliğini hem CPU'yu üçe katlıyor ve iş parçacıkları birikiyor
    (ölçüldü: iki misafirle sunucu yeni HTTP isteklerine hiç cevap veremez
    hâle geldi — telefonun kendi içinden bile).

    İzleyiciler senkron olduğu için aynı segmenti saniyeler içinde istiyorlar,
    yani önbellek isabeti kuraldışı değil KURAL. `lease` ile de aynı anda
    gelen ikinci istek ikinci indirmeyi başlatmaz, ilkinin bitmesini bekler.

    Bellek: toplam ve parça başına sınır var; segment MB'larca olabiliyor,
    telefonda sınırsız biriktirmek uygulamayı şişirir.
    """

    def __init__(self, max_bytes: int = 32 * 1024 * 1024,
                 max_item: int = 8 * 1024 * 1024, ttl: float = 90.0):
        self.max_item = max_item
        self._max_bytes = max_bytes
        self._ttl = ttl
        # değer: (gövde, content-type, son kullanma)
        self._items: dict[str, tuple[bytes, str, float]] = {}
        self._total = 0
        self._leases: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # --- önbellek --------------------------------------------------------
    def get(self, key: str) -> tuple[bytes, str] | None:
        """(gövde, content-type) ya da None. Tür de saklanıyor: segmentin
        gerçek türünü kaybedersek tarayıcı .jpg kılığındaki videoyu resim
        sanıyor."""
        with self._lock:
            hit = self._items.get(key)
            if not hit:
                return None
            data, ctype, expires = hit
            if expires <= time.time():
                self._items.pop(key, None)
                self._total -= len(data)
                return None
            return data, ctype

    def put(self, key: str, data: bytes, ctype: str = "") -> None:
        if not data or len(data) > self.max_item:
            return
        with self._lock:
            eski = self._items.pop(key, None)
            if eski:
                self._total -= len(eski[0])
            # Yer açarken en eskiden başla (eklenme sırası = dict sırası).
            while self._total + len(data) > self._max_bytes and self._items:
                k, v = next(iter(self._items.items()))
                self._items.pop(k, None)
                self._total -= len(v[0])
            self._items[key] = (data, ctype, time.time() + self._ttl)
            self._total += len(data)

    # --- tek uçuş --------------------------------------------------------
    def lease(self, key: str):
        """(sahip mi, olay) döner. Sahip indirmeyi yapar ve `release` çağırır;
        sahip olmayan `olay`ı bekleyip önbelleğe tekrar bakar."""
        with self._lock:
            olay = self._leases.get(key)
            if olay is not None:
                return False, olay
            olay = threading.Event()
            self._leases[key] = olay
            return True, olay

    def release(self, key: str) -> None:
        with self._lock:
            olay = self._leases.pop(key, None)
        if olay is not None:
            olay.set()

    @property
    def size(self) -> int:
        return self._total
