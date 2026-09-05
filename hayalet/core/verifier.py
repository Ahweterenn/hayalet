import re
import urllib.parse
import urllib.request
import ssl
import certifi
import json
from functools import lru_cache
from urllib.parse import urljoin
from typing import Optional, Tuple

from curl_cffi import requests

from hayalet.core.merge import MergedStream
from hayalet.core.network import Network
from hayalet.core import logs

# ---------------------------------------------------------------------------
# Yardımcı: isim benzerliği
# ---------------------------------------------------------------------------

_TR_MAP = str.maketrans({
    "ç": "c", "Ç": "c",
    "ğ": "g", "Ğ": "g",
    "ı": "i", "I": "i", "İ": "i", "i": "i",
    "ö": "o", "Ö": "o",
    "ş": "s", "Ş": "s",
    "ü": "u", "Ü": "u",
})

def _normalize(s: str) -> str:
    """Küçük harf, Türkçe karakter haritalama, noktalama sil, çoklu boşluğu tekle."""
    if not s:
        return ""
    s = s.translate(_TR_MAP).lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _name_score(query: str, candidate: str) -> float:
    """Kelime ve alt-dize örtüşme skoru (0.0 - 1.0). Film/sinema türevlerini eler."""
    qn = _normalize(query)
    cn = _normalize(candidate)
    if not qn or not cn:
        return 0.0
    if qn == cn:
        return 1.0

    qw = set(qn.split())
    cw = set(cn.split())
    if not qw or not cw:
        return 0.0

    # Dizi ararken sinema filmi / spin-off film adaylarını eler
    for mw in ["film", "filmi", "sinema", "movie", "seni kalbime gomdum", "ankara yaniyor"]:
        if mw in cn and mw not in qn:
            return 0.0

    inter = qw & cw
    q_cov = len(inter) / len(qw)
    c_cov = len(inter) / len(cw)

    score = (q_cov * 0.7) + (c_cov * 0.3)
    if qn in cn:
        score = max(score, 0.85 * (len(qn) / len(cn)))
    return score

# ---------------------------------------------------------------------------
# Yardımcı: tek HTTP getter (curl_cffi → certifi → unverified sırası)
# ---------------------------------------------------------------------------

def _fetch(url: str, label: str = "") -> Optional[str]:
    """curl_cffi → certifi urllib → unverified urllib sırasıyla dener."""
    try:
        resp = requests.get(url, impersonate="chrome120", timeout=10,
                            headers={"Accept-Language": "en-US,en;q=0.8"})
        if resp.status_code == 200:
            return resp.text
        print(f"HayaletDebug: {label} HTTP {resp.status_code}", flush=True)
    except Exception as e:
        print(f"HayaletDebug: {label} curl_cffi error: {e}", flush=True)

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.8",
    })
    for verify in (True, False):
        try:
            ctx = (ssl.create_default_context(cafile=certifi.where())
                   if verify else ssl._create_unverified_context())
            with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                return r.read().decode()
        except Exception as e:
            print(f"HayaletDebug: {label} urllib error (verify={verify}): {e}", flush=True)
    return None

# ---------------------------------------------------------------------------
# TVMaze show yolu çözücü — isim eşleştirmeli, HTML scraping
# ---------------------------------------------------------------------------

@lru_cache(maxsize=128)
def _resolve_tvmaze_show_path(series_name: str) -> Optional[str]:
    """
    TVMaze HTML arama sayfasından dizinin show yolunu döndürür.
    Birden fazla sonuç varsa slug isim benzerliğiyle en iyisi seçilir.
    Örnek dönüş: '/shows/123/behzat-c'
    """
    query = urllib.parse.quote_plus(series_name)
    text = _fetch(f"https://www.tvmaze.com/search?q={query}", "TVMaze-search")
    if not text:
        return None

    candidates = re.findall(r'href="(/shows/\d+/([^"]+))"', text)
    if not candidates:
        return None

    best_path, best_score = None, -1.0
    for path, slug in candidates:
        readable = slug.replace("-", " ")
        score = _name_score(series_name, readable)
        print(f"HayaletDebug: TVMaze candidate '{readable}' score={score:.2f}", flush=True)
        if score > best_score:
            best_score = score
            best_path = path

    if best_score < 0.4:
        print(f"HayaletDebug: TVMaze best score {best_score:.2f} < 0.4, reddedildi", flush=True)
        return None

    print(f"HayaletDebug: TVMaze secildi -> {best_path} (score={best_score:.2f})", flush=True)
    return best_path


