import re
import json
from typing import List, Dict, Optional, Any
from urllib.parse import urljoin

from hayalet.core.models import SearchResult, Episode, Stream
from hayalet.core.sites import SiteAdapter
from hayalet.core.network import NetworkContext

class WebDramaTurkeyAdapter(SiteAdapter):
    @property
    def name(self) -> str:
        return "webdramaturkey"
        
    def _extract_number(self, text: str) -> float:
        m = re.search(r'(\d+)', text)
        return float(m.group(1)) if m else 0.0

    def search(self, ctx: NetworkContext, query: str) -> List[SearchResult]:
        url = f"https://webdramaturkey2.com/arama/{query}"
        resp = ctx.session.get(url, impersonate='chrome120')
        
        results = []
        items = re.findall(r'<a href="([^"]+)" class="list-title"[^>]*>\s*([\s\S]*?)\s*</a>', resp.text)
        
        # Avoid duplicates
        seen = set()
        for link, title in items:
            title = re.sub(r'<[^>]+>', '', title).strip()
            if link not in seen:
                seen.add(link)
                results.append(SearchResult(
                    title=title,
                    url=link,
                    year=None
                ))
        return results

    def suggest(self, ctx: NetworkContext, query: str) -> List[SearchResult]:
        return self.search(ctx, query)

    def get_episodes(self, ctx: NetworkContext, url: str) -> List[Episode]:
        resp = ctx.session.get(url, impersonate='chrome120')
        episodes = []
        
        links = re.findall(r'<a href="([^"]+-bolum)"[^>]*>([\s\S]*?)</a>', resp.text)
        seen = set()
        for link, text in links:
            if link in seen:
                continue
            seen.add(link)
            
            title = re.sub(r'<[^>]+>', '', text).strip()
            num = self._extract_number(title)
            
            s_match = re.search(r'/(\d+)-sezon/', link)
            season = int(s_match.group(1)) if s_match else 1
            
            episodes.append(Episode(
                title=f"{season}. Sezon {title}",
                url=link,
                number=num,
                season=season
            ))
            
        return sorted(episodes, key=lambda x: (x.season, x.number))

    def build_stream(self, ctx: NetworkContext, episode: Episode) -> Optional[Stream]:
        resp = ctx.session.get(episode.url, impersonate='chrome120')
        
        embed_ids = re.findall(r'data-embed="(\d+)"', resp.text)
        seen = set()
        embed_ids = [x for x in embed_ids if not (x in seen or seen.add(x))]
        
        for e_id in embed_ids:
            try:
                post_resp = ctx.session.post('https://webdramaturkey2.com/ajax/embed', data={'id': e_id}, headers={'referer': episode.url}, impersonate='chrome120')
                iframe_m = re.search(r'src="([^"]+)"', post_resp.text)
                if not iframe_m:
                    continue
                iframe_url = iframe_m.group(1)
                
                if 'video.php' in iframe_url:
                    if iframe_url.startswith('/'):
                        iframe_url = 'https://webdramaturkey2.com' + iframe_url
                    v_resp = ctx.session.get(iframe_url, headers={'referer': episode.url}, impersonate='chrome120')
                    inner_m = re.search(r'iframe[^>]*src="([^"]+)"', v_resp.text)
                    if inner_m:
                        iframe_url = inner_m.group(1)
                
                player_resp = ctx.session.get(iframe_url, headers={'referer': episode.url}, impersonate='chrome120')
                
                q_m = re.search(r'const qualities = (\[.*?\]);', player_resp.text)
                if q_m:
                    qualities = json.loads(q_m.group(1))
                    if qualities:
                        best = qualities[-1]
                        redirect_url = best['url']
                        if redirect_url.startswith('/'):
                            domain = re.match(r'https?://[^/]+', iframe_url).group(0)
                            redirect_url = domain + redirect_url
                            
                        redir_resp = ctx.session.get(redirect_url, headers={'referer': iframe_url}, impersonate='chrome120', allow_redirects=False)
                        stream_url = redir_resp.headers.get('Location')
                        if stream_url:
                            return Stream(url=stream_url, headers={'Referer': iframe_url})
                            
                m_stream = re.search(r'(https?://[^"]+(?:m3u8|mp4)[^"]*)', player_resp.text)
                if m_stream:
                    return Stream(url=m_stream.group(1), headers={'Referer': iframe_url})

            except Exception:
                pass
                
        return None
