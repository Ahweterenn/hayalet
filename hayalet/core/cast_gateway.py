"""TV'ye yansıtma (Chromecast) için yerel ağ kapısı.

Oynatıcının HLS proxy'si (`proxy.HLSProxy`) yalnız 127.0.0.1'i dinliyor ve
kendisine verilen HER adresi çekip getiriyor. TV ise telefona ağ üzerinden
ulaşmak zorunda. Proxy'yi doğrudan yerel ağa açmak, aynı Wi-Fi'daki herkese
(yurt, kafe) telefonu açık bir aracı sunucu olarak kullandırmak olurdu.

Bunun yerine bu kapı:
  * yerel ağ adresinde ayrı bir portta dinler,
  * yalnız yolun başında 128 bitlik gizli anahtar taşıyan istekleri kabul eder
    (anahtarı yalnız yayın başlatılırken TV'ye verilen adres bilir),
  * isteği olduğu gibi 127.0.0.1'deki proxy'ye iletir,
  * playlist gövdelerindeki `http://127.0.0.1:PORT` adreslerini kendi
    adresine çevirir (alt playlist ve segmentler de kapıdan geçsin),
  * Chromecast alıcısının şart koştuğu CORS başlıklarını ekler.
"""
from __future__ import annotations

import secrets
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_CHUNK = 64 * 1024
_PASS_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges")
_TEXT_TYPES = ("mpegurl", "m3u8", "text/", "vtt")


def lan_ip() -> str | None:
    """Telefonun yerel ağdaki adresi (varsayılan rotanın arabirimi). UDP
    "bağlanma" paket göndermez; yalnız çekirdeğe hangi arabirimin
    kullanılacağını sorar."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        s.close()


class CastGateway:
    def __init__(self, host: str | None = None, port: int = 0):
        self.host = host or lan_ip()
        if not self.host:
            raise OSError("yerel ağ adresi bulunamadı (Wi-Fi bağlı mı?)")
        self.token = secrets.token_urlsafe(16)
        # Yalnız yansıtılan proxy'nin portu: anahtar sızsa bile kapı telefondaki
        # başka yerel servislere (oda sunucusu vb.) uzanmasın.
        self._ports: set[str] = set()
        self.httpd = ThreadingHTTPServer((self.host, port), self._handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.base = f"http://{self.host}:{self.port}/{self.token}"
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        kwargs={"poll_interval": 0.2}, daemon=True)

    def start(self) -> "CastGateway":
        self._thread.start()
        return self

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass

    def url_for(self, local_url: str) -> str:
        """127.0.0.1'deki proxy adresinin TV'nin açabileceği karşılığı."""
        if not local_url.startswith("http://127.0.0.1:"):
            raise ValueError("yalnız yerel proxy adresi yansıtılabilir")
        rest = local_url[len("http://127.0.0.1:"):]
        self._ports.add(rest.partition("/")[0])
        return f"{self.base}/{rest}"

    def _handler(gw):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                # Chrome'un "Private Network Access" kuralı: genel bir kökenden
                # yerel ağ adresine istek. Güvenli OLMAYAN (http) genel sayfaya
                # hiç izin yok (masaüstü Chrome'da ölçüldü: "not a secure
                # context ... more-private address space"); güvenli sayfa
                # (Chromecast alıcısı https gstatic.com'da) ön-onay ister ve bu
                # başlığı bekler. Gerçek Chromecast'te denenmedi.
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
                self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
                self.send_header("Access-Control-Expose-Headers",
                                 "Content-Length, Content-Range")

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_HEAD(self):
                self.do_GET(head=True)

            def do_GET(self, head=False):
                prefix = f"/{gw.token}/"
                # Anahtarsız istek: hiçbir şey sızdırmadan 404.
                if not self.path.startswith(prefix):
                    self.send_error(404)
                    return
                rest = self.path[len(prefix):]          # "PORT/yol?sorgu"
                port, _, path = rest.partition("/")
                if not port.isdigit() or port not in gw._ports:
                    self.send_error(404)
                    return
                req = urllib.request.Request(f"http://127.0.0.1:{port}/{path}")
                if self.headers.get("Range"):
                    req.add_header("Range", self.headers["Range"])
                try:
                    resp = urllib.request.urlopen(req, timeout=60)
                except urllib.error.HTTPError as e:
                    resp = e
                except Exception:
                    self.send_error(502)
                    return
                ctype = (resp.headers.get("Content-Type") or "").lower()
                text = any(t in ctype for t in _TEXT_TYPES) or path.split("?")[0].endswith(
                    (".m3u8", ".vtt"))
                try:
                    if text:
                        body = resp.read().replace(
                            b"http://127.0.0.1:", f"{gw.base}/".encode())
                        self.send_response(resp.status if hasattr(resp, "status") else resp.code)
                        self.send_header("Content-Type", resp.headers.get("Content-Type") or
                                         "application/vnd.apple.mpegurl")
                        self.send_header("Content-Length", str(len(body)))
                        self._cors()
                        self.end_headers()
                        if not head:
                            self.wfile.write(body)
                        return
                    self.send_response(resp.status if hasattr(resp, "status") else resp.code)
                    for k in _PASS_HEADERS:
                        v = resp.headers.get(k)
                        if v:
                            self.send_header(k, v)
                    self._cors()
                    self.end_headers()
                    if head:
                        return
                    while True:
                        chunk = resp.read(_CHUNK)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                    pass
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass
        return Handler
