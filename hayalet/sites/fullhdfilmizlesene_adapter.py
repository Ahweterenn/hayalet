import re
import json
import base64
from typing import List
from urllib.parse import urlparse, urljoin

from hayalet.core.models import Series, Episode
from hayalet.core.merge import MergedStream, AudioSource
from hayalet.core.network import Network, SessionState
from hayalet.core.sites import SiteAdapter, register
from hayalet.core.extractor import ExtractError
from hayalet.core.utils import poster_url_from_html


def _poster(fragment: str, base_url: str) -> str:
    """FullHDFilm kartlarındaki tt/lazy poster alanlarını seçer."""
    return poster_url_from_html(fragment, base_url)

class FullhdfilmizleseneAdapter:
    """fullhdfilmizlesene.now adaptörü (Film Odaklı)."""

    name = "fullhdfilmizlesene"
    known_domain = "https://www.fullhdfilmizlesene.now"

    def resolve_domain(self, net: Network, session: SessionState,
                       override: str | None = None, use_cache: bool = True) -> str:
        return override if override else self.known_domain

    def search(self, net: Network, session: SessionState, query: str) -> List[Series]:
        resp = net.get(f"{self.known_domain}/arama/{query}")
        results = []
        for m in re.finditer(r'<a[^>]*class="tt"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>',
                             resp.text):
            url = m.group(1)
            fragment = m.group(2)
            title = re.sub(r'<[^>]+>', '', fragment).strip()
            if title.endswith(" izle"):
                title = title[:-5]
            
            slug = urlparse(url).path.strip("/")
            
            poster = _poster(fragment, self.known_domain)
            results.append(Series(
                name=title,
                slug=slug,
                type="Movie",
                site=self.name,
                poster_url=poster,
            ))
        return results

    def suggest(self, net: Network, session: SessionState, query: str) -> List[Series]:
        return self.search(net, session, query)

    def get_episodes(self, net: Network, session: SessionState, series: Series) -> List[Episode]:
        return [Episode(
            season=1,
            number=1,
            title="Film",
            url=series.url(session.base_url)
        )]

    def _decrypt_rtt(self, enc_str: str) -> str:
        dec = ""
        for c in enc_str:
            if 'a' <= c <= 'z':
                dec += chr(ord('a') + (ord(c) - ord('a') + 13) % 26)
            elif 'A' <= c <= 'Z':
                dec += chr(ord('A') + (ord(c) - ord('A') + 13) % 26)
            else:
                dec += c
        try:
            dec += "=" * ((4 - len(dec) % 4) % 4)
            return base64.b64decode(dec).decode("utf-8")
        except Exception:
            return ""

    def _decrypt_qv(self, b64_rev: str) -> str:
        try:
            rev_str = b64_rev[::-1]
            rev_str += "=" * ((4 - len(rev_str) % 4) % 4)
            rev_decoded = base64.b64decode(rev_str).decode('utf-8')
            o = ""
            for i in range(len(rev_decoded)):
                r = "K9L"[i % 3]
                n = ord(rev_decoded[i]) - (ord(r) % 5 + 1)
                o += chr(n)
            o += "=" * ((4 - len(o) % 4) % 4)
            return base64.b64decode(o).decode('utf-8')
        except Exception:
            return ""

    def build_stream(self, net: Network, session: SessionState,
                     episode: Episode, series: Series) -> MergedStream:
        html = net.get(episode.url).text

        scx_m = re.search(r'var\s+scx\s*=\s*(\{.*?\});', html)
        if not scx_m:
            raise ExtractError("fullhdfilmizlesene: scx (kaynaklar) bulunamadı.")
        
        try:
            scx_data = json.loads(scx_m.group(1))
        except Exception:
            raise ExtractError("fullhdfilmizlesene: scx JSON ayrıştırılamadı.")

        rapidvid_url = None
        for key, val in scx_data.items():
            if isinstance(val, dict) and "sx" in val:
                sx = val["sx"]
                for source_type in ["t", "p"]:
                    if source_type in sx and isinstance(sx[source_type], list):
                        for enc_str in sx[source_type]:
                            dec_url = self._decrypt_rtt(enc_str)
                            if "rapidvid.net" in dec_url or "vmnow.online" in dec_url or "vidmoly" in dec_url:
                                rapidvid_url = dec_url
                                break
                    if rapidvid_url: break
            if rapidvid_url: break

        if not rapidvid_url:
            raise ExtractError("fullhdfilmizlesene: rapidvid/vidmoly kaynağı bulunamadı.")

        player_html = net.get(rapidvid_url, referer=self.known_domain,
                              retries=1, timeout=10).text
        
        qv_m = re.search(r'(_|av)\(["\']([^"\']+)["\']\)', player_html)
        if not qv_m:
            m3u8_m = re.search(r'(https?://[^"]+(?:m3u8|mp4)[^"]*)', player_html)
            if m3u8_m:
                m3u8_url = m3u8_m.group(1)
            else:
                raise ExtractError("fullhdfilmizlesene: rapidvid player şifreli kaynak bulunamadı.")
        else:
            m3u8_url = self._decrypt_qv(qv_m.group(2))

        if not m3u8_url or not m3u8_url.startswith("http"):
            raise ExtractError("fullhdfilmizlesene: M3U8 adresi çözülemedi.")

        referer = f"https://{urlparse(rapidvid_url).netloc}/"
        return MergedStream(video_master_url=m3u8_url, video_referer=referer, audios=[])

register(FullhdfilmizleseneAdapter())
