"""Dublaj + orijinal/altyazılı kaynakları tek akışta birleştirme.

Site aynı içeriği 'X' ve 'X Türkçe Dublaj' olarak ayrı listeler. Bu modül ikisini
de çözer ve tek bir akış tanımı (MergedStream) üretir:

    video  +  [Türkçe Dublaj sesi, Orijinal ses]  +  Türkçe altyazı

Böylece hem izleme (hls.js sentetik master) hem indirme (ffmpeg çok-giriş) aynı
kaynaktan ses dili ve altyazı seçtirir. Bir sürüm (genelde 'Türkçe Dublaj')
ölü/park edilmişse diğeriyle yetinir (graceful).
"""
from __future__ import annotations

from dataclasses import dataclass

from hayalet.core import catalog, extractor
from hayalet.core.catalog import Episode, Series
from hayalet.core.m3u8_parser import get_av_urls
from hayalet.core.network import Network
from hayalet.core.session import SessionState


@dataclass
class AudioSource:
    url: str          # ses media playlist'i (gömülüyse kaynağın video media playlist'i)
    referer: str
    lang: str
    name: str
    is_turkish: bool
    is_default: bool = False


@dataclass
class MergedStream:
    video_master_url: str
    video_referer: str
    audios: list[AudioSource]
    subtitle_url: str | None = None
    subtitle_referer: str | None = None


def _resolve(net, session, bolum_url):
    """Bölüm URL'sini çöz → (StreamInfo, video_media_url, audio_tracks) | None.

    Ölü/park edilmiş veya çözülemeyen kaynaklar için None döner (birleştirme yine
    diğer sürümle sürer).
    """
    try:
        si = extractor.extract_stream(net, session, bolum_url)
    except extractor.ExtractError:
        return None
    try:
        video_url, tracks = get_av_urls(net, session, si.m3u8_url, si.referer, "best")
    except Exception:
        video_url, tracks = si.m3u8_url, []
    return si, video_url, tracks


def _counterpart_episode(net, session, series, season, number):
    if not series:
        return None
    eps = catalog.get_episodes(net, session, series)
    return catalog.match_episode(eps, season, number)


def _audios_from(src, default_name: str, force_tr: bool | None):
    """Bir kaynağın ses kanallarını AudioSource listesine çevirir.

    Ayrı ses rendition'ları varsa her biri; yoksa (gömülü) kaynağın video media
    playlist'i tek ses olarak. force_tr=True → hepsi Türkçe kabul (dublaj kaynağı).
    """
    si, video_url, tracks = src
    out: list[AudioSource] = []
    if tracks:
        for t in tracks:
            is_tr = t.is_turkish if force_tr is None else force_tr
            out.append(AudioSource(
                url=t.url, referer=si.referer,
                lang=t.lang or ("tr" if is_tr else "und"),
                name=t.name or default_name, is_turkish=is_tr))
    else:
        is_tr = bool(force_tr)
        out.append(AudioSource(
            url=video_url, referer=si.referer,
            lang="tr" if is_tr else "und", name=default_name, is_turkish=is_tr))
    return out


def build_merged(net: Network, session: SessionState,
                 episode: Episode, series: Series) -> MergedStream:
    # Sürümleri sınıflandır ve karşı sürümün bölümünü bul
    if catalog.is_dubbed(series):
        dub_series = series
        orig_series = catalog.find_counterpart(net, session, series, want_dubbed=False)
        dub_ep = episode
        orig_ep = _counterpart_episode(net, session, orig_series,
                                       episode.season, episode.number)
    else:
        orig_series = series
        dub_series = catalog.find_counterpart(net, session, series, want_dubbed=True)
        orig_ep = episode
        dub_ep = _counterpart_episode(net, session, dub_series,
                                      episode.season, episode.number)

    orig = _resolve(net, session, orig_ep.url) if orig_ep else None
    dub = _resolve(net, session, dub_ep.url) if dub_ep else None

    if not orig and not dub:
        # Seçilen kaynağı doğrudan çözmeyi dene → anlamlı hata yükselsin
        extractor.extract_stream(net, session, episode.url)
        raise extractor.ExtractError("Kaynak çözülemedi.")

    # Video + altyazı: orijinal varsa ondan (orijinal kalite + TR altyazı), yoksa dub
    vsrc = orig or dub
    vsi = vsrc[0]
    video_master, video_ref = vsi.m3u8_url, vsi.referer
    sub_url = (orig[0].subtitle_url if orig else None) or vsi.subtitle_url
    sub_ref = orig[0].referer if orig else vsi.referer

    # Ses kanalları: Türkçe Dublaj (varsa) önce = varsayılan, sonra orijinal
    audios: list[AudioSource] = []
    if dub:
        audios += _audios_from(dub, "Türkçe Dublaj", force_tr=True)
    if orig:
        audios += _audios_from(orig, "Orijinal", force_tr=None)
    if not audios:
        audios += _audios_from(vsrc, "Ses", force_tr=None)

    # Aynı URL'yi iki kez ekleme
    seen: set[str] = set()
    uniq: list[AudioSource] = []
    for a in audios:
        if a.url in seen:
            continue
        seen.add(a.url)
        uniq.append(a)
    audios = uniq

    for a in audios:
        a.is_default = False
    if audios:
        audios[0].is_default = True

    return MergedStream(video_master_url=video_master, video_referer=video_ref,
                        audios=audios, subtitle_url=sub_url, subtitle_referer=sub_ref)
