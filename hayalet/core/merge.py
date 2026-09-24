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
from urllib.parse import urljoin

from hayalet.core import catalog, extractor
from hayalet.core.catalog import Episode, Series
from hayalet.core.m3u8_parser import get_av_urls
from hayalet.core.network import Network, media_headers
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


def _resolve_both(net, session, orig_url, dub_url):
    """Orijinal ve dublaj bölümünü AYNI ANDA çözer (eskiden ardışıktı: iki tam
    zincir üst üste biniyordu). Tek bir curl-cffi istemcisi iş parçacıkları
    arasında paylaşılmasın diye ikinci kaynak kendi Network'ünü kullanır."""
    if not (orig_url and dub_url):
        return (_resolve(net, session, orig_url) if orig_url else None,
                _resolve(net, session, dub_url) if dub_url else None)
    import concurrent.futures
    net2 = Network(session)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_orig = ex.submit(_resolve, net, session, orig_url)
        f_dub = ex.submit(_resolve, net2, session, dub_url)
        return f_orig.result(), f_dub.result()


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
    # Gömülü ses zaten video varyantının parçasıdır. Bunu ayrı bir AUDIO
    # rendition'ı gibi ilan etmek Media3 ve bazı HLS oynatıcılarında geçersiz
    # bir master üretir; ayrı kanal yoksa video tek başına oynatılmalıdır.
    return out


_PROBE_SEGMENT_RANGE = "bytes=0-65535"
# Bu boyutun altı segment değil, hata sayfası/yönlendirme demektir.
_PROBE_MIN_BYTES = 8192


