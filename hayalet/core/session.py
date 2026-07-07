"""SessionState — tüm katmanların paylaştığı tek kimlik nesnesi.

curl-cffi ile alınan cookie + user-agent + referer burada tutulur ve
sonradan mpv / yt-dlp'ye birebir aynısı aktarılır (anti-bot için kritik).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from hayalet import config


@dataclass
class SessionState:
    base_url: str = config.KNOWN_DOMAIN
    cookies: dict[str, str] = field(default_factory=dict)
    user_agent: str = config.USER_AGENT
    referer: str = config.KNOWN_DOMAIN
    impersonate: str = config.IMPERSONATE

    # --- Cookie yardımcıları ---------------------------------------------
    def cookie_header(self) -> str:
        """`k=v; k2=v2` biçiminde Cookie başlığı (mpv/yt-dlp fallback)."""
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def as_mpv_header_fields(self) -> str:
        """mpv `--http-header-fields` için virgülle ayrılmış başlık dizisi."""
        parts = [f"Referer: {self.referer}", f"User-Agent: {self.user_agent}"]
        ch = self.cookie_header()
        if ch:
            parts.append(f"Cookie: {ch}")
        return ",".join(parts)

    def cookies_to_netscape(self, path: str | Path) -> Path:
        """Çerezleri yt-dlp `--cookies` için Netscape formatlı dosyaya yazar."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        host = urlparse(self.base_url).hostname or ""
        domain = host if host.startswith(".") else "." + host
        lines = ["# Netscape HTTP Cookie File", ""]
        for name, value in self.cookies.items():
            # domain  include_subdomains  path  secure  expiry  name  value
            lines.append(
                "\t".join([domain, "TRUE", "/", "TRUE", "0", name, value])
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def summary(self) -> str:
        return (
            f"base_url={self.base_url}  "
            f"cookies={len(self.cookies)}  "
            f"ua={self.user_agent[:32]}…"
        )
