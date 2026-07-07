"""Dynamic Domain Resolver — güncel dizipalXXXX.com adresini bulur.

Sıra: önbellek -> bilinen domain -> artan sayı taraması. Bulunan domain
SessionState.base_url'e yazılır ve .cache/domain.json'a kaydedilir.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from hayalet import config
from hayalet.core.network import Network, BlockedError
from hayalet.core.session import SessionState

_CACHE = config.CACHE_DIR / "domain.json"
_CACHE_TTL = 6 * 3600   # 6 saat


def _looks_real(html: str) -> bool:
    low = html.lower()
    return "dizipal" in low and any(w in low for w in ("dizi", "bolum", "film"))


def _try(net: Network, url: str) -> bool:
    try:
        r = net.get(url, referer=url)
        return r.status_code == 200 and _looks_real(r.text)
    except (BlockedError, Exception):
        return False


def _cached() -> str | None:
    try:
        d = json.loads(_CACHE.read_text(encoding="utf-8"))
        if time.time() - d.get("ts", 0) < _CACHE_TTL:
            return d.get("domain")
    except Exception:
        return None
    return None


def _save(domain: str) -> None:
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps({"domain": domain, "ts": time.time()}),
                          encoding="utf-8")
    except Exception:
        pass


def resolve(net: Network, session: SessionState, override: str | None = None,
            use_cache: bool = True, scan: int = 8) -> str:
    candidates: list[str] = []
    if override:
        candidates.append(override.rstrip("/"))
    if use_cache:
        c = _cached()
        if c:
            candidates.append(c)
    candidates.append(config.KNOWN_DOMAIN)

    # Artan sayı taraması: dizipalNNNN -> NNNN+scan
    m = re.search(r"(dizipal)(\d+)(\.com)", config.KNOWN_DOMAIN)
    if m:
        base_n = int(m.group(2))
        for n in range(base_n, base_n + scan + 1):
            candidates.append(f"https://{m.group(1)}{n}{m.group(3)}")

    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        if _try(net, url):
            session.base_url = url
            session.referer = url
            _save(url)
            return url

    # Hiçbiri olmadıysa bilineni döndür (yine de denensin)
    session.base_url = config.KNOWN_DOMAIN
    return config.KNOWN_DOMAIN