def _first_url_line(body: str) -> str | None:
    """Playlist gövdesindeki ilk adres satırı (# ile başlayanlar etiket)."""
    for line in (body or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def _looks_like_media(resp) -> tuple[bool, str]:
    """Yanıt gerçekten medya mı, yoksa hata sayfası mı?

    Sınamanın ilk hâli `Range` ile alınan yanıtı "playlist değilse segmenttir"
    diye kabul ediyordu ve CDN'in HTML hata sayfasını segment sanıyordu:
    telefonda üç TLS profili de "ok, 4096 bayt" dedi ama gerçek zincir yine
    504 veriyordu. Artık tür ve içerik de bakılıyor.
    """
    kod = getattr(resp, "status_code", 200) or 200
    if kod >= 400:
        return False, "segment %d" % kod
    if kod not in (200, 206):
        return False, "segment beklenmeyen durum %d" % kod
    govde = getattr(resp, "content", b"") or b""
    if len(govde) < _PROBE_MIN_BYTES:
        return False, "segment %d bayt (çok küçük)" % len(govde)
    ctype = str(getattr(resp, "headers", {}).get("content-type", "") or "").lower()
    if "html" in ctype or govde[:512].lower().find(b"<html") >= 0:
        return False, "segment yerine HTML (hata sayfası)"
    return True, "%d bayt" % len(govde)


def probe_video(net: Network, master_url: str, referer: str | None,
                timeout: int = 8) -> tuple[bool, str]:
    """Kaynak gerçekten SEGMENT veriyor mu? (veriyor mu, sebep) döner.

    Neden gerekli: site oynatıcı sunucularını döndürüyor ve bazı örneklerinin
    segment CDN'i ölü çıkıyor — playlist 200 geliyor ama segment hiç cevap
    vermiyor. Ölçüldü (2026-09-23, telefonda): master 63 ms, alt playlist
    3,1 MB 743 ms, ilk segment ise 49 sn sonra yanıtsız (bizim 504) ve ikinci
    denemede CDN'den 522 "origin'e ulaşılamıyor". Oynatıcıya böyle bir kaynak
    verilince süre çubuğu doluyor ama görüntü hiç gelmiyor: 0:00'da donuyor,
    sohbete "bağlantı sorunu" düşüyor ve sebep hiçbir yerde görünmüyor.

    Playlist'ler tam alınır (birkaç yüz KB), segmentin ise yalnız ilk
    64 KB'ı istenir — sınama bir kez, kaynak seçiminde yapılır.
    """
    # Origin de gönderilmeli: oynatıcının kendisi gönderiyor ve bazı CDN'ler
    # onsuz sahte 522 dönüyor (bkz. network.media_headers). Sınama oynatıcıdan
    # farklı istek atarsa sağlam kaynağı ölü sanıp reddeder.
    baslik = media_headers(referer)

    def playlist_al(url):
        return net.get(url, referer=referer, retries=1, timeout=timeout,
                       headers=dict(baslik)).text

    try:
        body = playlist_al(master_url)
    except Exception as e:
        return False, "master alınamadı (%s)" % type(e).__name__
    if not (body or "").lstrip().startswith("#EXTM3U"):
        return False, "master playlist değil"

    url = master_url
    for _ in range(3):
        satir = _first_url_line(body)
        if not satir:
            return False, "playlist boş"
        url = urljoin(url, satir)
        # Sıradaki adres yine playlist mi, yoksa segment mi? Playlist'ler
        # metin ve `#EXTM3U` ile başlar; segmenti tam indirmemek için önce
        # menzilli deneyip içeriğe bakıyoruz.
        try:
            r = net.get(url, referer=referer, retries=1, timeout=timeout,
                        headers={**baslik, "Range": _PROBE_SEGMENT_RANGE})
        except Exception as e:
            return False, "segment alınamadı (%s)" % type(e).__name__
        onek = (getattr(r, "content", b"") or b"")[:16].lstrip()
        if onek.startswith(b"#EXTM3U"):
            body = getattr(r, "text", "") or ""
            # Menzil desteklenmiyorsa gövde zaten tam gelmiştir; desteklense
            # bile ilk satırlar elimizde, devam edebiliriz.
            continue
        return _looks_like_media(r)

    return False, "playlist zinciri fazla derin"


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

    orig, dub = _resolve_both(net, session,
                              orig_ep.url if orig_ep else None,
                              dub_ep.url if dub_ep else None)

    if not orig and not dub:
        # Seçilen kaynağı doğrudan çözmeyi dene → anlamlı hata yükselsin
        extractor.extract_stream(net, session, episode.url)
        raise extractor.ExtractError("Kaynak çözülemedi.")

    # Video + altyazı: orijinal varsa ondan (orijinal kalite + TR altyazı), yoksa dub
    vsrc = orig or dub
    # ÖLÜ KAYNAĞI ATLA. Eskiden video her zaman orijinalden alınıyordu ve
    # "çözülebildi" ile "segment veriyor" aynı şey sanılıyordu — oysa site
    # oynatıcı sunucularını döndürüyor, bazılarının segment CDN'i ölü
    # (bkz. probe_video). Böyle bir kaynak oynatıcıda 0:00'da donuyordu.
    # İki sürüm de varsa: seçileni sınayıp vermiyorsa ötekine geçiyoruz.
    if orig and dub and orig is not dub:
        ok, neden = probe_video(net, vsrc[0].m3u8_url, vsrc[0].referer)
        if not ok:
            obur = dub if vsrc is orig else orig
            ok2, neden2 = probe_video(net, obur[0].m3u8_url, obur[0].referer)
            if ok2:
                vsrc = obur
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

    # Aynı URL veya aynı isim/dil kombinasyonunu iki kez ekleme (korsan sitelerde dublaj ve orijinal listesi çift girebiliyor)
    seen_urls: set[str] = set()
    seen_names: set[str] = set()
    uniq: list[AudioSource] = []
    for a in audios:
        clean_name = (a.name or "").strip().lower()
        if a.url in seen_urls:
            continue
        if clean_name and clean_name in seen_names:
            continue
        seen_urls.add(a.url)
        if clean_name:
            seen_names.add(clean_name)
        uniq.append(a)
    audios = uniq

    for a in audios:
        a.is_default = False
    if audios:
        audios[0].is_default = True

    return MergedStream(video_master_url=video_master, video_referer=video_ref,
                        audios=audios, subtitle_url=sub_url, subtitle_referer=sub_ref)
