""""MolySTREAM/sheila" oynatıcı ailesi çözücüsü — birden çok sitede ortak.

Site adapter'ı DEĞİL; `HOST/embed/<id>` biçimli bir embed'i HLS master'a çeviren
paylaşılan yardımcı. Aynı oynatıcıyı kullanan hostlar: `dbx.molystream.org`
(dizibox) ve `*.popcornvakti.net` (yabancidizi).

Şema (canlı doğrulandı, 2026-11): embed sayfası CryptoJS ile şifreli bir oynatıcı
HTML'i çözüyor, ama o HTML'in `file` alanı deterministik bir "sheila" ucuna
işaret ediyor — şifre çözmeye gerek yok, iki bilinen sıralamadan biri tutuyor:
  molystream   : /embed/sheila/<id>
  popcornvakti : /embed/<id>/sheila
İlk `#EXTM3U` döneni master'dır. Medya istekleri embed sayfasını Referer ister.
"""
from __future__ import annotations

import re

from hayalet.core.extractor import ExtractError
from hayalet.core.network import Network, media_headers

_ID_RE = re.compile(r"/embed/([^/?#]+)")


def _abs(url: str) -> str:
    return "https:" + url if url.startswith("//") else url


def _origin(url: str) -> str:
    m = re.match(r"(https?://[^/]+)", url)
    return m.group(1) if m else url


def resolve(net: Network, embed_url: str, page_referer: str) -> tuple[str, str]:
    """molystream embed'inden (master_m3u8, medya_referer) döndürür."""
    embed_url = _abs(embed_url)
    m = _ID_RE.search(embed_url)
    if not m:
        raise ExtractError("molystream: embed kimliği bulunamadı.")
    vid = m.group(1)
    host = _origin(embed_url)
    # İki bilinen "sheila" sıralaması; ilk #EXTM3U döneni master.
    for master in (f"{host}/embed/sheila/{vid}", f"{host}/embed/{vid}/sheila"):
        try:
            r = net.get(master, referer=embed_url, headers=media_headers(embed_url))
        except Exception:
            continue
        if r.status_code == 200 and r.text.lstrip().startswith("#EXTM3U"):
            return master, embed_url
    raise ExtractError("molystream: master m3u8 bulunamadı (şema değişmiş olabilir).")
