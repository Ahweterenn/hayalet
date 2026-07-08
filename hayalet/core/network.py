"""curl-cffi tabanlı istek katmanı — Cloudflare/anti-bot bypass.

Standart `requests` KULLANILMAZ; gerçek Chrome TLS/JA3 imzasını taklit eden
curl-cffi ile istek atılır. Alınan cookie'ler SessionState'e senkronlanır.
"""
from __future__ import annotations

import random
import time

from curl_cffi import requests

from hayalet import config
from hayalet.core.session import SessionState


class BlockedError(Exception):
    """Anti-bot tarafından engellendi (403 / 429 / 503 / challenge)."""


class Network:
    def __init__(self, session: SessionState):
        self.session = session
        kwargs = {"impersonate": session.impersonate}
        if session.proxy:
            kwargs["proxies"] = {"http": session.proxy, "https": session.proxy}
        self._client = requests.Session(**kwargs)
        # User-Agent, SessionState'ten (kimlik rotasyonuyla değişebilir) alınır —
        # config.DEFAULT_HEADERS'taki sabit değeri DEĞİL, çünkü curl-cffi ve
        # proxy/ffmpeg'in aynı UA'yı göndermesi kritik (bkz. session.py).
        headers = dict(config.DEFAULT_HEADERS)
        headers["User-Agent"] = session.user_agent
        self._client.headers.update(headers)

    # --- Genel API --------------------------------------------------------
    def get(self, url: str, referer: str | None = None, **kwargs):
        return self._request("GET", url, referer=referer, **kwargs)

    def post(self, url: str, referer: str | None = None, **kwargs):
        return self._request("POST", url, referer=referer, **kwargs)

    # --- İç mantık --------------------------------------------------------
    def _sync_cookies(self) -> None:
        try:
            for name, value in self._client.cookies.items():
                self.session.cookies[name] = value
        except Exception:
            pass

    def _request(self, method: str, url: str, referer: str | None = None, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Referer", referer or self.session.referer)

        last_exc: Exception | None = None
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                resp = self._client.request(
                    method,
                    url,
                    headers=headers,
                    timeout=config.REQUEST_TIMEOUT,
                    allow_redirects=True,
                    **kwargs,
                )
            except Exception as e:  # ağ hatası → tekrar dene
                last_exc = e
                time.sleep(random.uniform(0.85, 1.4) * attempt)
                continue

            self._sync_cookies()

            if resp.status_code in (403, 429, 503):
                last_exc = BlockedError(f"HTTP {resp.status_code} @ {url}")
                time.sleep(random.uniform(1.1, 1.9) * attempt)
                continue

            return resp

        if isinstance(last_exc, BlockedError):
            raise last_exc
        raise BlockedError(f"İstek başarısız: {url} ({last_exc})")

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
