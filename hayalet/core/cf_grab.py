"""Cloudflare cf_clearance cookie'sini gerçek bir Chrome penceresinden otomatik alır.

Neden: JS challenge tarayıcısız geçilemez; elle F12 → Cookies → kopyala yerine
Chrome'u ayrı bir profille, uzaktan hata ayıklama (CDP) açık şekilde başlatıp
challenge geçince cookie'yi + o tarayıcının User-Agent'ını okuruz.

- Kullanıcının normal Chrome profiline dokunulmaz (ayrı --user-data-dir).
- cf_clearance HttpOnly'dir; sayfa JS'i okuyamaz, ama CDP Storage.getCookies okur.
- Otomatik yenileme (hidden=True) headless çalışır, ekranda HİÇBİR pencere açılmaz.
  Headless Chrome'un kendi UA'sında "HeadlessChrome" yazar ve Cloudflare onu
  reddeder; bu yüzden --user-agent ile normal Chrome UA'sı verilir (ölçüldü
  2026-11-25: hdfilmcehennemi ~2 sn'de geçti, cookie curl-cffi'de 200 verdi).
  Eskiden pencere ekran dışına (-32000) konuyordu ama GNOME onu ekrana geri
  çekiyordu: her gün saat 12'deki kontrolde üst üste pencereler açılıyordu.
- --cf-al (elle) yine görünür pencere açar: kutucuk isteyen challenge'ı
  ancak bir insan tıklayabilir.
"""
from __future__ import annotations

import itertools
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

PROFILE_DIR = Path.home() / ".cache" / "hayalet" / "cf-chrome"
_CHROMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


class CfGrabError(Exception):
    pass


def _find_chrome() -> str:
    for name in _CHROMES:
        path = shutil.which(name)
        if path:
            return path
    raise CfGrabError("Chrome/Chromium bulunamadı (google-chrome veya chromium kurulu olmalı).")


def _headless_ua(chrome: str) -> str:
    """Bu Chrome'un normal (pencereli) halinin UA'sı; "Chrome/157.0.0.0" biçiminde
    (Chrome UA'da yalnız ana sürümü gösterir)."""
    out = subprocess.run([chrome, "--version"], capture_output=True, text=True,
                         timeout=10).stdout
    m = re.search(r"(\d+)\.\d+", out)
    if not m:
        raise CfGrabError(f"Chrome sürümü okunamadı: {out.strip()!r}")
    return ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{m.group(1)}.0.0.0 Safari/537.36")


def _devtools_ws(timeout: float = 20) -> str:
    """Chrome port 0 ile açılınca seçtiği portu profil klasörüne yazar."""
    port_file = PROFILE_DIR / "DevToolsActivePort"
    end = time.time() + timeout
    while time.time() < end:
        try:
            port, path = port_file.read_text().split("\n")[:2]
            return f"ws://127.0.0.1:{port.strip()}{path.strip()}"
        except (OSError, ValueError):
            time.sleep(0.3)
    raise CfGrabError("Chrome açıldı ama hata ayıklama bağlantısı kurulamadı.")


class _Cdp:
    def __init__(self, ws_url: str):
        import simple_websocket
        self.ws = simple_websocket.Client.connect(ws_url)
        self.ids = itertools.count(1)

    def call(self, method: str, **params):
        msg_id = next(self.ids)
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.receive(timeout=10) or "{}")
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise CfGrabError(f"CDP {method}: {msg['error']}")
                return msg.get("result", {})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def grab(url: str, timeout: float = 180, on_wait=None,
         hidden: bool = False) -> tuple[str, str]:
    """Chrome'da url'yi açar, cf_clearance düşene kadar bekler.

    Dönüş: ("cf_clearance=...", user_agent). Süre dolarsa CfGrabError.
    on_wait: bekleme sırasında bir kez çağrılır (kullanıcıya mesaj basmak için).
    hidden: penceresiz (headless) çalışır — otomatik yenileme için. Kutucuk
    isteyen bir challenge'ı bu modda kimse tıklayamaz, o zaman süre dolar ve
    CfGrabError yükselir (o durumda --cf-al gerekir).
    """
    host = urlparse(url).hostname or ""
    # "www.site.nl" cookie'si genelde ".site.nl" alanına yazılır.
    base = host[4:] if host.startswith("www.") else host

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    (PROFILE_DIR / "DevToolsActivePort").unlink(missing_ok=True)
    chrome = _find_chrome()
    fixed_ua = _headless_ua(chrome) if hidden else None
    extra = (["--headless=new", f"--user-agent={fixed_ua}", "--window-size=1280,800"]
             if hidden else [])
    proc = subprocess.Popen(
        [chrome, f"--user-data-dir={PROFILE_DIR}", "--remote-debugging-port=0",
         "--no-first-run", "--no-default-browser-check", *extra,
         "--new-window", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp = None
    try:
        cdp = _Cdp(_devtools_ws())
        ua = fixed_ua or cdp.call("Browser.getVersion").get("userAgent", "")
        # Profil kalıcı: içinde ESKİ (süresi dolmuş) bir cf_clearance kalmış
        # olabilir ve yeni challenge geçmeden onu kapardık (ölçüldü: kaydedilen
        # cookie anında reddedildi). Önce temizle, siteyi SONRA aç.
        cdp.call("Storage.clearCookies")
        cdp.call("Target.createTarget", url=url)
        if on_wait:
            on_wait()
        end = time.time() + timeout
        while time.time() < end:
            if proc.poll() is not None:
                raise CfGrabError("Chrome penceresi kapatıldı; cookie alınamadı.")
            for c in cdp.call("Storage.getCookies").get("cookies", []):
                domain = c.get("domain", "").lstrip(".")
                if c.get("name") == "cf_clearance" and (domain == base or domain.endswith("." + base)
                                                       or base.endswith(domain)):
                    return f"cf_clearance={c['value']}", ua
            time.sleep(1)
        raise CfGrabError(f"{int(timeout)} saniyede doğrulama geçilmedi.")
    finally:
        if cdp:
            try:
                cdp.call("Browser.close")
            except Exception:
                pass
            cdp.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
