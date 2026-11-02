"""Uqload (uqload.io/.net/.cx…) oynatıcı-host çözücüsü — anime fansub fallback.

Site adapter'ı DEĞİL; `uqload.XX/embed-<id>.html` embed'ini HLS master'a çeviren
paylaşılan yardımcı (animecix'te tau-video yoksa yedek).

Şema (canlı doğrulandı, 2026-11): embed sayfası bir Dean Edwards "packer"
(`eval(function(p,a,c,k,e,d){…}('…',36,409,'…'.split('|')))`) içerir; açılınca
`jwplayer(...).setup({sources:[{file:"https://…/master.m3u8?t=…"}]})` çıkar.
Adres HLS'tir (mp4 değil) ve IP+süre imzalıdır → aynı IP'den (oynatıcı/proxy)
geçerlidir, birkaç saat sonra düşer. Opsiyonel `.vtt` altyazı da taşınır.
Packer açma mantığı hdfilmcehennemi ile ORTAK (`_packer_unpack`), yeniden yazılmaz.
"""
from __future__ import annotations

import re

from hayalet.core.extractor import ExtractError
from hayalet.core.network import Network, media_headers
from hayalet.sites.hdfilmcehennemi_adapter import (_packer_unpack,
                                                   _unescape_js_single_quoted)

# Packer çağrısı: }('<payload>',<a>,<c>,'<k>'.split('|')  — host/sürümden bağımsız.
_PACKED_RE = re.compile(
    r"\}\s*\(\s*'(.*?)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'(.*?)'\.split\('\|'\)", re.S)
_FILE_RE = re.compile(r'file\s*:\s*["\'](https?://[^"\']+?\.m3u8[^"\']*)', re.I)
_VTT_RE = re.compile(r'["\'](https?://[^"\']+?\.vtt[^"\']*)', re.I)


def _extract(unpacked_js: str) -> tuple[str, str | None]:
    """Açılmış jwplayer kurulumundan (master_m3u8, altyazı_vtt|None)."""
    m = _FILE_RE.search(unpacked_js)
    if not m:
        raise ExtractError("uqload: m3u8 adresi bulunamadı (şema değişmiş olabilir).")
    sub = _VTT_RE.search(unpacked_js)
    return m.group(1), (sub.group(1) if sub else None)


def resolve(net: Network, embed_url: str,
            page_referer: str | None = None) -> tuple[str, str, str | None]:
    """Uqload embed'inden (master_m3u8, medya_referer, altyazı|None) döndürür."""
    if embed_url.startswith("//"):
        embed_url = "https:" + embed_url
    origin = re.match(r"(https?://[^/]+)", embed_url)
    referer = origin.group(1) + "/" if origin else (page_referer or embed_url)
    try:
        html = net.get(embed_url, referer=page_referer or referer).text
    except Exception as e:
        raise ExtractError(f"uqload: embed alınamadı: {e}")
    pk = _PACKED_RE.search(html)
    if not pk:
        raise ExtractError("uqload: packer bloğu bulunamadı (video silinmiş olabilir).")
    unpacked = _packer_unpack(_unescape_js_single_quoted(pk.group(1)),
                              int(pk.group(2)), int(pk.group(3)), pk.group(4).split("|"))
    master, sub = _extract(unpacked)
    return master, referer, sub
