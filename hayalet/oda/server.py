"""Oda sunucusu: WSGI uygulaması + werkzeug ile çalıştırma.

Neden werkzeug: `wsgiref` WebSocket yükseltmesini desteklemiyor (environ'da
ham soketi vermiyor, engine.io `_upgrade_websocket`'te patlıyor — ölçüldü).
werkzeug veriyor ve o da saf Python, yani Chaquopy'ye giriyor.

Eski Node sürümünden BİLEREK taşınmayanlar:
  * `/api/shutdown` — telefonda servis bildiriminden durduruluyor, uzaktan
    kapatma yolu hiç açılmasın.
  * `/api/extract-video` (puppeteer) — hayalet'in kendi çıkarıcıları var.
"""
from __future__ import annotations

import json
from collections import OrderedDict
import logging
import mimetypes
import threading
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode

import socketio
from werkzeug.serving import make_server

from hayalet.oda import events, guard, proxy_api
from hayalet.oda import rooms as R

WEB = Path(__file__).parent / "web"
DEFAULT_PORT = 8477
# Poster aracısı: yalnız görüntü, küçük boyut, sınırlı önbellek.
_IMG_MAX = 2 * 1024 * 1024
_IMG_CACHE = 200

# werkzeug her isteği stderr'e yazıyor; telefonda logcat'i boğuyor ve her
# segment bir satır demek. Sadece gerçek hatalar kalsın.
logging.getLogger("werkzeug").setLevel(logging.ERROR)


ANDROID_PACKAGE = "com.hayalet.test"
TUNNEL_SUFFIXES = (".trycloudflare.com", ".srv.us", ".lhr.life")


def _android_browser(environ) -> bool:
    """Android'deki bir TARAYICI mı. Uygulamanın kendi WebView'i (`; wv)`)
    zaten odadadır, yönlendirilmez."""
    ua = environ.get("HTTP_USER_AGENT", "")
    return "Android" in ua and "; wv)" not in ua


def _app_intent(environ, oda_yolu: str) -> str:
    """Davet linkini uygulamaya aktaran `intent://` adresi.

    Neden: tünel adresi her odada değişen rastgele bir alt alan, Android 12+
    sahibi doğrulanmamış https linkini uygulamaya kendiliğinden vermiyor
    (kullanıcının ayarlardan izin vermesi gerekiyordu). `intent://` şemasını
    ise tarayıcı paket adıyla doğrudan uygulamaya iletiyor; uygulama yüklü
    değilse `browser_fallback_url` ile odayı tarayıcıda açıyor. Ayar yok,
    harici sayfa yok.
    """
    proto = (environ.get("HTTP_X_FORWARDED_PROTO", "").split(",")[0].strip()
             or environ.get("wsgi.url_scheme", "http"))
    host = environ.get("HTTP_HOST", "")
    if host.split(":")[0].endswith(TUNNEL_SUFFIXES):
        proto = "https"   # tünel TLS'i kendisi bitirir, başlık her zaman gelmiyor
    oda = f"{proto}://{host}{oda_yolu}"
    return (f"intent://oda?u={quote(oda, safe='')}#Intent;scheme=hayalet;"
            f"package={ANDROID_PACKAGE};"
            f"S.browser_fallback_url={quote(oda, safe='')};end")


