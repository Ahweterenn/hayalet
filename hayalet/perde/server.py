"""Perde sunucusu: WSGI uygulaması + werkzeug ile çalıştırma.

Neden werkzeug: `wsgiref` WebSocket yükseltmesini desteklemiyor (environ'da
ham soketi vermiyor, engine.io `_upgrade_websocket`'te patlıyor — ölçüldü).
werkzeug veriyor ve o da saf Python, yani Chaquopy'ye giriyor.

Perde'nin Node sürümünden BİLEREK taşınmayanlar:
  * `/api/shutdown` — telefonda servis bildiriminden durduruluyor, uzaktan
    kapatma yolu hiç açılmasın.
  * `/api/extract-video` (puppeteer) — hayalet'in kendi çıkarıcıları var.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import threading
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode

import socketio
from werkzeug.serving import make_server

from hayalet.perde import events, proxy_api
from hayalet.perde import rooms as R

PUBLIC = Path(__file__).parent / "public"
DEFAULT_PORT = 8477

# werkzeug her isteği stderr'e yazıyor; telefonda logcat'i boğuyor ve her
# segment bir satır demek. Sadece gerçek hatalar kalsın.
logging.getLogger("werkzeug").setLevel(logging.ERROR)


class PerdeServer:
    def __init__(self, port: int = DEFAULT_PORT,
                 ctx: proxy_api.ProxyContext | None = None,
                 room_id: str | None = None):
        self.port = port
        self.room_id = room_id or R.new_room_id()
        self.host_token = R.new_host_token()
        self.store = R.RoomStore()
        self.store.get_or_create(self.room_id)
        self.ctx = ctx or proxy_api.ProxyContext()
        self.public_url: str | None = None      # röle/tünel adresi (Faz 4)

        self.sio = socketio.Server(async_mode="threading",
                                   cors_allowed_origins="*")
        events.register(self.sio, self.store, self.host_token)
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
            return self._file(start_response, PUBLIC / "socket.io.min.js",
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

        if path == "/api/rooms":
            return self._json(start_response, {
                "rooms": [{"id": r.id, "userCount": len(r.users),
                           "hasVideo": bool(r.video_url)}
                          for r in self.store.all().values()],
                "defaultRoom": self.room_id,
            })

        if path == "/api/state":
            room = self.store.get(self.room_id)
            return self._json(start_response, {
                "roomId": self.room_id,
                "users": room.usernames() if room else [],
                "hasVideo": bool(room.video_url) if room else False,
                "publicUrl": self.public_url,
            })

        # Kısa davet yolu — mesajlaşma uygulamalarında bozulmuyor.
        if path == "/i":
            return self._redirect(start_response,
                                  f"/invite.html?room={quote(self.room_id)}")

        if path in ("/", "/index.html"):
            return self._redirect(start_response,
                                  f"/invite.html?room={quote(self.room_id)}")

        # Oda kimliği olmadan gelen misafiri KENDİ odamıza al. room.js
        # parametre yoksa sabit 'PERDE' odasına düşüyor; öyle gelen herkes o
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

        # Depoya girmeyen isteğe bağlı kişisel dosyalar: 404 yerine boş içerik,
        # yoksa room.html konsolda hata basıyor.
        if path in ("/couple-private.local.js", "/couple-private.local.css"):
            ctype = ("text/css" if path.endswith(".css")
                     else "application/javascript")
            start_response("200 OK", [("Content-Type", ctype),
                                      ("Content-Length", "0")])
            return [b""]

        name = path.lstrip("/")
        if name and "/" not in name and ".." not in name:
            f = PUBLIC / name
            if f.is_file():
                ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
                if ctype.startswith("text/") or "javascript" in ctype:
                    ctype += "; charset=utf-8"
                return self._file(start_response, f, ctype)

        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"bulunamadi"]

    # --- yardimcilar ------------------------------------------------------
    def _json(self, start_response, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        start_response("200 OK", [("Content-Type", "application/json; charset=utf-8"),
                                  ("Content-Length", str(len(data))),
                                  ("Access-Control-Allow-Origin", "*")])
        return [data]

    def _file(self, start_response, path: Path, ctype: str):
        data = path.read_bytes()
        start_response("200 OK", [("Content-Type", ctype),
                                  ("Content-Length", str(len(data)))])
        return [data]

    def _redirect(self, start_response, location: str):
        start_response("302 Found", [("Location", location),
                                     ("Content-Length", "0")])
        return [b""]

    # --- yasam dongusu ----------------------------------------------------
    def start(self) -> "PerdeServer":
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


def set_video(server: PerdeServer, video_url: str,
              subtitles: list | None = None, headers: dict | None = None) -> None:
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
    server.sio.emit("video-changed",
                    {"videoUrl": video_url,
                     "subtitles": [s.as_dict() for s in subs]},
                    room=server.room_id)
