"""pichive / NP_CONFIG oynatıcı ailesi çözücüsü — birden çok sitede ortak.

Bu bir site adapter'ı DEĞİL; `iframe.php?v=HASH` biçimli bir pichive oynatıcı
çerçevesini çözen paylaşılan yardımcı. Aynı aileyi kullanan siteler (asyawatch,
selcukflix ve benzerleri) bunu çağırır — KekikStream'in "Extractors" ayrımı gibi.

Şema (canlı doğrulandı, 2026-11, şifre YOK):
  GET //HOST/iframe.php?v=HASH   -> HTML içinde `window.NP_CONFIG = {...}` JSON
  NP_CONFIG.sources[]  -> {title, kind, master}   master = //HOST/nm.php?v=...  (çok sesli m3u8)
  NP_CONFIG.subtitles[] -> {label, lang, tok}      altyazı = //HOST/nsub.php?v=<tok>
Medya istekleri pichive host'unu Referer+Origin ister (bkz. media_headers).
"""
from __future__ import annotations

import json
import re

from hayalet.core.extractor import ExtractError
from hayalet.core.network import Network, media_headers

_NP_RE = re.compile(r"NP_CONFIG\s*=\s*(\{.*?\})\s*;", re.S)


def _abs(url: str) -> str:
    """`//host/...` -> `https://host/...`; tam adres aynen kalır."""
    if url.startswith("//"):
        return "https:" + url
    return url


def _origin(url: str) -> str:
    m = re.match(r"(https?://[^/]+)", url)
    return m.group(1) if m else url


def resolve(net: Network, iframe_url: str, page_referer: str) -> tuple[str, str, str | None]:
    """pichive iframe'inden (master_m3u8, medya_referer, altyazı_url|None) döndürür.

    Birden çok kaynak varsa ilki alınır (çoğu zaman 'Orijinal', çok sesli master —
    Türkçe + orijinal ses tek master'da; ses kanallarını get_av_urls ayıklar).
    """
    iframe_url = _abs(iframe_url)
    host = _origin(iframe_url)
    referer = host + "/"
    try:
        t = net.get(iframe_url, referer=page_referer,
                    headers=media_headers(page_referer)).text
    except Exception as e:
        raise ExtractError(f"pichive: iframe alınamadı: {e}")
    m = _NP_RE.search(t)
    if not m:
        raise ExtractError("pichive: NP_CONFIG bulunamadı (oynatıcı değişmiş olabilir).")
    try:
        cfg = json.loads(m.group(1))
    except ValueError as e:
        raise ExtractError(f"pichive: NP_CONFIG çözülemedi: {e}")

    sources = [s for s in (cfg.get("sources") or []) if s.get("master")]
    if not sources:
        raise ExtractError("pichive: oynatılacak kaynak yok.")
    master = _abs(sources[0]["master"])

    # Altyazı: Türkçe olanı tercih et, yoksa ilki.
    subtitle_url = None
    subs = cfg.get("subtitles") or []
    tr = next((s for s in subs if (s.get("lang") or "").lower().startswith("tr")), None)
    chosen = tr or (subs[0] if subs else None)
    if chosen and chosen.get("tok"):
        subtitle_url = f"{host}/nsub.php?v={chosen['tok']}"
    return master, referer, subtitle_url
