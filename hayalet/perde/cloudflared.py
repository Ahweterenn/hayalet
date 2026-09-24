"""Cloudflare hızlı tüneli (trycloudflare.com) — DNS'e muhtaç olmadan.

Perde'nin web sürümü uzaktan izlemeyi bu tünelle yapıyor: hesap istemiyor,
Cloudflare'in ağından geçtiği için hızlı. Telefonda ise uzun süre "çalışmıyor"
sanıldı (perde-plan.md Ö5): cloudflared saf Go ile statik derlenmiş, Go'nun
kendi DNS çözücüsü `/etc/resolv.conf` arıyor, Android'de o dosya yok ve
`[::1]:53`'e düşüp duruyor. Root olmadan dosya yazılamıyor, 53'e vekil
konamıyor.

Oysa cloudflared'ın DNS'e ihtiyacı yalnız iki yerde var ve ikisini de
dışarıdan karşılamak mümkün (ölçüldü 2026-09-24, tablet, root yok):

1. Hızlı tünel kaydı (`POST api.trycloudflare.com/tunnel`). Bunu biz
   yapıyoruz — Python soketleri Android'in kendi çözücüsünü kullanıyor —
   ve dönen kimliği bir kimlik dosyasına yazıp `tunnel run`a veriyoruz.
2. Bağlanılacak Cloudflare sunucuları (`region{1,2}.v2.argotunnel.com`).
   IP'leri yine biz çözüp `--edge IP:7844` ile veriyoruz.

Sonuç: `Registered tunnel connection … location=ist03`; tabletten
trycloudflare üzerinden 5 MB 0,97–1,41 MB/s geldi (hattın yükleme
kapasitesine yakın). Karşılaştırma: paramiko'lu SSH tüneli aynı gün
0,23–0,29 MB/s veriyordu.

Arayüz `tunnel.SshTunnel` ile aynı (`start`, `stop`, `url`, `last_error`,
`errors`, `on_url`), çağıran ikisini birbirinin yerine kullanabilir.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

QUICK_API = "https://api.trycloudflare.com/tunnel"
EDGE_HOSTS = ("region1.v2.argotunnel.com", "region2.v2.argotunnel.com")
EDGE_PORT = 7844
# Her bölgeden kaç sunucu verilir. cloudflared varsayılan olarak 4 bağlantı
# açıyor; iki bölgeden ikişer adres hem yeterli hem biri düşerse yedekli.
_EDGES_PER_HOST = 2

# Bağlantının gerçekten kurulduğunu gösteren satır. Hızlı tünelde adres bu
# satırdan önce belli ama bağlantı kurulmadan paylaşılan link misafire hata
# sayfası açar.
_REGISTERED_RE = re.compile(r"Registered tunnel connection")
_ERROR_RE = re.compile(r"\b(ERR|FTL)\b")

_REQUEST_TIMEOUT = 15.0
# Süreç düşerse yeniden deneme beklemesi (artan, üst sınırlı).
_RETRY_FIRST = 3.0
_RETRY_MAX = 60.0
# Son log satırları hata metnine eklenir; teşhis için yeterli, bellek için az.
_LOG_KEEP = 40

BINARY_ENV = "HAYALET_CLOUDFLARED"


class CloudflaredError(Exception):
    pass


@dataclass(frozen=True)
class QuickTunnel:
    """api.trycloudflare.com'un verdiği tek kullanımlık tünel kimliği."""

    id: str
    hostname: str
    account_tag: str
    secret: str          # base64, API'nin verdiği hâliyle

    @property
    def url(self) -> str:
        return "https://" + self.hostname

    def credentials(self) -> dict:
        """cloudflared'ın `--credentials-file` biçimi."""
        return {"AccountTag": self.account_tag,
                "TunnelSecret": self.secret,
                "TunnelID": self.id}


# --- saf yardımcılar (testler bunları doğrudan sınıyor) ----------------------

