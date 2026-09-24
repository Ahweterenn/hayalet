"""`/api/proxy` — video/altyazı trafiğini kendi üstümüzden geçirir.

Perde'nin Node sürümündeki proxy'nin karşılığı, iki farkla:

1. **curl-cffi** kullanılıyor (düz istek değil). Dizipal'in master playlist'i
   düz HTTP'de kararsız; TLS parmak izi taklidi bunu çözüyor.
2. Segmentler **akışla** geçiyor, belleğe toplanmıyor — telefonda birkaç
   izleyicide 2-3 MB'lık segmentleri biriktirmek şişer.

Tarayıcının segmentleri doğrudan CDN'den çekememesinin sebebi CORS ve
Referer zorunluluğu (ölçüldü: referersiz Dizipal 403, hdfilmcehennemi 404).
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from curl_cffi import requests as creq

from hayalet.perde import guard, manifest

_CHUNK = 64 * 1024
_TIMEOUT_MANIFEST = 25
_TIMEOUT_SEGMENT = 45
# Aynı segmenti başkası indiriyorsa bu kadar bekleriz; sonra kendimiz
# indiririz. KISA tutulması şart: hls.js bir segmenti iptal edip AYNI adresi
# hemen yeniden isteyebiliyor ve uzun bekleme oynatmayı kilitliyordu
# (telefonda ölçüldü: 30 sn'lik beklemeyle oynatıcı 0:00'da kaldı, sohbete
# "ev sahibi tarafında bağlantı sorunu" düştü). İzleyiciler senkron olduğu
# için gerçek örtüşme zaten saniyeler mertebesinde; bekleme dolarsa kendi
# kopyamızı çekeriz — yani en kötü ihtimalde eski davranışa döneriz.
_SEGMENT_WAIT = 3.0

# curl-cffi oturumu iş parçacığı başına. Paylaşılan tek oturum denendi ve
# kırılmadı (350 istek, eş zamanlı akış dahil), ama depo bu endişeyi
# belgelediği için ucuz sigortayı koruyoruz.
_local = threading.local()


@dataclass
class ProxyContext:
    """Proxy'nin kimliği. hayalet'in SessionState'inden doldurulur."""
    user_agent: str = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/133.0.0.0 Safari/537.36")
    impersonate: str = "chrome"
    cookies: dict = field(default_factory=dict)
    cache: manifest.ManifestCache = field(default_factory=manifest.ManifestCache)
    # Segment önbelleği: odadaki her izleyici AYNI segmenti istiyor.
    segments: manifest.SegmentCache = field(default_factory=manifest.SegmentCache)


def _session(ctx: ProxyContext):
    s = getattr(_local, "sess", None)
    if s is None:
        s = creq.Session(impersonate=ctx.impersonate)
        _local.sess = s
    return s


def _is_manifest_url(url: str) -> bool:
    return bool(re.search(r"\.m3u8($|\?)", url, re.I)
                or re.search(r"[?&](type|format)=m3u8(&|$)", url, re.I))


def _is_fake_image(url: str) -> bool:
    """Uzantısı .jpg/.png ama aslında video segmenti — bazı kaynaklar
    engellemeyi atlatmak için segmentleri böyle sunuyor."""
    return bool(re.search(r"\.(jpg|jpeg|png|bmp)($|\?)", url, re.I)
                and not _is_manifest_url(url))


def _upstream_headers(ctx: ProxyContext, url: str, referer: str | None,
                      room_headers: dict, range_header: str | None) -> dict:
    is_manifest = _is_manifest_url(url)
    h = {
        "User-Agent": room_headers.get("user-agent") or ctx.user_agent,
        "Accept": "*/*, application/vnd.apple.mpegurl" if is_manifest else "*/*",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }
    ref = room_headers.get("referer") or referer
    if ref and ref != "null":
        h["Referer"] = ref
        origin = room_headers.get("origin")
        if not origin:
            try:
                p = urlparse(ref)
                origin = f"{p.scheme}://{p.netloc}"
            except Exception:
                origin = None
        if origin:
            h["Origin"] = origin
    if room_headers.get("cookie"):
        h["Cookie"] = room_headers["cookie"]
    if room_headers.get("authorization"):
        h["Authorization"] = room_headers["authorization"]
    if range_header:
        h["Range"] = range_header
    return h


