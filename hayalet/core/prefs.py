"""Kullanıcı tercihi kalıcılığı — .cache/prefs.json.

Üç alan grubu tutulur:
  - quality: son seçilen video kalitesi (interaktif menüde varsayılan olarak öne
    çıkar / non-interaktif'te --quality verilmezse bu değer kullanılır).
  - resume: "kaldığın yerden devam" — hangi site/dizi/sezon/bölümde kalındığı,
    artı o bölümdeki dakika-dakika konum (position/duration, saniye cinsinden;
    bkz. cli._save_resume/_resume_position, core/proxy.py /progress ucu).
  - cf: siteye göre Cloudflare cf_clearance cookie'si + onu veren tarayıcının
    User-Agent'ı (bkz. load_cf/save_cf ve cli --cf-cookie).
"""
from __future__ import annotations

import json
import time
from urllib.parse import urlparse

from hayalet import config

_FILE = config.CACHE_DIR / "prefs.json"

# cf_clearance'in ömrünü site belirler (çoğu kez saatler). Kesin süreyi bilemeyiz;
# bayat kayıt biriktirmemek için 24 saatten eski kayıtlar atılır, gerçek geçerlilik
# kararını ise siteye sorup ChallengeError'a bakarak veriyoruz.
_CF_TTL = 24 * 3600


def load() -> dict:
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(**kv) -> None:
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        data = load()
        data.update(kv)
        _FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# --- Cloudflare cf_clearance kalıcılığı ----------------------------------
def _host(url_or_host: str) -> str:
    """'https://www.site.nl/x' ya da 'www.site.nl' -> 'www.site.nl'."""
    s = (url_or_host or "").strip()
    return (urlparse(s).netloc or s).lower()


def save_cf(host: str, cookie: str, user_agent: str) -> None:
    """cf_clearance cookie'sini + onu veren UA'yı siteye göre saklar.

    Cookie ile UA AYRILMAZ: Cloudflare cf_clearance'i onu aldığı tarayıcının
    UA'sına bağlar, farklı UA ile gönderilirse geçersiz sayar. Bu yüzden ikisi
    tek kayıtta birlikte tutulur ve birlikte geri yüklenir.
    """
    cf = load().get("cf") or {}
    cf[_host(host)] = {"cookie": cookie, "user_agent": user_agent, "ts": time.time()}
    save(cf=cf)


def load_cf(host: str) -> dict | None:
    """Saklanmış cf kaydını döndürür (yoksa/çok eskiyse None)."""
    rec = (load().get("cf") or {}).get(_host(host))
    if not isinstance(rec, dict) or not rec.get("cookie"):
        return None
    if time.time() - rec.get("ts", 0) > _CF_TTL:
        return None
    return rec


def forget_cf(host: str) -> None:
    """Geçersizleşmiş kaydı siler (challenge tekrar geldiğinde çağrılır)."""
    cf = load().get("cf") or {}
    if cf.pop(_host(host), None) is not None:
        save(cf=cf)
