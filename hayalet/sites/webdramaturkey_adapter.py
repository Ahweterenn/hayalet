"""WebDramaTurkey site adapter'ı."""
from __future__ import annotations

import html
import re
import json
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
    """WebDramaTurkey kartlarında data-background/CSS poster öncelikleri."""
    return poster_url_from_html(fragment, base_url)

class WebDramaTurkeyAdapter:
    name = "webdramaturkey"
    known_domain = "https://webdramaturkey2.com"
    
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
        url = f"{session.base_url}/arama/{query}"
        resp = net.get(url, referer=session.base_url)
        
        results: list[Series] = []
        items = re.findall(r'<a href="([^"]+)" class="list-title"[^>]*>([\s\S]*?)</a>',
                           resp.text)
        
        seen = set()
        for link, title_html in items:
            title = title_html
            title = re.sub(r'<[^>]+>', '', title).strip()
            title = html.unescape(title)
            slug = urlparse(link).path.strip("/")
            if slug not in seen:
                seen.add(slug)
                anchor = re.search(
                    r'<a[^>]+href="' + re.escape(link) + r'"[^>]*>[\s\S]*?</a>',
                    resp.text, re.IGNORECASE)
                poster = _poster(
                    anchor.group(0) if anchor else "", session.base_url
                )
                results.append(Series(name=title, slug=slug, type="Dizi",
                                      site=self.name, poster_url=poster))
        return results

    def suggest(self, net: Network, session: SessionState, query: str) -> list[Series]:
        return self.search(net, session, query)

    def _extract_number(self, text: str) -> int:
        m = re.search(r'(\d+)', text)
        return int(m.group(1)) if m else 0

    def get_episodes(self, net: Network, session: SessionState, series: Series) -> list[Episode]:
        page = net.get(series.url(session.base_url), referer=session.base_url).text
        episodes: list[Episode] = []
        
        links = re.findall(r'<a href="([^"]+-bolum)"[^>]*>([\s\S]*?)</a>', page)
        seen = set()
        for link, text in links:
            if link in seen:
                continue
            seen.add(link)
            
            title = re.sub(r'<[^>]+>', '', text).strip()
            num = self._extract_number(title)
            
            s_match = re.search(r'/(\d+)-sezon/', link)
            season = int(s_match.group(1)) if s_match else 1
            
            episodes.append(Episode(season=season, number=num, url=link))
            
        return sorted(episodes, key=lambda x: (x.season, x.number))

    def build_stream(self, net: Network, session: SessionState,
                     episode: Episode, series: Series) -> MergedStream:
        page = net.get(episode.url, referer=session.base_url).text
        
        embed_ids = re.findall(r'data-embed="(\d+)"', page)
        seen = set()
        embed_ids = [x for x in embed_ids if not (x in seen or seen.add(x))]
        
        m3u8_url = None
        iframe_url = ""
        
        for e_id in embed_ids:
            try:
                post_resp = net.post(session.base_url + '/ajax/embed',
                                     data={'id': e_id}, referer=episode.url,
                                     retries=1, timeout=8)
                iframe_m = re.search(r'src="([^"]+)"', post_resp.text)
                if not iframe_m:
                    continue
                iframe_url = iframe_m.group(1)
                
                if 'video.php' in iframe_url:
                    if iframe_url.startswith('/'):
                        iframe_url = session.base_url + iframe_url
                    v_resp = net.get(iframe_url, referer=episode.url,
                                     retries=1, timeout=8)
                    inner_m = re.search(r'iframe[^>]*src="([^"]+)"', v_resp.text)
                    if inner_m:
                        iframe_url = inner_m.group(1)
                
                player_resp = net.get(iframe_url, referer=episode.url,
                                      retries=1, timeout=10)
                
                q_m = re.search(r'const qualities = (\[.*?\]);', player_resp.text)
                if q_m:
                    qualities = json.loads(q_m.group(1))
                    if qualities:
                        best = qualities[-1]
                        redirect_url = best['url']
                        if redirect_url.startswith('/'):
                            domain = re.match(r'https?://[^/]+', iframe_url).group(0)
                            redirect_url = domain + redirect_url
                            
                        redir_resp = net.get(redirect_url, referer=iframe_url,
                                             retries=1, timeout=8,
                                             allow_redirects=False)
                        stream_url = redir_resp.headers.get('Location')
                        if stream_url:
                            m3u8_url = stream_url
                            break
                            
                m_stream = re.search(r'(https?://[^"]+(?:m3u8|mp4)[^"]*)', player_resp.text)
                if m_stream:
                    m3u8_url = m_stream.group(1)
                    break

            except Exception:
                pass
                
        if not m3u8_url:
            raise ExtractError("webdramaturkey: Uygun m3u8 stream'i bulunamadı.")
            
        return MergedStream(video_master_url=m3u8_url, video_referer=iframe_url,
                            audios=[], subtitle_url=None, subtitle_referer=iframe_url)

register(WebDramaTurkeyAdapter())