_CORS = [
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS"),
    ("Access-Control-Allow-Headers",
     "Origin, X-Requested-With, Content-Type, Accept, Range, Authorization"),
    ("Access-Control-Expose-Headers", "Content-Length, Content-Range"),
]


def _text(status: str, body: str, ctype="text/plain; charset=utf-8"):
    data = body.encode("utf-8")
    return status, _CORS + [("Content-Type", ctype),
                            ("Content-Length", str(len(data))),
                            ("Cache-Control", "no-store")], [data]


class _SegIzni:
    """Tek uçuş kilidinin sahipliği.

    Akışı üstlenen üretici `devret` der ve kilidi kendisi bırakır; başka her
    dönüş yolunda `bitir` bırakır. Her `return`u tek tek kollamak hataya
    açıktı: unutulan bir yol, aynı segmenti isteyen ötekileri 30 sn
    bekletirdi.
    """

    def __init__(self):
        self.cache = None
        self.key = None
        self._devredildi = False

    def al(self, cache, key) -> None:
        self.cache, self.key = cache, key

    def devret(self) -> None:
        self._devredildi = True

    def bitir(self) -> None:
        if self.cache is not None and not self._devredildi:
            self.cache.release(self.key)
        self.cache = None


def handle(environ, ctx: ProxyContext, room_lookup) -> tuple:
    """WSGI'ye uygun (status, headers, iterable) döndürür.

    room_lookup(room_id) -> Room | None ; oda başına saklanan istek
    başlıklarını (UA/Referer/Cookie) almak için.
    """
    seg = _SegIzni()
    try:
        return _handle(environ, ctx, room_lookup, seg)
    finally:
        seg.bitir()


