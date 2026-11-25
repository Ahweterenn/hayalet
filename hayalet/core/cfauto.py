"""Cloudflare cf_clearance'ı kendiliğinden yenileme — masaüstü ve Android ortak.

Cookie'yi KİM alırsa alsın kayıt aynıdır (prefs.save_cf: cookie + onu veren
tarayıcının UA'sı). Değişen yalnız "çözücü":
  - masaüstü: penceresiz (headless) Chrome (core/cf_grab.py, CDP)
  - Android: uygulamanın görünmez WebView'i (CfSolver.java) — köprü
    (hayalet_app.py) açılışta `cfauto.solver`'ı ona bağlar.
Cookie onu alan cihazın IP'sine bağlı olduğundan her cihaz kendi cookie'sini
kendisi almak ZORUNDA; PC'nin cookie'si telefonda işe yaramaz.
"""
from __future__ import annotations

import os
import threading
import time

from hayalet.core import prefs

# callable(url: str, timeout_s: float) -> (cookie, user_agent); None = masaüstü varsayılanı
solver = None

_lock = threading.Lock()


def impersonate_for(ua: str) -> str | None:
    """Cookie'yi veren tarayıcının UA'sına uyan curl-cffi TLS profili.

    Cloudflare UA ile TLS imzasının aynı aileden olmasına bakabiliyor;
    rastgele persona (ör. Safari) imzasıyla Chrome cookie'si göndermek riskli.
    """
    if not ua or "Edg/" in ua:
        return None
    if "Chrome/" in ua:
        return "chrome_android" if "Android" in ua else "chrome"
    return None


def parse_cookie(raw: str) -> dict[str, str]:
    """'abc', 'cf_clearance=abc' ya da 'a=1; b=2' -> sözlük."""
    raw = (raw or "").strip()
    if "=" not in raw:
        return {"cf_clearance": raw} if raw else {}
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def apply_saved(adapter, session) -> bool:
    """Kayıtlı cookie+UA varsa oturuma uygular. Uygulandıysa True."""
    rec = prefs.load_cf(adapter.known_domain)
    if not rec:
        return False
    session.cookies.update(parse_cookie(rec["cookie"]))
    ua = rec.get("user_agent")
    if ua:
        session.user_agent = ua
        imp = impersonate_for(ua)
        if imp:
            session.impersonate = imp
    return True


def _desktop_solver(url: str, timeout: float):
    # Headless: ekran (DISPLAY) gerekmez, systemd zamanlayıcısından da çalışır.
    from hayalet.core import cf_grab
    return cf_grab.grab(url, timeout=timeout, hidden=True)


def available() -> bool:
    if os.environ.get("HAYALET_CF_OTOMATIK", "1") == "0":
        return False
    if solver is not None:
        return True
    from hayalet.core import cf_grab
    try:
        cf_grab._find_chrome()
    except cf_grab.CfGrabError:
        return False
    return True


def refresh(adapter, timeout: float = 45) -> bool:
    """Yeni cookie alıp kaydeder. Başarılıysa True (hata yutulur, False döner).

    Eski kayıt yenisi gelene kadar SİLİNMEZ: önce silmek, çözücü çalışırken
    (~3 sn) aynı siteye bağlanan diğer iş parçacıklarına "cookie yok" gösterip
    onları da yenilemeye itiyordu — biri az önce alınan cookie'yi silip
    tarayıcıyı yeniden açıyordu (ölçüldü 2026-11-25, telefonda açılış).
    Kilidi beklerken başkası yenilediyse çözücü hiç çalıştırılmaz.
    """
    if not available():
        return False
    asked = time.time()
    with _lock:   # aynı anda iki pencere/WebView açılmasın
        rec = prefs.load_cf(adapter.known_domain)
        if rec and rec.get("ts", 0) >= asked:
            return True   # biz beklerken başka bir iş parçacığı yeniledi
        try:
            got = (solver or _desktop_solver)(adapter.known_domain, timeout)
        except Exception:
            got = None
        if not got or not got[0]:
            prefs.forget_cf(adapter.known_domain)   # bayat kaydı tutma
            return False
        cookie, ua = got
        prefs.save_cf(adapter.known_domain, cookie, ua)
        return True


def is_challenge(exc: BaseException) -> bool:
    return "challenge" in str(exc).lower()