def parse_quick_response(body: bytes | str) -> QuickTunnel:
    """API yanıtını çözer; eksik/başarısız yanıtta CloudflaredError."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError) as e:
        raise CloudflaredError(f"hızlı tünel yanıtı JSON değil: {e}") from e
    if not isinstance(data, dict) or not data.get("success"):
        errors = data.get("errors") if isinstance(data, dict) else None
        raise CloudflaredError(f"hızlı tünel reddedildi: {errors or data!r}")
    r = data.get("result") or {}
    try:
        qt = QuickTunnel(id=str(r["id"]), hostname=str(r["hostname"]),
                         account_tag=str(r["account_tag"]),
                         secret=str(r["secret"]))
    except KeyError as e:
        raise CloudflaredError(f"hızlı tünel yanıtında {e} yok") from e
    if not all((qt.id, qt.hostname, qt.account_tag, qt.secret)):
        raise CloudflaredError("hızlı tünel yanıtında boş alan var")
    return qt


def resolve_edges(resolver: Callable = socket.getaddrinfo,
                  hosts: Sequence[str] = EDGE_HOSTS,
                  per_host: int = _EDGES_PER_HOST) -> list[str]:
    """Cloudflare tünel sunucularını `IP:7844` listesine çözer.

    Yalnız IPv4: `--edge-ip-version 4` ile eşleşmeli. Bir bölge çözülemezse
    öbürüyle devam edilir; hiçbiri çözülemezse CloudflaredError.
    """
    edges: list[str] = []
    failures: list[str] = []
    for host in hosts:
        try:
            infos = resolver(host, EDGE_PORT, socket.AF_INET, socket.SOCK_STREAM)
        except OSError as e:
            failures.append(f"{host}: {e}")
            continue
        ips = sorted({info[4][0] for info in infos})
        for ip in random.sample(ips, min(per_host, len(ips))):
            edges.append(f"{ip}:{EDGE_PORT}")
    if not edges:
        raise CloudflaredError("Cloudflare sunucuları çözülemedi: "
                               + ("; ".join(failures) or "adres yok"))
    return edges


def build_command(binary: str | Sequence[str], creds_path: str,
                  edges: Sequence[str], local_port: int,
                  tunnel_id: str) -> list[str]:
    """`cloudflared tunnel run` komutu. `binary` bir önek listesi de olabilir
    (testler sahte süreci `python sahte.py` diye veriyor)."""
    prefix = [binary] if isinstance(binary, str) else list(binary)
    cmd = prefix + ["tunnel", "--no-autoupdate",
                    "--edge-ip-version", "4",
                    # TCP: mobil CGNAT'ta UDP'den daha az sürpriz; ölçülen
                    # hız da bununla alındı.
                    "--protocol", "http2"]
    for e in edges:
        cmd += ["--edge", e]
    cmd += ["--credentials-file", creds_path,
            "--url", f"http://127.0.0.1:{local_port}",
            "run", tunnel_id]
    return cmd


def find_binary(explicit: str | None = None) -> str | None:
    """Açık yol > HAYALET_CLOUDFLARED > PATH. Bulunamazsa None."""
    for cand in (explicit, os.environ.get(BINARY_ENV)):
        if cand and os.path.isfile(cand):
            return cand
    return shutil.which("cloudflared")


def request_quick_tunnel(timeout: float = _REQUEST_TIMEOUT) -> QuickTunnel:
    """api.trycloudflare.com'dan yeni bir tünel kimliği ister."""
    req = urllib.request.Request(
        QUICK_API, data=b"", method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "cloudflared"})
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ssl_context()) as resp:
            body = resp.read()
    except OSError as e:
        raise CloudflaredError(f"hızlı tünel istenemedi: {e}") from e
    return parse_quick_response(body)


def _ssl_context() -> ssl.SSLContext:
    # Android'deki Python'un kendi sertifika deposu yok; certifi APK'da var.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


# --- tünel -------------------------------------------------------------------