def _handle(environ, ctx: ProxyContext, room_lookup, seg: "_SegIzni") -> tuple:
    if environ.get("REQUEST_METHOD") == "OPTIONS":
        return "200 OK", _CORS + [("Content-Length", "0")], [b""]

    q = parse_qs(environ.get("QUERY_STRING", ""))
    target = (q.get("url") or [""])[0]
    room_id = (q.get("roomId") or ["-"])[0]
    referer = (q.get("ref") or [""])[0] or None
    if not target:
        return _text("400 Bad Request", "URL parametresi eksik.")

    if not referer:
        try:
            p = urlparse(target)
            referer = f"{p.scheme}://{p.netloc}"
        except Exception:
            referer = None

    try:
        guard.check_target(target)
    except guard.BlockedTarget as e:
        return _text("403 Forbidden", f"Bu hedefe erişim engellendi ({e}).")

    room = room_lookup(room_id)
    room_headers = {}
    if room:
        room_headers = dict(room.headers or {})
        extra = (room.sub_headers or {}).get(target)
        if extra:
            room_headers.update(extra)

    is_manifest = _is_manifest_url(target)
    cache_key = f"{room_id}|{referer or ''}|{target}"
    if is_manifest:
        hit = ctx.cache.get(cache_key)
        if hit is not None:
            return _manifest_response(hit, manifest.ttl_seconds(hit))

    range_header = environ.get("HTTP_RANGE")

    # --- segment: önce önbellek, sonra tek uçuş ---------------------------
    # Oda senkron olduğu için izleyiciler aynı segmenti saniyeler içinde
    # istiyor. Eskiden her istek ayrı bir yukarı akış indirmesi başlatıyordu:
    # üç izleyici = telefonun aynı 1 MB'ı üç kez indirmesi. Range'li istekler
    # dışarıda (parça parça gövdeyi önbelleğe almak karışık, oynatıcılar da
    # HLS segmentini bütün ister).
    seg_key = None
    seg_sahip = False
    if not is_manifest and not _is_subtitle(target) and not range_header:
        seg_key = target
        hit = ctx.segments.get(seg_key)
        if hit is not None:
            return _cached_segment(hit)
        seg_sahip, olay = ctx.segments.lease(seg_key)
        if not seg_sahip:
            olay.wait(_SEGMENT_WAIT)
            hit = ctx.segments.get(seg_key)
            if hit is not None:
                return _cached_segment(hit)
            # İndiren başarısız olmuş: sırayı biz alıp kendimiz deneriz.
            seg_sahip, olay = ctx.segments.lease(seg_key)
        if seg_sahip:
            seg.al(ctx.segments, seg_key)

    headers = _upstream_headers(ctx, target, referer, room_headers, range_header)
    timeout = _TIMEOUT_MANIFEST if is_manifest else _TIMEOUT_SEGMENT
    # Segmentler akışla geçer, playlist'ler bir kerede alınır. Adresine
    # bakarak karar veriyoruz; yanıtın kendisi playlist çıkarsa gövdeyi
    # `_read_all` ile okumak ŞART (bkz. aşağıdaki not).
    streamed = not is_manifest

    try:
        resp = _session(ctx).get(
            target, headers=headers, cookies=ctx.cookies or None,
            timeout=timeout, stream=streamed, allow_redirects=True)
    except Exception as e:
        if is_manifest:
            stale = ctx.cache.get_stale(cache_key)
            if stale is not None:
                return _manifest_response(stale, 2)
        return _text("504 Gateway Timeout", f"Kaynak yanıt vermedi ({type(e).__name__}).")

    # Bazı sağlayıcılarda ses playlist'i ld.php altında 404 dönüyor; aynı
    # query ile l.php bir kez denenir (Perde'den gelen bilinen düzeltme).
    if resp.status_code == 404 and "/ld.php" in target and "ldRetry" not in environ.get("QUERY_STRING", ""):
        alt = target.replace("/ld.php", "/l.php")
        try:
            resp = _session(ctx).get(alt, headers=headers, timeout=timeout,
                                     stream=streamed, allow_redirects=True)
            target = alt
        except Exception:
            pass

    ctype = str(resp.headers.get("content-type", "")).lower()
    is_manifest_resp = (is_manifest or "mpegurl" in ctype
                        or "application/x-mpegurl" in ctype)

    # Manifest için üst kaynak hatası: HTML gövdesini m3u8 gibi göndermek
    # oynatıcıyı anlaşılmaz şekilde kilitliyor. Hatayı aynen ilet, önbelleğe alma.
    if is_manifest_resp and resp.status_code >= 400:
        stale = ctx.cache.get_stale(cache_key)
        if stale is not None:
            return _manifest_response(stale, 2)
        code = 410 if resp.status_code in (404, 410) else resp.status_code
        return _text(f"{code} Gone",
                     f"Kaynak linki geçersiz/süresi dolmuş (upstream {resp.status_code}).")

    if is_manifest_resp:
        # `resp.text` YALNIZCA akışsız istekte dolu. Burası iki yoldan
        # geliniyor ve ikincisi akış modunda: URL `.m3u8` içermiyorsa
        # (Dizipal'in `l.php?v=...` playlist'leri) `stream=True` ile
        # istiyoruz, ama content-type mpegurl gelince bu dala düşüyor ve
        # `resp.text` BOŞ dönüyordu. Sonuç: 200 + 0 bayt playlist, üstüne
        # önbelleğe yazılıp sonraki istekleri de zehirliyor → oynatıcı
        # 0:00'da kalıyor. Odaya gönderilen videonun "bağlantı sorunu"
        # diye görünen hatası tam buydu; kararsız olmasının sebebi de aynı
        # adresin content-type'ını bazen text/html bazen mpegurl döndürmesi.
        body = (resp.text if not streamed
                else _read_all(resp).decode("utf-8", "replace"))
        build = manifest.make_proxy_url_builder(target, referer, room_id)
        rewritten = manifest.rewrite_body(body, build)
        if not rewritten.strip():
            # Boş playlist'i ASLA önbelleğe almıyoruz: geçici bir aksaklığı
            # kalıcı takılmaya çeviriyor.
            stale = ctx.cache.get_stale(cache_key)
            if stale is not None:
                return _manifest_response(stale, 2)
            return _text("502 Bad Gateway", "Kaynak boş playlist döndü.")
        ttl = manifest.ttl_seconds(rewritten)
        ctx.cache.put(cache_key, rewritten, ttl)
        return _manifest_response(rewritten, ttl)

    if _is_subtitle(target):
        text = manifest.to_webvtt(_read_all(resp).decode("utf-8", "replace"))
        data = text.encode("utf-8")
        return ("200 OK",
                _CORS + [("Content-Type", "text/vtt; charset=utf-8"),
                         ("Content-Length", str(len(data)))],
                [data])

    # Gizli manifest: text/plain veya octet-stream ile gelen m3u8.
    if not _is_fake_image(target) and _maybe_text(ctype):
        raw = _read_all(resp)
        try:
            as_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            as_text = ""
        if as_text and manifest.looks_like_manifest(as_text):
            build = manifest.make_proxy_url_builder(target, referer, room_id)
            rewritten = manifest.rewrite_body(as_text, build)
            ttl = manifest.ttl_seconds(rewritten)
            ctx.cache.put(cache_key, rewritten, ttl)
            return _manifest_response(rewritten, ttl)
        # Manifest değilmiş: demek ki text/plain ya da octet-stream ile gelen
        # bir segment. Bu dal önbelleği ATLIYORDU — bazı kaynaklar segmenti
        # tam da böyle sunuyor (ölçüldü: octet-stream yanıtta dördüncü istek
        # hâlâ yukarı akışa gidiyordu), yani odadaki her izleyici yine ayrı
        # indiriyordu.
        if seg_sahip and resp.status_code == 200:
            ctx.segments.put(seg_key, raw, _cached_ctype(resp, target))
        return (_status_line(resp.status_code),
                _CORS + _segment_headers(resp, target) + [("Content-Length", str(len(raw)))],
                [raw])

    if seg_sahip and resp.status_code == 200:
        # Akarken önbelleğe de yazıyoruz: ilk izleyici beklemiyor, sonraki
        # izleyiciler aynı baytları yukarı akışa hiç gitmeden alıyor.
        seg.devret()
        return (_status_line(resp.status_code),
                _CORS + _segment_headers(resp, target),
                _stream_and_cache(resp, ctx.segments, seg_key,
                                  _cached_ctype(resp, target)))

    return (_status_line(resp.status_code),
            _CORS + _segment_headers(resp, target),
            _stream(resp))