@lru_cache(maxsize=128)
def get_tvmaze_episode_runtime(
    series_name: str, season: int, episode: int
) -> Optional[int]:
    """TVMaze episode guide sayfasından bölüm süresini okur (isim eşleştirmeli)."""
    try:
        show_path = _resolve_tvmaze_show_path(series_name)
        if not show_path:
            return None

        ep_text = _fetch(f"https://www.tvmaze.com{show_path}/episodeguide",
                         "TVMaze-episodeguide")
        if not ep_text:
            return None

        marker = rf"\b{season}x{episode:02d}\b"
        m = re.search(
            marker + r".{0,500}?\((\d+)\s*min\)",
            re.sub(r"<[^>]+>", " ", ep_text),
            re.I | re.S,
        )
        if m:
            return int(m.group(1))
        return None
    except Exception as e:
        print(f"HayaletDebug: TVMaze runtime check failed: {e}", flush=True)
        return None


@lru_cache(maxsize=128)
def get_imdb_episode_runtime(
    series_name: str, season: int, episode: int
) -> Optional[int]:
    """IMDb bölüm sayfasındaki süreyi, isim eşleştirmesiyle doğru show için okur."""
    try:
        query = urllib.parse.quote_plus(series_name)
        search_text = _fetch(
            f"https://www.imdb.com/find/?q={query}&s=tt&ttype=tv",
            "IMDb-search"
        )
        if not search_text:
            return None

        pairs = re.findall(
            r'href="/title/(tt\d+)[^"]*"[^>]*>([^<]{2,80})<',
            search_text
        )
        if not pairs:
            ids = re.findall(r"/title/(tt\d+)", search_text)
            if not ids:
                return None
            best_id = ids[0]
            print(f"HayaletDebug: IMDb fallback, ilk ID: {best_id}", flush=True)
        else:
            best_id, best_score = None, -1.0
            for tt_id, title in pairs:
                score = _name_score(series_name, title)
                print(f"HayaletDebug: IMDb candidate '{title}' ({tt_id}) score={score:.2f}", flush=True)
                if score > best_score:
                    best_score = score
                    best_id = tt_id
            if best_score < 0.4:
                print(f"HayaletDebug: IMDb best score {best_score:.2f} < 0.4, reddedildi", flush=True)
                return None
            print(f"HayaletDebug: IMDb secildi -> {best_id} (score={best_score:.2f})", flush=True)

        page_text = _fetch(
            f"https://www.imdb.com/title/{best_id}/episodes/?season={season}",
            "IMDb-episodes"
        )
        if not page_text:
            return None

        text = re.sub(r"<[^>]+>", " ", page_text)
        marker = rf"(?:Episode\s+)?{episode}\b"
        m = re.search(
            marker + r".{0,900}?(?:(\d+)\s*min|PT(\d+)H?(\d+)?M)",
            text, re.I | re.S,
        )
        if not m:
            return None
        if m.group(1):
            return int(m.group(1))
        return int(m.group(2) or 0) * 60 + int(m.group(3) or 0)
    except Exception as e:
        print(f"HayaletDebug: IMDb runtime check failed: {e}", flush=True)
        return None


