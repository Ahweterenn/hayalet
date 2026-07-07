"""Basit dosya log'u — hatalar/olaylar .cache/hayalet.log'a yazılır (terminali kirletmez).

Kullanım:
    from hayalet.core import logs
    logs.setup()                 # bir kez (cli.main başında)
    logs.log.info("...")
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from hayalet import config

log = logging.getLogger("hayalet")
_ready = False


def setup() -> logging.Logger:
    """Log dosyasını hazırlar (idempotent). Terminale hiçbir şey basmaz."""
    global _ready
    if _ready:
        return log
    try:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            config.CACHE_DIR / "hayalet.log",
            maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False       # kök logger'a (dolayısıyla terminale) sızmasın
    except Exception:
        pass
    _ready = True
    return log
