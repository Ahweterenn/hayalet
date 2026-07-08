"""SessionState — tüm katmanların paylaştığı tek kimlik nesnesi.

curl-cffi ile alınan cookie + user-agent + referer burada tutulur ve
proxy/ffmpeg'e birebir aynısı aktarılır (anti-bot için kritik).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from hayalet import config


@dataclass
class SessionState:
    base_url: str = config.KNOWN_DOMAIN
    cookies: dict[str, str] = field(default_factory=dict)
    user_agent: str = config.USER_AGENT
    referer: str = config.KNOWN_DOMAIN
    impersonate: str = config.IMPERSONATE
    # http(s)://... veya socks5h://127.0.0.1:9050 (Tor) gibi bir proxy URL'si;
    # None ise doğrudan bağlanılır. curl-cffi Session'a olduğu gibi geçirilir.
    proxy: str | None = None

    def summary(self) -> str:
        return (
            f"base_url={self.base_url}  "
            f"cookies={len(self.cookies)}  "
            f"ua={self.user_agent[:32]}…"
        )