@lru_cache(maxsize=128)
def get_tvmaze_average_runtime(series_name: str) -> Optional[int]:
    """
    TVMaze show ana sayfasından ortalama/standart bölüm süresini HTML olarak okur.
    Sayfada 'Runtime: 45 minutes' ya da '45 min' şeklinde geçer.
    """
    try:
        show_path = _resolve_tvmaze_show_path(series_name)
        if not show_path:
            return None

        page_text = _fetch(f"https://www.tvmaze.com{show_path}", "TVMaze-showpage")
        if not page_text:
            return None

        text = re.sub(r"<[^>]+>", " ", page_text)
        m = re.search(r"[Rr]untime[:\s]+(\d+)\s*(?:min|minutes)?", text)
        if m:
            val = int(m.group(1))
            if 1 < val < 300:
                print(f"HayaletDebug: TVMaze averageRuntime={val} dk (HTML)", flush=True)
                return val
        return None
    except Exception as e:
        print(f"HayaletDebug: TVMaze averageRuntime check failed: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Birleşik referans süre döndürücü
# ---------------------------------------------------------------------------

def get_reference_runtimes(
    series_name: str, season: int, episode: int
) -> Tuple[list[int], list[int]]:
    """Aynı bölümün farklı yayın kurgularından bilinen süreleri döndürür.
    Dönüş: (Birincil Referanslar, İkincil/Ortalama Referanslar)"""
    primary = {
        value for value in (
            get_tvmaze_episode_runtime(series_name, season, episode),
            get_imdb_episode_runtime(series_name, season, episode),
        ) if value
    }
    secondary = {
        value for value in (
            get_tvmaze_average_runtime(series_name),
        ) if value
    }
    return sorted(primary), sorted(secondary)

def get_stream_duration(net: Network, stream: MergedStream) -> Optional[float]:
    """Bir MergedStream nesnesindeki videonun toplam süresini (dakika) tahmin eder."""
    try:
        m3u8_url = stream.video_master_url
        if not m3u8_url:
            return None
            
        resp = net.get(m3u8_url, referer=stream.video_referer)
        if resp.status_code != 200:
            return None
            
        text = resp.text.strip()
        lines = text.split('\n')
        
        # Eğer master playlist ise en yüksek kalite/ilk sıradaki stream'in adresini bul
        chunklist_url = None
        if "#EXT-X-STREAM-INF" in text:
            for line in lines:
                if not line.startswith('#') and line.strip():
                    chunklist_url = line.strip()
                    break
        else:
            # Doğrudan chunklist
            chunklist_url = m3u8_url
            
        if chunklist_url:
            if not chunklist_url.startswith('http'):
                chunklist_url = urljoin(m3u8_url, chunklist_url)
                
            if chunklist_url != m3u8_url:
                resp2 = net.get(chunklist_url, referer=stream.video_referer)
                if resp2.status_code == 200:
                    text = resp2.text
                    
        # #EXTINF sürelerini topla
        duration_sec = sum(float(m.group(1)) for m in re.finditer(r'#EXTINF:\s*([\d\.]+)', text))
        if duration_sec > 0:
            return duration_sec / 60.0
        return None
    except Exception as e:
        logs.log.debug("Stream duration check failed: %s", e)
        return None

def verify_episode(net: Network, series_name: str, season: int, episode: int, stream: MergedStream) -> Tuple[bool, Optional[str]]:
    """
    TVMaze/IMDb bölüm süresi ile M3U8 süresini karşılaştırır. 
    """
    primary_refs, secondary_refs = get_reference_runtimes(series_name, season, episode)
    
    if primary_refs:
        all_refs = primary_refs
    else:
        all_refs = secondary_refs
        
    print(f"HayaletDebug: verify_episode('{series_name}') -> primary={primary_refs}, secondary={secondary_refs}, all={all_refs}", flush=True)
    if not all_refs:
        print(f"HayaletDebug: verify_episode -> all_refs is empty, returning True!", flush=True)
        return True, None
        
    stream_min = get_stream_duration(net, stream)
    print(f"HayaletDebug: verify_episode stream_min={stream_min}", flush=True)
    if not stream_min:
        return True, None
        
    closest = min(all_refs, key=lambda value: abs(value - stream_min))
    
    # Tolerans: Standart 12 dakika
    if abs(closest - stream_min) <= 12:
        print(f"HayaletDebug: verify_episode closest={closest}, stream_min={stream_min} -> OK", flush=True)
        return True, None
        
    print(f"HayaletDebug: verify_episode closest={closest}, stream_min={stream_min} -> FAILED", flush=True)
        
    display_refs = primary_refs if primary_refs else all_refs
    
    return False, (
        f"Bölüm süresi eksik/hatalı! Olması Gereken: "
        f"{', '.join(str(value) for value in display_refs)} dk, "
        f"Kaynakta Bulunan: {int(stream_min)} dk."
    )
