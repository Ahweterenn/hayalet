"""Dizipal'in Next.js motorunu paylaşan siteler için ortak taban adapter.

asyawatch, selcukflix ve benzerleri aynı "Web Drama Turkey"/Dizipal-Next.js
arka ucunu kullanıyor: arama `POST /api/bg/searchContent?searchterm=`, sayfa
verisi `__NEXT_DATA__.pageProps.secureData`, oynatıcı pichive/NP_CONFIG ailesi.
Tek fark, `secureData`/`response` yükünün nasıl çözüldüğü (düz base64 mı, AES mı);
onu alt sınıf `_decode(raw)` ile verir. Geri kalan her şey ortak.

Zincir (canlı doğrulandı, 2026-11):
  POST /api/bg/searchContent?searchterm=   -> {"response": <yük>}; .result[] kartlar
  GET  /<slug>                              -> __NEXT_DATA__.secureData
       ...getSerieSeasonAndEpisodes.result[].episodes[]  (sezon/bölüm listesi)
  GET  /<slug>/sezon-N/bolum-M              -> __NEXT_DATA__.secureData
       ...getEpisodeSources.result[].source_content  (pichive iframe)
"""
from __future__ import annotations

import json
import re

from hayalet.core import remote
from hayalet.core.extractor import ExtractError
from hayalet.core.m3u8_parser import get_av_urls
from hayalet.core.merge import AudioSource, MergedStream
from hayalet.core.models import Episode, Series
from hayalet.core.network import BlockedError, Network, media_headers
from hayalet.core.resolver import ResolverError
from hayalet.core.session import SessionState
from hayalet.sites import _pichive

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_IFRAME_SRC_RE = re.compile(r'src=["\']([^"\']+)["\']', re.I)


def _find(obj, key):
    """İç içe dict/list içinde `key` anahtarının ilk değerini döndürür."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find(v, key)
            if found is not None:
                return found
    return None


class DizipalNextAdapter:
    """Ortak taban. Alt sınıf `name`, `known_domain` ve `_decode(raw)` verir."""

    name = ""
    known_domain = ""
    # pichive akışı yalnız chrome kimliğine düzgün yanıt veriyor (vidmoly gibi).
    impersonates = ("chrome",)

    def _decode(self, raw: str):
        """Sitenin `secureData`/`response` yükünü Python nesnesine çevirir."""
        raise NotImplementedError

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

    # --- Sayfa verisi -----------------------------------------------------
    def _secure_data(self, net: Network, session: SessionState, url: str):
        base = session.base_url
        html = net.get(url, referer=base + "/",
                       headers=media_headers(base + "/")).text
        m = _NEXT_DATA_RE.search(html)
        if not m:
            raise ExtractError(f"{self.name}: __NEXT_DATA__ bulunamadı.")
        pp = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
        secure = pp.get("secureData")
        if not isinstance(secure, str):
            raise ExtractError(f"{self.name}: secureData yok (sayfa yapısı değişmiş).")
        return self._decode(secure)

    # --- Arama ------------------------------------------------------------
    def _card(self, item: dict) -> Series | None:
        slug = item.get("used_slug")                 # örn. "dizi/spooks"
        name = item.get("object_name")
        if not slug or not name:
            return None
        is_series = (item.get("used_type") or "").lower() == "series"
        year = item.get("object_release_year")
        rating = item.get("object_related_imdb_point") or item.get("imdb_point")
        return Series(name=name, slug=slug, site=self.name,
                      type="Dizi" if is_series else "Film",
                      year=str(year) if year else "",
                      rating=str(rating) if rating else "")

    def search(self, net: Network, session: SessionState, query: str) -> list[Series]:
        base = session.base_url
        resp = net.post(base + "/api/bg/searchContent", params={"searchterm": query},
                        referer=base + "/",
                        headers={**media_headers(base + "/"),
                                 "X-Requested-With": "XMLHttpRequest"})
        try:
            env = self._decode(resp.json()["response"])
        except (ValueError, KeyError) as e:
            raise ExtractError(f"{self.name}: arama yanıtı çözülemedi: {e}")
        out, seen = [], set()
        for item in env.get("result") or []:
            s = self._card(item)
            if s and s.slug not in seen:
                seen.add(s.slug)
                out.append(s)
        return out

    def suggest(self, net: Network, session: SessionState, query: str) -> list[Series]:
        return self.search(net, session, query)

    # --- Bölümler ---------------------------------------------------------
    def get_episodes(self, net: Network, session: SessionState,
                     series: Series) -> list[Episode]:
        base = session.base_url
        page_url = series.url(base)
        if series.type == "Film":
            return [Episode(season=1, number=1, url=page_url, title=series.name)]
        sd = self._secure_data(net, session, page_url)
        seasons = _find(sd, "getSerieSeasonAndEpisodes") or {}
        result = seasons.get("result") if isinstance(seasons, dict) else None
        episodes: list[Episode] = []
        for season_block in result or []:
            for ep in season_block.get("episodes") or []:
                s = ep.get("season_no")
                n = ep.get("episode_no")
                if not s or not n:
                    continue
                episodes.append(Episode(
                    season=int(s), number=int(n),
                    url=f"{page_url}/sezon-{s}/bolum-{n}",
                    title=ep.get("episode_text") or ""))
        return sorted(episodes, key=lambda e: (e.season, e.number))

    # --- Akış -------------------------------------------------------------
    def _iframes(self, sd) -> list[str]:
        srcs = _find(sd, "getEpisodeSources") or {}
        result = srcs.get("result") if isinstance(srcs, dict) else None
        urls: list[str] = []
        for s in result or []:
            m = _IFRAME_SRC_RE.search(s.get("source_content") or "")
            if m:
                urls.append(m.group(1))
        return urls

    def build_stream(self, net: Network, session: SessionState,
                     episode: Episode, series: Series) -> MergedStream:
        sd = self._secure_data(net, session, episode.url)
        iframes = self._iframes(sd)
        if not iframes:
            raise ExtractError(f"{self.name}: bu bölümde oynatılacak kaynak yok.")

        last_err: Exception | None = None
        for iframe in iframes:
            try:
                master, referer, sub = _pichive.resolve(net, iframe, episode.url)
                _, tracks = get_av_urls(net, session, master, referer, "best")
                audios = [AudioSource(url=t.url, referer=referer, lang=(t.lang or "und"),
                                      name=(t.name or "Ses"), is_turkish=t.is_turkish)
                          for t in tracks]
                for i, a in enumerate(audios):
                    a.is_default = i == 0
                return MergedStream(video_master_url=master, video_referer=referer,
                                    audios=audios, subtitle_url=sub,
                                    subtitle_referer=referer)
            except Exception as e:   # bu kaynak ölüyse sıradakini dene
                last_err = e
                continue
        raise ExtractError(f"{self.name}: çalışan kaynak yok "
                           f"({len(iframes)} denendi): {last_err}")
