"""Uzaktan ayar — kırılgan site sabitlerini (domain / endpoint / regex / kripto
anahtarı) APK YAYINLAMADAN güncellemek için.

Mantık: bu değerler koda gömülü yerine, herkese açık (public) küçük bir JSON'da
durur (ör. bir GitHub Gist). Uygulama açılışta onu çeker, `.cache`'e yazar ve
`remote.get("anahtar", gömülü_varsayılan)` ile okutur. Bir site bozulunca o tek
dosya düzenlenir → tüm kullanıcılar bir sonraki açılışta düzelir, APK gerekmez.

GÜVENLİK AĞI: dosya ulaşılamaz / bozuk / URL boşsa HER ZAMAN gömülü varsayılana
düşülür → en kötü durumda bugünkü davranış. Uzaktan KOD çalıştırılmaz, yalnız
düz veri (anahtar→değer) okunur. Çekme ARKA PLANDADIR, açılışı bloklamaz: taze
değer bir sonraki açılışta geçerli olur (önce disk cache, yoksa gömülü).

JSON biçimi — düz anahtar/değer sözlüğü:
    { "selcukflix.key_seed": "!!22xx!!90!!",
      "dizibox.known_domain": "https://dizibox.live",
      "dizisol.search_path": "/api/search?q=" }
Adaptör tarafı:  remote.get("selcukflix.key_seed", "!!22xx!!90!!")
"""
from __future__ import annotations

import json
import os
import threading
import time

# --- KULLANICI AYARI -------------------------------------------------------
# remote_config.json'un herkese açık ham (raw) adresi. BOŞ bırakılırsa uzaktan
# ayar KAPALIDIR ve her şey gömülü varsayılanlarla çalışır (bugünkü davranış).
# Örnek (GitHub Gist):
#   https://gist.githubusercontent.com/<kullanici>/<gist_id>/raw/remote_config.json
REMOTE_CONFIG_URL = "https://gist.githubusercontent.com/Ahweterenn/96f95474dd50740ee8b180e00fc99e1a/raw/remote_config.json"

_TTL = 3 * 3600          # taze sayılma süresi; sonra arka planda tazelenir
_CACHE_NAME = "remote_config.json"
_TIMEOUT = 8

_lock = threading.Lock()
_data: dict | None = None     # yüklü ayar; None = henüz belleğe alınmadı
_loaded_ts = 0.0
_refreshing = False


def _cache_path() -> str:
    from hayalet import config
    return os.path.join(str(config.CACHE_DIR), _CACHE_NAME)


def _read_cache() -> tuple[dict, float]:
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            p = json.load(f)
        d = p.get("data")
        return (d if isinstance(d, dict) else {}), float(p.get("ts", 0))
    except Exception:
        return {}, 0.0


def _write_cache(data: dict, ts: float) -> None:
    try:
        from hayalet import config
        os.makedirs(str(config.CACHE_DIR), exist_ok=True)
        tmp = _cache_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": ts, "data": data}, f, ensure_ascii=False)
        os.replace(tmp, _cache_path())
    except Exception:
        pass


def _fetch() -> dict | None:
    """Uzaktaki JSON'u çeker; başarısızsa None (çağıran gömülü değere düşer)."""
    if not REMOTE_CONFIG_URL:
        return None
    try:
        from curl_cffi import requests
        r = requests.get(REMOTE_CONFIG_URL, impersonate="chrome", timeout=_TIMEOUT)
        if r.status_code == 200:
            d = r.json()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return None


def _refresh_bg() -> None:
    global _data, _loaded_ts, _refreshing
    fresh = _fetch()
    if fresh is not None:
        with _lock:
            _data = fresh
            _loaded_ts = time.time()
        _write_cache(fresh, _loaded_ts)
    with _lock:
        _refreshing = False


def _ensure() -> None:
    """Belleğe disk cache'i alır (anında) ve bayatsa ARKA PLANDA tazeler.
    Açılışı asla bloklamaz."""
    global _data, _loaded_ts, _refreshing
    with _lock:
        if _data is None:
            _data, _loaded_ts = _read_cache()
        stale = time.time() - _loaded_ts > _TTL
        if stale and not _refreshing and REMOTE_CONFIG_URL:
            _refreshing = True
            threading.Thread(target=_refresh_bg, daemon=True).start()


def get(key: str, default=None):
    """Uzaktan ayardan `key`; yoksa / ulaşılamazsa / bozuksa `default` (gömülü)."""
    try:
        _ensure()
    except Exception:
        return default
    with _lock:
        v = (_data or {}).get(key)
    return v if v is not None else default


def _reset_for_test() -> None:
    """Testler arası bellek durumunu sıfırlar (yalnız testlerde kullanılır)."""
    global _data, _loaded_ts, _refreshing
    with _lock:
        _data, _loaded_ts, _refreshing = None, 0.0, False
