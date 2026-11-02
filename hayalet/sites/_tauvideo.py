"""TauVideo (tau-video.xyz) oynatıcı-host çözücüsü — anime fansub kaynağı.

Site adapter'ı DEĞİL; `tau-video.xyz/embed/<id>` embed'ini DOĞRUDAN MP4 adresine
çeviren paylaşılan yardımcı (animecix ve diğer anime siteleri kullanır).

Şema (canlı doğrulandı, 2026-11): embed sayfası boş bir SPA kabuğu (1.3 KB); asıl
veri ayrı bir JSON ucunda:
    GET https://tau-video.xyz/api/video/<id>
    -> {"urls":[{"label":"1080p","url":"https://*.cfd/file/tau-video/..._1080p.mp4"},
                {"label":"720p",...},{"label":"480p",...}], ...}
Adresler DÜZ PROGRESSIVE MP4'tür (HLS değil). CDN tamamen açık: Referer gerekmez,
206/Range destekler → tarayıcı native oynatır, proxy gerekmez. Anime altyazısı
videoya gömülüdür; ayrı altyazı kanalı yoktur.
"""
from __future__ import annotations

import re

from hayalet.core.extractor import ExtractError
from hayalet.core.network import Network

_ID_RE = re.compile(r"/embed/([0-9a-fA-F]+)")

# Kalite tercih sırası (yüksekten düşüğe); listede olan ilk etiket seçilir.
_QUALITY_ORDER = ["2160p", "1440p", "1080p", "720p", "480p", "360p", "240p"]


def _quality_key(label: str) -> int:
    """Etiketten çözünürlük (yükseklik) çıkarır; bilinmiyorsa 0."""
    m = re.search(r"(\d{3,4})p?", label or "")
    return int(m.group(1)) if m else 0


def resolve(net: Network, embed_url: str, page_referer: str | None = None) -> str:
    """TauVideo embed'inden EN YÜKSEK kaliteli doğrudan MP4 adresini döndürür."""
    if embed_url.startswith("//"):
        embed_url = "https:" + embed_url
    m = _ID_RE.search(embed_url)
    if not m:
        raise ExtractError(f"tauvideo: embed kimliği bulunamadı: {embed_url[:60]}")
    vid = m.group(1)
    api = f"https://tau-video.xyz/api/video/{vid}"
    try:
        r = net.get(api, referer=page_referer or embed_url)
        data = r.json()
    except Exception as e:
        raise ExtractError(f"tauvideo: api yanıtı çözülemedi: {e}")
    urls = data.get("urls") or []
    if not urls:
        raise ExtractError("tauvideo: kaynak adresi yok (video silinmiş olabilir).")
    # En yüksek çözünürlük (etiketten); hiç sayı yoksa listedeki ilk.
    best = max(urls, key=lambda u: _quality_key(u.get("label", "")))
    url = best.get("url")
    if not url:
        raise ExtractError("tauvideo: seçilen kaynakta adres yok.")
    return url