class CloudflaredTunnel:
    """Yerel `local_port`'u trycloudflare.com adresiyle dışarı açar."""

    def __init__(self, local_port: int, on_url=None,
                 binary: str | Sequence[str] | None = None,
                 work_dir: str | os.PathLike | None = None,
                 request_fn: Callable[[], QuickTunnel] = request_quick_tunnel,
                 resolver: Callable = socket.getaddrinfo):
        self.local_port = local_port
        self.url: str | None = None
        self._on_url = on_url
        self._binary = binary if binary is not None else find_binary()
        self._work_dir = Path(work_dir) if work_dir else None
        self._request_fn = request_fn
        self._resolver = resolver
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._creds_path: Path | None = None
        self._last_error: str | None = None
        self._log: deque[str] = deque(maxlen=_LOG_KEEP)

    # --- dış yüz ---------------------------------------------------------
    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def errors(self) -> list[str]:
        return [self._last_error] if self._last_error else []

    @property
    def log(self) -> list[str]:
        """cloudflared'ın son satırları (teşhis için)."""
        return list(self._log)

    def start(self, timeout: float = 30.0) -> str:
        """Tüneli kurar, genel adresi döndürür; kurulamazsa CloudflaredError.

        SshTunnel gibi: zaman aşımı yalnız beklemeyi bitirir, arka plan
        denemeye devam eder ve adres sonradan gelirse `on_url` ile bildirilir.
        Çağıran vazgeçip yedeğe geçecekse `stop()` çağırmalı.
        """
        if not self._binary:
            raise CloudflaredError("cloudflared bulunamadı")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="cloudflared")
        self._thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.url:
                return self.url
            if not self._thread.is_alive():
                break
            time.sleep(0.2)
        raise CloudflaredError(self._last_error or "tünel adresi alınamadı")

    def stop(self) -> None:
        """Süreci öldürür; döndüğünde süreç bitmiş ve gizli kimlik silinmiştir.

        Arka plan iş parçacığı da kendi `finally`sinde temizlik yapıyor. Onu
        beklemeden dönmek, silmenin stop()'tan SONRA olmasına yol açıyordu
        (ölçüldü: testte ara sıra anahtar dosyası stop() dönünce hâlâ vardı).
        """
        self._stop.set()
        self.url = None
        self._kill()
        th = self._thread
        if th is not None and th is not threading.current_thread():
            th.join(timeout=10)
        self._remove_creds()

    # --- iç işleyiş -------------------------------------------------------
    def _run(self) -> None:
        wait = _RETRY_FIRST
        while not self._stop.is_set():
            served = False
            try:
                served = self._serve_once()
            except CloudflaredError as e:
                self._last_error = str(e)
            except Exception as e:  # noqa: BLE001 - iş parçacığı ölmesin
                self._last_error = f"{type(e).__name__}: {e}"
            finally:
                self.url = None
                self._kill()
                self._remove_creds()
            if self._stop.is_set():
                return
            # Kurulup düştüyse hemen yeniden dene (adres DEĞİŞİR, on_url ile
            # bildirilecek); hiç kurulamadıysa artan beklemeyle.
            wait = _RETRY_FIRST if served else min(wait * 2, _RETRY_MAX)
            self._stop.wait(wait)

    def _serve_once(self) -> bool:
        """Bir tünel açar, süreç bitene kadar izler. Kurulduysa True."""
        qt = self._request_fn()
        edges = resolve_edges(self._resolver)
        creds = self._write_creds(qt)
        cmd = build_command(self._binary, str(creds), edges,
                            self.local_port, qt.id)
        with self._proc_lock:
            if self._stop.is_set():
                return False
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1)
            proc = self._proc

        registered = False
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            self._log.append(line)
            if not registered and _REGISTERED_RE.search(line):
                registered = True
                self.url = qt.url
                self._last_error = None
                if self._on_url:
                    try:
                        self._on_url(qt.url)
                    except Exception:
                        pass
            elif not registered and _ERROR_RE.search(line):
                self._last_error = "cloudflared: " + line[-300:]
            if self._stop.is_set():
                break
        code = proc.wait()
        if not self._stop.is_set():
            tail = " | ".join(list(self._log)[-3:])
            self._last_error = (f"cloudflared {code} koduyla çıktı"
                                + (f": {tail}" if tail else ""))
        return registered

    def _write_creds(self, qt: QuickTunnel) -> Path:
        base = self._work_dir or Path(tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"cloudflared-{qt.id}.json"
        # Gizli anahtar taşıyor: yalnız bu kullanıcı okuyabilsin.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(qt.credentials(), f)
        self._creds_path = path
        return path

    def _remove_creds(self) -> None:
        with self._proc_lock:
            p, self._creds_path = self._creds_path, None
        if p is None:
            return
        try:
            p.unlink()
        except OSError:
            pass

    def _kill(self) -> None:
        with self._proc_lock:
            proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
