"""Diziyou site adapter'ı."""
from __future__ import annotations

import html
import re
from urllib.parse import urljoin, urlparse

from hayalet.core.extractor import ExtractError
from hayalet.core.m3u8_parser import get_av_urls
from hayalet.core.merge import AudioSource, MergedStream
from hayalet.core.models import Episode, Series
from hayalet.core.network import BlockedError, Network
from hayalet.core.resolver import ResolverError
from hayalet.core.session import SessionState
from hayalet.core.sites import register
from hayalet.core.utils import poster_url_from_html


def _poster(fragment: str, base_url: str) -> str:
    """Diziyou/WordPress kartında thumbnail ve lazy görsel öncelikleri."""
    return poster_url_from_html(fragment, base_url)


class DiziyouAdapter:
    name = "diziyou"
    known_domain = "https://www.diziyou.one"

    def resolve_domain(self, net: Network, session: SessionState,
                       override: str | None = None, use_cache: bool = True) -> str:
        domain = (override or self.known_domain).rstrip("/")
        try:
            r = net.get(domain, referer=domain)
        except BlockedError as e:
            raise ResolverError(f"{domain}'e ulaşılamadı: {e}")
        if r.status_code != 200:
            raise ResolverError(f"{domain} yanıt vermiyor (HTTP {r.status_code}).")
        session.base_url = domain
        session.referer = domain
        return domain

    def search(self, net: Network, session: SessionState, query: str) -> list[Series]:
        resp = net.post(session.base_url + "/wp-admin/admin-ajax.php", 
                        data={"action": "data_fetch", "keyword": query},
                        referer=session.base_url)
        out: list[Series] = []
        for m in re.finditer(r'<div id="searchelement">([\s\S]*?)</div>(?=<div id="searchelement">|$)',
                             resp.text, re.IGNORECASE):
            block = m.group(1)
            link_m = re.search(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', block)
            if not link_m:
                continue
            href = link_m.group(1)
            title = html.unescape(link_m.group(2)).strip()
            slug = urlparse(href).path.strip("/")
            if slug:
                poster = _poster(block, session.base_url)
                year_m = re.search(r'<div id="search-cat-year">(\d{4})</div>', block)
                year = year_m.group(1) if year_m else ""
                out.append(Series(name=title, slug=slug, type="Dizi",
                                  site=self.name, year=year, poster_url=poster))
        return out

    def suggest(self, net: Network, session: SessionState, query: str) -> list[Series]:
        return self.search(net, session, query)

    def get_episodes(self, net: Network, session: SessionState, series: Series) -> list[Episode]:
        page = net.get(series.url(session.base_url), referer=session.base_url).text
        episodes: list[Episode] = []
        seen = set()
        
        for m in re.finditer(r'href="(https?://[^"]+-(\d+)-sezon-(\d+)-bolum/?)"', page, re.IGNORECASE):
            url = m.group(1)
            season = int(m.group(2))
            number = int(m.group(3))
            if url not in seen:
                seen.add(url)
                episodes.append(Episode(season=season, number=number, url=url))
                
        episodes.sort(key=lambda e: (e.season, e.number))
        return episodes

    def build_stream(self, net: Network, session: SessionState,
                     episode: Episode, series: Series) -> MergedStream:
        page = net.get(episode.url, referer=session.base_url).text
        
        iframe_m = re.search(r'<iframe[^>]+src="([^"]+player/[^"]+)"', page, re.IGNORECASE)
        if not iframe_m:
            raise ExtractError("diziyou: Oynatıcı iframe'i bulunamadı.")
            
        embed_url = iframe_m.group(1)
        if embed_url.startswith("//"):
            embed_url = "https:" + embed_url
            
        embed_html = net.get(embed_url, referer=episode.url,
                             retries=1, timeout=10).text
        
        source_m = re.search(r'<source[^>]+src="([^"]+\.m3u8)"', embed_html, re.IGNORECASE)
        if not source_m:
            raise ExtractError("diziyou: m3u8 kaynağı bulunamadı.")
        
        m3u8_url = source_m.group(1)
        
        subtitle_url = None
        track_m = re.search(r'<track[^>]+src="([^"]+\.vtt)"[^>]*srclang="tr"', embed_html, re.IGNORECASE)
        if track_m:
            subtitle_url = track_m.group(1)
            
        _, tracks = get_av_urls(net, session, m3u8_url, embed_url, "best")
        
        audios = [AudioSource(url=t.url, referer=embed_url, lang=(t.lang or "und"),
                             name=(t.name or "Ses"), is_turkish=t.is_turkish)
                 for t in tracks]
        for a in audios:
            a.is_default = False
        if audios:
            audios[0].is_default = True

        return MergedStream(video_master_url=m3u8_url, video_referer=embed_url,
                            audios=audios, subtitle_url=subtitle_url,
                            subtitle_referer=embed_url)

register(DiziyouAdapter())
