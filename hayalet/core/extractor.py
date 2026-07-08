"""Bölüm sayfası -> m3u8 + Türkçe altyazı çıkarımı.

Tam zincir (canlı doğrulandı, saf Python / headless):
  1) /bolum/... sayfası -> `data-rm-k` şifreli JSON -> çöz -> iframe URL (dplayer)
  2) iframe -> openPlayer('<playList>') + gömülü TR .vtt
  3) source2.php?v=<playList> -> JSON -> sources[0].file (m.php -> master.m3u8)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from hayalet import config
from hayalet.core import utils
from hayalet.core.network import Network
from hayalet.core.session import SessionState


@dataclass
class StreamInfo:
    m3u8_url: str
    subtitle_url: str | None
    referer: str          # m3u8/proxy/ffmpeg için kullanılacak referer (iframe origin)


class ExtractError(Exception):
    pass


class DeadSourceError(ExtractError):
    """Player host'u ölü/park edilmiş — arkasında video yok (bypass edilemez)."""


def _abs_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    return url


def extract_stream(net: Network, session: SessionState, bolum_url: str) -> StreamInfo:
    # 1) Bölüm sayfası -> data-rm-k -> iframe URL ------------------------
    bhtml = net.get(bolum_url, referer=session.base_url).text
    m = re.search(config.SELECTORS["rmk_block"], bhtml, re.S)
    if not m:
        raise ExtractError("Şifreli kaynak (data-rm-k) bulunamadı.")
    iframe_url = _abs_url(utils.decrypt_rmk(m.group(1)))
    if "http" not in iframe_url:
        raise ExtractError(f"Çözülen değer URL değil: {iframe_url[:60]}")

    origin = f"{urlparse(iframe_url).scheme}://{urlparse(iframe_url).netloc}"

    # 2) iframe -> playList + TR altyazı --------------------------------
    ihtml = net.get(iframe_url, referer=bolum_url).text

    mp = re.search(r"openPlayer\('([^']+)'", ihtml)
    if not mp:
        # Bazı embed'lerde m3u8 doğrudan sayfada olabilir (fallback)
        direct = re.search(r'https?:\\?/\\?/[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*', ihtml)
        if direct:
            return StreamInfo(direct.group(0).replace("\\/", "/"),
                              _find_turkish_vtt(ihtml), iframe_url)
        # Ölü/park edilmiş player host'u: CHEQ → RTB → domain-parking zinciri.
        # (canvascascade.site vb. — genelde "Türkçe Dublaj" embed'lerinde.)
        # Arkasında video yok; bir anti-bot değil, süresi dolmuş kaynak. Tarayıcı
        # da açamaz (bkz. CLAUDE.md — DeadSourceError / CHEQ notu).
        host = urlparse(iframe_url).netloc
        dead_markers = ("oncheqresponse", "cheq", "northwavepoint",
                        "sk-park", "yfdpco1", "canvascascade", "ww38.")
        if any(mk in ihtml.lower() or mk in host.lower() for mk in dead_markers):
            raise DeadSourceError(
                f"Bu bölümün player host'u ({host}) ölü/park edilmiş — arkasında "
                f"video yok (domain parking reklamı). Bu genelde 'Türkçe Dublaj' "
                f"sürümlerde olur; aynı içeriğin 'Orijinal / Altyazılı' sürümünü deneyin."
            )
        raise ExtractError(
            f"Desteklenmeyen oynatıcı ({host}) — m3u8/openPlayer bulunamadı."
        )
    play_list = mp.group(1)
    subtitle_url = _find_turkish_vtt(ihtml)

    # 3) source2.php -> gerçek m3u8 -------------------------------------
    src2 = f"{origin}/source2.php?v={play_list}"
    resp = net.get(src2, referer=iframe_url,
                   headers={"X-Requested-With": "XMLHttpRequest"})
    try:
        data = resp.json()
    except Exception:
        data = json.loads(resp.text)

    if data.get("expired"):
        raise ExtractError("Kaynak süresi doldu (expired) — tekrar deneyin.")

    playlist = data.get("playlist") or []
    if not playlist:
        raise ExtractError("source2.php yanıtında playlist yok.")

    raw_file = playlist[0]["sources"][0]["file"]
    m3u8_url = raw_file.replace("m.php", "master.m3u8")

    return StreamInfo(m3u8_url, subtitle_url, iframe_url)


def _find_turkish_vtt(ihtml: str) -> str | None:
    """iframe HTML'inden Türkçe .vtt altyazısını seç (eklenti sezgisiyle).

    Not: player config JSON'unda URL'ler `\\/` ile kaçışlıdır; önce lang:"tr" /
    Türkçe label ile işaretli olanı dene, sonra kaçışları çözüp URL sezgisine bak.
    """
    # 1) JSON'da açıkça Türkçe işaretli altyazı (kaçışlı HTML üzerinde çalışır)
    for pat in (
        r'"file"\s*:\s*"([^"]+\.vtt[^"]*)"[^}]*?"lang"\s*:\s*"tr"',
        r'"lang"\s*:\s*"tr"[^}]*?"file"\s*:\s*"([^"]+\.vtt[^"]*)"',
        r'"file"\s*:\s*"([^"]+\.vtt[^"]*)"[^}]*?"label"\s*:\s*"[^"]*[Tt]ürk',
    ):
        m = re.search(pat, ihtml)
        if m:
            return m.group(1).replace("\\/", "/")

    # 2) Tüm .vtt adayları (JSON \/ kaçışlarını çöz) → is_turkish_sub sezgisi
    unescaped = ihtml.replace("\\/", "/")
    cands = dict.fromkeys(
        re.findall(r'https?://[^\s"\'<>\\]+\.vtt[^\s"\'<>\\]*', unescaped)
    )
    for c in cands:
        if utils.is_turkish_sub(c):
            return c
    return None
