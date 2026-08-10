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


class ChallengeError(BlockedError):
    """Cloudflare JS challenge ("Just a moment...") — TLS taklidiyle GEÇİLEMEZ.

    403'ün iki ayrı sebebi var ve karıştırılmaları teşhisi yanıltıyor:
      * TLS/JA3 parmak izi reddi → curl-cffi impersonate ile çözülür, tekrar
        denemek anlamlı.
      * JS challenge (cf-mitigated: challenge) → çözümü tarayıcıda JS
        çalıştırmayı gerektirir; tekrar denemek yalnızca zaman kaybı ve siteye
        gereksiz istek. Bu sınıf o durumu ayırır: retry YAPILMAZ, kullanıcıya
        ne yapacağı söylenir (bkz. --cf-cookie).
    """


def _is_challenge(resp) -> bool:
    """Yanıt bir Cloudflare JS challenge sayfası mı?"""
    if (resp.headers.get("cf-mitigated") or "").lower() == "challenge":
        return True
    if resp.status_code not in (403, 503):
        return False
    try:
        head = resp.text[:4000]
    except Exception:
        return False
    return "_cf_chl_opt" in head or "cf-browser-verification" in head


class Network:
    def __init__(self, session: SessionState):
        self.session = session
        kwargs = {"impersonate": session.impersonate}
        if session.proxy:
            kwargs["proxies"] = {"http": session.proxy, "https": session.proxy}
        self._client = requests.Session(**kwargs)
        # SessionState'te önceden verilmiş cookie'ler (ör. tarayıcıdan alınan
        # cf_clearance, bkz. --cf-cookie) client'a ekilir — yoksa yalnızca
        # istemciden session'a tek yönlü akar ve elle verilen cookie hiç gitmez.
        for name, value in (session.cookies or {}).items():
            try:
                self._client.cookies.set(name, value)
            except Exception:
                pass
        # User-Agent, SessionState'ten (kimlik rotasyonuyla değişebilir) alınır —
        # config.DEFAULT_HEADERS'taki sabit değeri DEĞİL, çünkü curl-cffi ve
        # proxy/ffmpeg'in aynı UA'yı göndermesi kritik (bkz. session.py).
        headers = dict(config.DEFAULT_HEADERS)
        headers["User-Agent"] = session.user_agent
        self._client.headers.update(headers)

    # --- Genel API --------------------------------------------------------
    def get(self, url: str, referer: str | None = None,
            retries: int | None = None, timeout: int | None = None, **kwargs):
        return self._request("GET", url, referer=referer, retries=retries,
                             timeout=timeout, **kwargs)

    def post(self, url: str, referer: str | None = None,
             retries: int | None = None, timeout: int | None = None, **kwargs):
        return self._request("POST", url, referer=referer, retries=retries,
                             timeout=timeout, **kwargs)

    # --- İç mantık --------------------------------------------------------
    def _sync_cookies(self) -> None:
        try:
            for name, value in self._client.cookies.items():
                self.session.cookies[name] = value
        except Exception:
            pass

    def _request(self, method: str, url: str, referer: str | None = None,
                 retries: int | None = None, timeout: int | None = None, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Referer", referer or self.session.referer)

        # `retries`/`timeout` yalnızca "bu istek zaten büyük ihtimalle boşa
        # gidecek" durumlar için (bkz. resolver domain taraması: olmayan bir
        # adresi 3 kez, 20 sn timeout'la denemek aday başına saniyeler yakıyor).
        # Verilmezlerse davranış eskisiyle birebir aynı.
        attempts = max(1, retries or config.MAX_RETRIES)
        wait = timeout or config.REQUEST_TIMEOUT

        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = self._client.request(
                    method,
                    url,
                    headers=headers,
                    timeout=wait,
                    allow_redirects=True,
                    **kwargs,
                )
            except Exception as e:  # ağ hatası → tekrar dene
                last_exc = e
                if attempt < attempts:   # son denemeden sonra beklemek boşuna
                    time.sleep(random.uniform(0.85, 1.4) * attempt)
                continue

            self._sync_cookies()

            if _is_challenge(resp):
                raise ChallengeError(
                    f"Cloudflare JS challenge @ {url} — bu 403 TLS parmak izi "
                    "bloğu değil, tarayıcıda JS çalıştırmayı gerektiren bir "
                    "doğrulama; tarayıcısız geçilemez. Tarayıcıda siteyi bir kez "
                    "açıp cf_clearance cookie'sini alıp "
                    "--cf-cookie ve --user-agent ile verebilirsin (aynı IP + aynı "
                    "UA şart), ya da --tor / VPN ile farklı bir çıkış IP'si dene."
                )

            if resp.status_code in (403, 429, 503):
                last_exc = BlockedError(f"HTTP {resp.status_code} @ {url}")
                if attempt < attempts:
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
