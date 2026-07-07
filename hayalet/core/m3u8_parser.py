"""Master m3u8 -> çözünürlük ayrıştırma ve seçim."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

import m3u8

from hayalet.core.network import Network
from hayalet.core.session import SessionState


@dataclass
class Variant:
    url: str
    height: int          # 1080, 720 ... (0 = bilinmiyor)
    bandwidth: int

    @property
    def label(self) -> str:
        q = f"{self.height}p" if self.height else "otomatik"
        mbps = self.bandwidth / 1_000_000 if self.bandwidth else 0
        return f"{q}" + (f"  (~{mbps:.1f} Mbps)" if mbps else "")


@dataclass
class AudioTrack:
    url: str
    lang: str            # 'tur', 'eng', '' ...
    name: str            # rendition adı (ör. "Türkçe", "Orijinal")
    is_turkish: bool


def list_variants(net: Network, session: SessionState, m3u8_url: str,
                  referer: str) -> list[Variant]:
    text = net.get(m3u8_url, referer=referer).text
    pl = m3u8.loads(text, uri=m3u8_url)
    if not pl.playlists:
        return []   # media playlist (tek kalite)
    variants: list[Variant] = []
    for p in pl.playlists:
        si = p.stream_info
        res = si.resolution if si else None
        height = res[1] if res else 0
        uri = p.uri if p.uri.startswith("http") else urljoin(m3u8_url, p.uri)
        variants.append(Variant(url=uri, height=height,
                                bandwidth=si.bandwidth or 0 if si else 0))
    variants.sort(key=lambda v: (v.height, v.bandwidth), reverse=True)
    return variants


def pick_best(variants: list[Variant]) -> Variant:
    return variants[0]


def _pick_by_quality(variants: list[Variant], quality: str | None) -> Variant:
    if not variants:
        return None
    if not quality or quality == "best":
        return variants[0]
    if quality == "worst":
        return variants[-1]
    try:
        h = int(str(quality).rstrip("p"))
        for v in variants:
            if v.height == h:
                return v
    except ValueError:
        pass
    return variants[0]


def get_av_urls(net: Network, session: SessionState, master_url: str,
                referer: str, quality: str | None = "best"
                ) -> tuple[str, list[AudioTrack]]:
    """Master'dan (video varyantı, ayrı ses kanalları) URL'lerini döndürür.

    Ayrı ses rendition'ları varsa TÜMÜ döndürülür (Türkçe önce) → indirmede
    dual-audio olarak gömülür. Ses videoya gömülüyse liste boş döner.
    """
    from urllib.parse import urljoin
    from hayalet.core.utils import looks_turkish

    text = net.get(master_url, referer=referer).text
    pl = m3u8.loads(text, uri=master_url)

    # --- Video varyantı ---
    if pl.playlists:
        variants = []
        for p in pl.playlists:
            si = p.stream_info
            res = si.resolution if si else None
            height = res[1] if res else 0
            uri = p.uri if p.uri.startswith("http") else urljoin(master_url, p.uri)
            variants.append(Variant(url=uri, height=height,
                                    bandwidth=(si.bandwidth or 0) if si else 0))
        variants.sort(key=lambda v: (v.height, v.bandwidth), reverse=True)
        video_url = _pick_by_quality(variants, quality).url
    else:
        video_url = master_url   # tek media playlist

    # --- Ayrı ses kanalları (varsa) → hepsi, Türkçe önce ---
    tracks: list[AudioTrack] = []
    auds = [m for m in pl.media if (m.type or "").upper() == "AUDIO" and m.uri]
    for m in auds:
        is_tr = ((m.language or "").lower().startswith("tr")
                 or looks_turkish(m.name or ""))
        url = m.absolute_uri or urljoin(master_url, m.uri)
        tracks.append(AudioTrack(url=url, lang=(m.language or ""),
                                 name=(m.name or ""), is_turkish=is_tr))
    # Türkçe ilk sırada (varsayılan ses o olsun); geri kalanı kaynak sırasında.
    tracks.sort(key=lambda t: not t.is_turkish)

    return video_url, tracks
