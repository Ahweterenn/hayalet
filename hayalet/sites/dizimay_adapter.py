"""dizimay.pro site adapter'ı (Kore/Çin/Asya/BL dizileri, WordPress).

Basit WordPress kazıması: arama `/?s=`, dizi sayfası `/dizi/<slug>/`, bölüm
`/bolum/<slug>-N-bolum/`. Oynatıcı ok.ru embed'i → ortak `_okru` çözücüsü.
Asya dizilerinde altyazı videoya gömülü gelir; ayrı altyazı kanalı yok.

Zincir (canlı doğrulandı, 2026-11):
  GET /?s=<q>                    -> /dizi/<slug>/ linkli sonuç kartları
  GET /dizi/<slug>/             -> /bolum/<slug>-N-bolum/ bölüm linkleri
  GET /bolum/<slug>-N-bolum/    -> <iframe src="//ok.ru/videoembed/<id>">
"""
from __future__ import annotations

import html
import re

from hayalet.core import remote
from hayalet.core.extractor import ExtractError
from hayalet.core.m3u8_parser import get_av_urls
from hayalet.core.merge import AudioSource, MergedStream
from hayalet.core.models import Episode, Series
from hayalet.core.network import BlockedError, Network
from hayalet.core.resolver import ResolverError
from hayalet.core.session import SessionState
from hayalet.core.sites import register
from hayalet.sites import _okru

_CARD_RE = re.compile(r'<a[^>]+href="(https?://[^"]*/dizi/[^"]+)"[^>]*>(.*?)</a>', re.S)
_BOLUM_RE = re.compile(r'href="(https?://[^"]*/bolum/[^"]+)"')
_EPNUM_RE = re.compile(r'-(\d+)-bolum')
_SEASON_RE = re.compile(r'/(\d+)-sezon/')
_IFRAME_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)


def _clean(fragment: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))).strip()


class DizimayAdapter:
    name = "dizimay"
    known_domain = "https://dizimay.pro"

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
        base = session.base_url
        page = net.get(f"{base}/?s={query}", referer=base + "/").text
        out, seen = [], set()
        for link, inner in _CARD_RE.findall(page):
            slug = re.sub(r"^https?://[^/]+/", "", link).strip("/")
            name = _clean(inner)
            if not name or slug in seen:
                continue
            seen.add(slug)
            out.append(Series(name=name, slug=slug, type="Dizi", site=self.name))
        return out

    def suggest(self, net: Network, session: SessionState, query: str) -> list[Series]:
        return self.search(net, session, query)

    def get_episodes(self, net: Network, session: SessionState,
                     series: Series) -> list[Episode]:
        base = session.base_url
        page = net.get(series.url(base), referer=base + "/").text
        episodes, seen = [], set()
        for link in _BOLUM_RE.findall(page):
            if link in seen:
                continue
            seen.add(link)
            num = _EPNUM_RE.search(link)
            season = _SEASON_RE.search(link)
            episodes.append(Episode(
                season=int(season.group(1)) if season else 1,
                number=int(num.group(1)) if num else 0,
                url=link))
        return sorted(episodes, key=lambda e: (e.season, e.number))

    def build_stream(self, net: Network, session: SessionState,
                     episode: Episode, series: Series) -> MergedStream:
        base = session.base_url
        page = net.get(episode.url, referer=base + "/").text
        iframes = [u for u in _IFRAME_RE.findall(page) if "ok.ru" in u]
        if not iframes:
            raise ExtractError("dizimay: bu bölümde ok.ru oynatıcısı yok.")

        last_err: Exception | None = None
        for iframe in iframes:
            try:
                master, referer = _okru.resolve(net, iframe, episode.url)
                _, tracks = get_av_urls(net, session, master, referer, "best")
                audios = [AudioSource(url=t.url, referer=referer, lang=(t.lang or "und"),
                                      name=(t.name or "Ses"), is_turkish=t.is_turkish)
                          for t in tracks]
                for i, a in enumerate(audios):
                    a.is_default = i == 0
                return MergedStream(video_master_url=master, video_referer=referer,
                                    audios=audios, subtitle_url=None,
                                    subtitle_referer=referer)
            except Exception as e:
                last_err = e
                continue
        raise ExtractError(f"dizimay: çalışan kaynak yok ({len(iframes)} denendi): {last_err}")


register(DizimayAdapter())
