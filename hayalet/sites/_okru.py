"""ok.ru (Odnoklassniki) video embed çözücüsü — birden çok sitede ortak.

Site adapter'ı DEĞİL; `//ok.ru/videoembed/<id>` biçimli bir embed'i HLS master'a
çeviren paylaşılan yardımcı. ok.ru'yu oynatıcı olarak kullanan Türk siteleri
(dizimay, dizitivi ...) bunu çağırır.

Şema (canlı doğrulandı, 2026-11):
  POST /dk?cmd=videoPlayerMetadata&mid=<id>  -> JSON, hlsManifestUrl (master m3u8)
Bu uç, embed sayfasındaki `data-options` gömülü OLMASA da çalışır (bazı videolarda
gömülü, bazılarında değil); gömülü varsa yedek olarak o da okunur. Dönen master
IP'ye bağlıdır; medya istekleri `https://ok.ru/` referer'ını ister.
"""
from __future__ import annotations

import html
import json
import re

from hayalet.core.extractor import ExtractError
from hayalet.core.network import Network

_ID_RE = re.compile(r"/video(?:embed)?/(\d+)")
_OPTS_RE = re.compile(r'data-options="([^"]+)"')
_REFERER = "https://ok.ru/"


def _abs(url: str) -> str:
    return "https:" + url if url.startswith("//") else url


def _from_metadata(meta: dict) -> str | None:
    if isinstance(meta, dict):
        return meta.get("hlsManifestUrl") or meta.get("hlsMasterPlaylistUrl")
    return None


def resolve(net: Network, embed_url: str, page_referer: str) -> tuple[str, str]:
    """ok.ru embed'inden (master_m3u8, medya_referer) döndürür."""
    embed_url = _abs(embed_url)
    m = _ID_RE.search(embed_url)
    if not m:
        raise ExtractError("ok.ru: video kimliği bulunamadı.")
    vid = m.group(1)

    # 1) Standart metadata ucu — en güvenilir yol.
    try:
        r = net.post("https://ok.ru/dk",
                     params={"cmd": "videoPlayerMetadata", "mid": vid},
                     referer=embed_url)
        j = json.loads(r.text)
        hls = _from_metadata(j)
        if hls:
            return hls, _REFERER
    except Exception:
        pass

    # 2) Yedek: embed sayfasındaki gömülü data-options.
    try:
        t = net.get(embed_url, referer=page_referer).text
        mo = _OPTS_RE.search(t)
        if mo:
            opts = json.loads(html.unescape(mo.group(1)))
            hls = _from_metadata(opts.get("flashvars", {}).get("metadata"))
            if hls:
                return hls, _REFERER
    except Exception:
        pass

    raise ExtractError("ok.ru: hlsManifestUrl bulunamadı (video kaldırılmış/kısıtlı olabilir).")