def _read_all(resp) -> bytes:
    """Akış hâlindeki yanıtın gövdesini tamamen okur.

    DİKKAT: `stream=True` ile alınan bir yanıtta `resp.content` / `resp.text`
    BOŞ döner — gövde henüz okunmamıştır. Bu sessiz bir hata: proxy 200 ve
    0 bayt döndürüyordu, altyazılar boş geliyordu (telefonda ölçüldü).
    """
    if getattr(resp, "_perde_body", None) is None:
        try:
            resp._perde_body = b"".join(c for c in resp.iter_content(_CHUNK) if c)
        except Exception:
            resp._perde_body = b""
    return resp._perde_body


def _maybe_text(ctype: str) -> bool:
    return (not ctype) or ctype.startswith("text/") or "octet-stream" in ctype


def _is_subtitle(url: str) -> bool:
    return ".vtt" in url.lower() or ".srt" in url.lower()


def _cached_ctype(resp, target: str) -> str:
    if _is_fake_image(target):
        return "video/mp2t"
    return str(resp.headers.get("content-type") or "video/mp2t")


def _cached_segment(hit) -> tuple:
    """Önbellekten gelen segment — yukarı akışa hiç gitmeden."""
    data, ctype = hit
    return ("200 OK",
            _CORS + [("Content-Type", ctype or "video/mp2t"),
                     ("Content-Length", str(len(data))),
                     ("Cache-Control", "public, max-age=120")],
            [data])


