"""Telefonda oda yığınının gerçekten ayağa kalktığını doğrular.

Masaüstünde çalıştığı ölçüldü; asıl soru Chaquopy/Android tarafı:
  1. Yedi saf-Python paketi APK'ya girdi mi, içe aktarılıyor mu?
  2. werkzeug 0.0.0.0'a bağlanabiliyor mu (Android port kısıtı var mı)?
  3. engine.io el sıkışması dönüyor mu (socket.io'nun ilk adımı)?
  4. Telefonun yerel IP'si ne — PC'den aynı Wi-Fi üzerinden vurmak için.

`run()` sonucu sözlük olarak döndürür; sunucu arka planda AÇIK kalır ki
dışarıdan da denenebilsin. `stop()` ile kapatılır.
"""
from __future__ import annotations

import json
import socket
import threading
import traceback

_server = None
_thread = None

PORT = 8099


def _local_ip() -> str:
    """Telefonun Wi-Fi adresi. Dışarı paket göndermeden, yalnızca yönlendirme
    tablosuna sorarak bulunur (UDP bağlanmak veri göndermez)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "?"
    finally:
        s.close()


def _imports() -> dict:
    """Yedi paketi tek tek dener; hangisi eksikse adıyla görünsün."""
    out = {}
    for mod in ("socketio", "engineio", "bidict", "simple_websocket",
                "wsproto", "h11", "werkzeug"):
        try:
            m = __import__(mod)
            out[mod] = getattr(m, "__version__", "var")
        except Exception as e:
            out[mod] = f"YOK ({type(e).__name__})"
    return out


def run(out_path: str | None = None) -> dict:
    """Yığını ayağa kaldırır ve sonucu döndürür (sunucu açık kalır)."""
    global _server, _thread
    res: dict = {"adim": "baslangic"}
    try:
        res["paketler"] = _imports()
        eksik = [k for k, v in res["paketler"].items() if str(v).startswith("YOK")]
        if eksik:
            res["sonuc"] = "KALDI"
            res["hata"] = f"eksik paket: {', '.join(eksik)}"
            return _finish(res, out_path)

        import socketio
        from werkzeug.serving import make_server

        res["adim"] = "sunucu kuruluyor"
        sio = socketio.Server(async_mode="threading", cors_allowed_origins="*")

        @sio.event
        def connect(sid, environ):
            pass

        @sio.on("ping-test")
        def _ping(sid, data):
            sio.emit("pong-test", {"echo": data}, to=sid)

        def fallback(environ, start_response):
            body = b"oda selftest ayakta"
            start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8"),
                                      ("Content-Length", str(len(body)))])
            return [body]

        app = socketio.WSGIApp(sio, fallback)

        # 0.0.0.0: ayni Wi-Fi'daki PC/telefonlar da vurabilsin.
        _server = make_server("0.0.0.0", PORT, app, threaded=True)
        _thread = threading.Thread(target=_server.serve_forever, daemon=True)
        _thread.start()
        res["adim"] = "sunucu calisiyor"
        res["port"] = PORT
        res["yerel_ip"] = _local_ip()

        # engine.io el sikismasi: socket.io'nun ilk adimi. Buradan 0{"sid":...}
        # donuyorsa protokol katmani saglam demektir.
        res["adim"] = "el sikismasi deneniyor"
        import urllib.request
        url = f"http://127.0.0.1:{PORT}/socket.io/?EIO=4&transport=polling"
        with urllib.request.urlopen(url, timeout=8) as r:
            raw = r.read(300).decode("utf-8", "replace")
        res["el_sikismasi_kodu"] = r.status
        res["el_sikismasi"] = raw[:160]
        res["sonuc"] = "GECTI" if '"sid"' in raw else "KALDI"
        if res["sonuc"] == "KALDI":
            res["hata"] = "el sikismasinda sid yok"
    except Exception as e:
        res["sonuc"] = "KALDI"
        res["hata"] = f"{type(e).__name__}: {e}"
        res["iz"] = traceback.format_exc()[-700:]
    return _finish(res, out_path)


def _finish(res: dict, out_path: str | None) -> dict:
    if out_path:
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False, indent=2)
        except Exception as e:
            res["yazma_hatasi"] = str(e)
    return res


def stop() -> None:
    global _server
    if _server is not None:
        try:
            _server.shutdown()
        except Exception:
            pass
        _server = None
