"""Diziyou site adapter'ı."""
from __future__ import annotations

import html
import json
import re
from urllib.parse import quote, urljoin, urlparse

from hayalet.core import npgate, remote
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
    known_domain = "https://diziyou.net"

    def resolve_domain(self, net: Network, session: SessionState,
                       override: str | None = None, use_cache: bool = True) -> str:
        domain = (override or remote.get(self.name + ".known_domain",
                                          self.known_domain)).rstrip("/")
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
        # Site 2026 sonunda diziyou.one -> diziyou.net taşındı ve tema değişti:
        # eski admin-ajax araması yok, arama artık /?s= sayfasındaki kartlar.
        page = net.get(f"{session.base_url}/?s={quote(query)}",
                       referer=session.base_url).text
        out: list[Series] = []
        seen: set[str] = set()
        for m in re.finditer(r'<article class="card">([\s\S]*?)</article>', page):
            block = m.group(1)
            link_m = re.search(r'<a href="([^"]+/dizi/[^"]+)"', block)
            title_m = re.search(r'<h3>([^<]+)</h3>', block)
            if not link_m or not title_m:
                continue
            slug = urlparse(link_m.group(1)).path.strip("/")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            year_m = re.search(r'<span>(\d{4})</span>', block)
            out.append(Series(name=html.unescape(title_m.group(1)).strip(),
                              slug=slug, type="Dizi", site=self.name,
                              year=year_m.group(1) if year_m else "",
                              poster_url=_poster(block, session.base_url)))
        return out

    def suggest(self, net: Network, session: SessionState, query: str) -> list[Series]:
        return self.search(net, session, query)

    def get_episodes(self, net: Network, session: SessionState, series: Series) -> list[Episode]:
        page = net.get(series.url(session.base_url), referer=session.base_url).text
        # Sayfada yalnızca son sezonun bölüm linkleri var; diğerleri JS ile
        # yükleniyor. Ama her sezon butonu bölüm sayısını taşıyor ve bölüm
        # adresi sabit kalıpta: /bolum/<slug>-<S>-sezon-<E>-bolum/
        totals: dict[int, int] = {}
        for m in re.finditer(r'data-season-number="(\d+)"\s+data-total-episodes="(\d+)"', page):
            s, n = int(m.group(1)), int(m.group(2))
            totals[s] = max(totals.get(s, 0), n)

        name = series.slug.rstrip("/").split("/")[-1]
        episodes: dict[tuple[int, int], Episode] = {}
        for s, n in totals.items():
            for e in range(1, n + 1):
                url = f"{session.base_url}/bolum/{name}-{s}-sezon-{e}-bolum/"
                episodes[(s, e)] = Episode(season=s, number=e, url=url)
        # Sayfadaki gerçek linkler (kalıp dışı olursa) önceliklidir.
        for m in re.finditer(r'href="(https?://[^"]+-(\d+)-sezon-(\d+)-bolum/?)"', page, re.IGNORECASE):
            key = (int(m.group(2)), int(m.group(3)))
            episodes[key] = Episode(season=key[0], number=key[1], url=m.group(1))
        return [episodes[k] for k in sorted(episodes)]

    def build_stream(self, net: Network, session: SessionState,
                     episode: Episode, series: Series) -> MergedStream:
        page = net.get(episode.url, referer=session.base_url).text

        iframe_m = re.search(r'<iframe[^>]+src="([^"]+(?:dzembed\.php|player/)[^"]*)"',
                             page, re.IGNORECASE)
        if not iframe_m:
            raise ExtractError("diziyou: Oynatıcı iframe'i bulunamadı.")
        embed_url = html.unescape(iframe_m.group(1))
        if embed_url.startswith("//"):
            embed_url = "https:" + embed_url

        embed_html = net.get(embed_url, referer=episode.url,
                             retries=1, timeout=10).text

        m3u8_url, subtitle_url = _parse_np_config(embed_html)
        if not m3u8_url:  # eski oynatıcı: <source src=...m3u8>
            source_m = re.search(r'<source[^>]+src="([^"]+\.m3u8)"', embed_html, re.IGNORECASE)
            if source_m:
                m3u8_url = source_m.group(1)
            track_m = re.search(r'<track[^>]+src="([^"]+\.vtt)"[^>]*srclang="tr"',
                                embed_html, re.IGNORECASE)
            if track_m:
                subtitle_url = track_m.group(1)
        if not m3u8_url:
            raise ExtractError("diziyou: m3u8 kaynağı bulunamadı.")
        m3u8_url = urljoin(embed_url, m3u8_url)
        if subtitle_url:
            subtitle_url = urljoin(embed_url, subtitle_url)

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


def _parse_np_config(embed_html: str) -> tuple[str | None, str | None]:
    """Yeni oynatıcının window.NP_CONFIG JSON'undan (master m3u8, TR altyazı)."""
    m = re.search(r'window\.NP_CONFIG\s*=\s*(\{.*?\});\s*</script>', embed_html, re.S)
    if not m:
        return None, None
    try:
        cfg = json.loads(m.group(1))
    except ValueError:
        return None, None
    try:
        npgate.apply(embed_html, cfg)
    except npgate.GateError as e:
        raise ExtractError(f"diziyou: {e}")
    master = next((src.get("master") for src in cfg.get("sources") or []
                   if src.get("master")), None)
    sub = next((t.get("url") for t in cfg.get("subtitles") or []
                if t.get("lang") == "tr" and t.get("url")), None)
    return master, sub


register(DiziyouAdapter())