def _stream_and_cache(resp, cache, key: str, ctype: str):
    """Segmenti istemciye akıtırken kopyasını önbelleğe biriktirir.

    Yarıda kesilirse (oynatıcı seek yaptı, izleyici kapattı) önbelleğe
    YAZILMAZ — eksik segment sonraki izleyiciyi bozardı.
    """
    parcalar = []
    boyut = 0
    tam = False
    try:
        for chunk in resp.iter_content(_CHUNK):
            if not chunk:
                continue
            if parcalar is not None:
                boyut += len(chunk)
                if boyut > cache.max_item:
                    parcalar = None          # çok büyük: biriktirmeyi bırak
                else:
                    parcalar.append(chunk)
            yield chunk
        tam = True
    except Exception:
        # Oynatıcı segmenti yarıda bırakabilir (seek, kapatma) — normal.
        return
    finally:
        try:
            if tam and parcalar:
                cache.put(key, b"".join(parcalar), ctype)
        finally:
            cache.release(key)


def _stream(resp):
    try:
        for chunk in resp.iter_content(_CHUNK):
            if chunk:
                yield chunk
    except Exception:
        # Oynatıcı segmenti yarıda bırakabilir (seek, kapatma) — normal.
        return


def _segment_headers(resp, target: str) -> list:
    out = []
    if _is_fake_image(target):
        # Sahte .jpg/.png uzantılı video segmenti: gerçek türünü bildir,
        # yoksa tarayıcı resim sanıp çözmeye çalışıyor.
        out.append(("Content-Type", "video/mp2t"))
    elif resp.headers.get("content-type"):
        out.append(("Content-Type", resp.headers["content-type"]))
    # DİKKAT: curl-cffi gzip/br/zstd gövdeyi AÇARAK veriyor, ama başlıktaki
    # `content-length` sıkıştırılmış boyutu söylüyor. Onu olduğu gibi iletmek
    # yanıtı kırpıyor — ölçüldü: 940 baytlık JSON istemciye 515 bayt olarak
    # ulaştı ve "unterminated string" ile bozuldu. Kodlama varsa uzunluğu hiç
    # göndermiyoruz; sunucu gövdeyi kendisi sonlandırır.
    kodlama = str(resp.headers.get("content-encoding", "") or "").lower()
    acilmis = kodlama not in ("", "identity")
    for h in ("accept-ranges", "content-range", "content-length"):
        if h == "content-length" and acilmis:
            continue
        v = resp.headers.get(h)
        if v:
            out.append((h.title(), v))
    out.append(("Cache-Control", "public, max-age=120"))
    return out


def _manifest_response(body: str, ttl: float):
    data = body.encode("utf-8")
    return ("200 OK",
            _CORS + [("Content-Type", "application/vnd.apple.mpegurl; charset=utf-8"),
                     ("Content-Length", str(len(data))),
                     ("Cache-Control", f"public, max-age={max(1, int(ttl))}")],
            [data])


_STATUS_TEXT = {200: "OK", 206: "Partial Content", 301: "Moved Permanently",
                302: "Found", 403: "Forbidden", 404: "Not Found",
                410: "Gone", 500: "Internal Server Error"}


def _status_line(code: int) -> str:
    return f"{code} {_STATUS_TEXT.get(code, 'OK')}"