class OdaServer:
    def __init__(self, port: int = DEFAULT_PORT,
                 ctx: proxy_api.ProxyContext | None = None,
                 room_id: str | None = None, catalog=None):
        self.port = port
        self.room_id = room_id or R.new_room_id()
        self.host_token = R.new_host_token()
        self.store = R.RoomStore()
        self.store.get_or_create(self.room_id)
        self.ctx = ctx or proxy_api.ProxyContext()
        self.public_url: str | None = None      # tünel adresi
        self.catalog = catalog
        self._img_cache: "OrderedDict[str, tuple[str, bytes]]" = OrderedDict()
        self._img_lock = threading.Lock()

        self.sio = socketio.Server(async_mode="threading",
                                   cors_allowed_origins="*")
        self._ops = events.register(self.sio, self.store, self.host_token,
                                    catalog=catalog, on_identity=self.apply_identity)
        self._sio_app = socketio.WSGIApp(self.sio, self._wsgi)
        self.app = self._dispatch
        self._server = None
        self._thread = None

    def _dispatch(self, environ, start_response):
        """socket.io ara katmanı `/socket.io/` ile başlayan HER yolu kendi
        el sıkışması sanıp 400 döndürüyor — istemci kütüphanesinin kendisi de
        o ön ekte duruyor (`room.html` onu oradan çekiyor). Bu yüzden o tek
        dosya ara katmandan ÖNCE karşılanmalı."""
        if environ.get("PATH_INFO") == "/socket.io/socket.io.js":
            return self._file(start_response, WEB / "socket.io.min.js",
                              "application/javascript; charset=utf-8")
        return self._sio_app(environ, start_response)

    # --- yönlendirmeler ---------------------------------------------------
    def _wsgi(self, environ, start_response):
        path = environ.get("PATH_INFO", "/") or "/"

        if path == "/api/proxy":
            status, headers, body = proxy_api.handle(
                environ, self.ctx, self.store.get)
            start_response(status, headers)
            return body

        if path == "/api/img":
            return self._image(environ, start_response)

        if path == "/api/state":
            room = self.store.get(self.room_id)
            return self._json(start_response, {
                "roomId": self.room_id,
                "users": room.usernames() if room else [],
                "hasVideo": bool(room.video_url) if room else False,
                "publicUrl": self.public_url,
            })

        # Kısa davet yolu — mesajlaşma uygulamalarında bozulmuyor. İsim
        # sorma ekranı oda sayfasının içinde; ayrı bir davet sayfası yok.
        if path in ("/i", "/", "/index.html"):
            oda = f"/room.html?room={quote(self.room_id)}"
            if path == "/i" and _android_browser(environ):
                return self._redirect(start_response, _app_intent(environ, oda))
            return self._redirect(start_response, oda)

        # Oda kimliği olmadan gelen misafiri KENDİ odamıza al. room.js
        # parametre yoksa sabit bir odaya düşüyordu; öyle gelen herkes o
        # hayalet odada buluşup birbirini görüyor, ev sahibi ise kendi
        # odasında yalnız kalıyordu (ölçüldü: sunucuda iki oda, sohbet
        # geçmiyor, odaya gönderilen video görünmüyor). Asıl hata davet
        # sayfasındaydı ve düzeltildi; bu, misafirin tarayıcısında ESKİ davet
        # sayfası önbellekte kalırsa diye ikinci emniyet.
        if path == "/room.html":
            sorgu = parse_qs(environ.get("QUERY_STRING", ""))
            if not (sorgu.get("room") or [""])[0]:
                sorgu["room"] = [self.room_id]
                yeni = urlencode({k: v[0] for k, v in sorgu.items() if v})
                return self._redirect(start_response, f"/room.html?{yeni}")
            return self._file(start_response, WEB / "index.html",
                              "text/html; charset=utf-8", cache=False)

        name = path.lstrip("/")
        if name and "/" not in name and ".." not in name:
            f = WEB / name
            if f.is_file():
                ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
                if ctype.startswith("text/") or "javascript" in ctype:
                    ctype += "; charset=utf-8"
                # Yalnız değişmeyen kütüphaneler önbelleğe alınır. Sayfanın kendi
                # dosyaları (oda.js, oda.css…) alınırsa güncellemeden sonra
                # tarayıcı eskisini kullanıyor; eski CSS + yeni JS karışıyordu
                # (tablette görüldü).
                return self._file(start_response, f, ctype, cache=name.endswith(".min.js"))

        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"bulunamadi"]

    # --- yardimcilar ------------------------------------------------------
    def _json(self, start_response, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        start_response("200 OK", [("Content-Type", "application/json; charset=utf-8"),
                                  ("Content-Length", str(len(data))),
                                  ("Access-Control-Allow-Origin", "*")])
        return [data]

    def _file(self, start_response, path: Path, ctype: str, cache: bool = True):
        data = path.read_bytes()
        headers = [("Content-Type", ctype), ("Content-Length", str(len(data)))]
        # Sayfanın kendisi önbelleğe alınmasın: güncellenen arayüz misafirde
        # eski sürümüyle kalmasın. Kütüphaneler (hls, socket.io) alınabilir.
        headers.append(("Cache-Control", "max-age=3600" if cache else "no-cache"))
        start_response("200 OK", headers)
        return [data]

    def _image(self, environ, start_response):
        """Poster aracısı. Bazı sitelerin görsel sunucusu TLS taklidi olmayan
        istemciye 403 veriyor (hdfilmcehennemi, ölçüldü); misafirin tarayıcısı
        posteri doğrudan alamıyor. Yalnız http(s), yalnız genel adresler
        (guard), yalnız image/* ve en fazla 2 MB."""
        url = (parse_qs(environ.get("QUERY_STRING", "")).get("u") or [""])[0]
        with self._img_lock:
            hit = self._img_cache.get(url)
        if hit is None:
            try:
                guard.check_target(url)
                from curl_cffi import requests as creq
                r = creq.get(url, impersonate=self.ctx.impersonate or "chrome",
                             headers={"User-Agent": self.ctx.user_agent},
                             timeout=15, allow_redirects=True)
                ctype = str(r.headers.get("content-type", "")).split(";")[0].strip()
                body = r.content or b""
                if r.status_code != 200 or not ctype.startswith("image/") or len(body) > _IMG_MAX:
                    raise ValueError(f"görsel değil ({r.status_code}, {ctype})")
            except Exception:
                start_response("404 Not Found", [("Content-Length", "0")])
                return [b""]
            hit = (ctype, body)
            with self._img_lock:
                self._img_cache[url] = hit
                while len(self._img_cache) > _IMG_CACHE:
                    self._img_cache.popitem(last=False)
        ctype, body = hit
        start_response("200 OK", [("Content-Type", ctype), ("Content-Length", str(len(body))),
                                  ("Cache-Control", "max-age=86400")])
        return [body]

    def play(self, ref: str) -> None:
        """Katalog ref'ini odaya koyar (arka planda çözülür, odada
        "Hazırlanıyor…" görünür; hata ev sahibinin ekranına düşer)."""
        self._ops["play"](self.room_id, ref)

    def apply_identity(self, resolved) -> None:
        """Çözülen akışın kimliğini proxy'ye taşır: CDN aynı UA/çerezi istiyor."""
        if resolved.user_agent:
            self.ctx.user_agent = resolved.user_agent
        if resolved.impersonate:
            self.ctx.impersonate = resolved.impersonate
        self.ctx.cookies = dict(resolved.cookies or {})

    def _redirect(self, start_response, location: str):
        start_response("302 Found", [("Location", location),
                                     ("Content-Length", "0")])
        return [b""]

    # --- yasam dongusu ----------------------------------------------------
    def start(self) -> "OdaServer":
        self._server = make_server("0.0.0.0", self.port, self.app, threaded=True)
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
            self._server = None

    # --- adresler ---------------------------------------------------------
    def host_url(self, base: str) -> str:
        """Ev sahibinin kendi açacağı adres — hostToken ile, lider olsun diye."""
        return (f"{base}/room.html?room={quote(self.room_id)}"
                f"&hostToken={quote(self.host_token)}")

    def invite_url(self, base: str) -> str:
        return f"{base}/i"


def set_video(server: OdaServer, video_url: str,
              subtitles: list | None = None, headers: dict | None = None,
              now: dict | None = None) -> None:
    """hayalet tarafından çözülmüş bir akışı odaya koyar ("Odaya gönder").

    Sunucu tarafından çağrılır (socket üzerinden değil), bu yüzden lider
    denetimine takılmaz.
    """
    room = server.store.get_or_create(server.room_id)
    subs = R.normalize_subtitles(subtitles or [])
    if headers:
        room.headers = {str(k).lower(): v for k, v in headers.items()}
    room.video_url = video_url
    room.subtitles = subs
    room.current_time = 0.0
    room.is_playing = False
    room.now = dict(now or {})
    server.sio.emit("video-changed",
                    {"videoUrl": video_url,
                     "subtitles": [s.as_dict() for s in subs],
                     "now": room.now},
                    room=server.room_id)
