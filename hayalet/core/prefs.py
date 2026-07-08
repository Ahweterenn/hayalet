"""Kullanıcı tercihi kalıcılığı — .cache/prefs.json.

Şu an tek alan: son seçilen video kalitesi. Bir sonraki çalıştırmada
(interaktif menüde varsayılan olarak öne çıkar / non-interaktif'te
--quality verilmezse) bu değer kullanılır.
"""
from __future__ import annotations

import json

from hayalet import config

_FILE = config.CACHE_DIR / "prefs.json"


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
