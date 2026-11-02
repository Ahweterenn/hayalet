"""DiziSol site adapter'ı (film + dizi, TMDB kimlikli JSON API).

Site bir toplayıcı: her yapım için birden çok kaynağı veritabanında tutuyor ve
hepsini tek yanıtta veriyor. Kaynak adları burada sabit değil, API'den okunur;
biri ölünce sıradakine geçilir. Yalnızca herkese açık içerik uçları kullanılır.

Zincir (canlı doğrulandı, 2026-11):
  GET /api/movies/search?q=                      -> [{tmdbId, title, type, ...}]
  GET /api/movies/by-tmdb/<id>/episodes          -> [{season, episode, ...}]
  GET /api/movies/by-tmdb/<id>?type=movie
      veya ?season=&episode=&type=tv             -> {sources: [...], subtitleTr}
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from hayalet.core.extractor import ExtractError
from hayalet.core.m3u8_parser import get_av_urls
from hayalet.core.merge import AudioSource, MergedStream
from hayalet.core.models import Episode, Series
from hayalet.core.network import BlockedError, Network, media_headers
from hayalet.core.resolver import ResolverError
from hayalet.core.session import SessionState
from hayalet.core.sites import register

_TMDB_IMG = "https://image.tmdb.org/t/p/w500"
_EP_SUFFIX_RE = re.compile(r"\s+-\s+\d+\.\s*Bölüm\s*$", re.I)
_DIZIYOU_RE = re.compile(r"^diziyou::(\d+)::")
_PROBE_TIMEOUT = 8      # ölü kaynak uzun bekletmesin, sırada başkası var


def _poster(path: str | None) -> str:
    if not path:
        return ""
    return path if path.startswith("http") else _TMDB_IMG + path


def _parse_tmdb(slug: str) -> tuple[str, str]:
    """"tv/1396" -> ("tv", "1396")."""
    kind, _, tmdb_id = slug.partition("/")
    return kind, tmdb_id


def _source_target(raw: str, base_url: str) -> tuple[str, str] | None:
    """API'deki kaynak değerini (oynatılacak adres, referer) çiftine çevirir.

    `site::...` biçimli kayıtlar sitenin iç kodudur; çözümünü bildiğimiz
    diziyou dışındakiler atlanır.
    """
    raw = (raw or "").strip()
    m = _DIZIYOU_RE.match(raw)
    if m:
        vid = m.group(1)
        return (f"https://storage.diziyou.one/episodes/{vid}/play.m3u8",
                f"https://www.diziyou.one/player/{vid}.html")
    if not raw or "::" in raw:
        return None
    return urljoin(base_url + "/", raw), base_url + "/"


class DizisolAdapter:
    name = "dizisol"
    known_domain = "https://dizisol.com"

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

    def _api(self, net: Network, session: SessionState, path: str, **params):
        base = session.base_url
        resp = net.get(base + path, referer=base + "/", params=params or None,
                       headers={**media_headers(base + "/"), "Accept": "application/json"})
        try:
            return resp.json()
        except ValueError as e:
            raise ExtractError(f"dizisol: {path} JSON dönmedi: {e}")

    def _series(self, item: dict) -> Series | None:
        """Arama (`tmdbId`) ve katalog (`id`) kartlarının ikisini de okur."""
        tmdb_id = item.get("tmdbId") or item.get("id")
        kind = item.get("type") or item.get("media_type") or item.get("mediaType")
        title = item.get("titleTr") or item.get("title") or item.get("name")
        if not tmdb_id or kind not in ("tv", "movie") or not title:
            return None
        date = item.get("first_air_date") or item.get("release_date") or ""
        rating = item.get("vote_average")
        return Series(name=_EP_SUFFIX_RE.sub("", title), slug=f"{kind}/{tmdb_id}",
                      type="Dizi" if kind == "tv" else "Film", site=self.name,
                      year=str(item.get("year") or date[:4]),
                      rating=str(rating) if rating else "",
                      poster_url=_poster(item.get("poster") or item.get("poster_path")),
                      backdrop_url=_poster(item.get("backdrop_path")),
                      description=item.get("overview") or "")

    def _cards(self, items) -> list[Series]:
        out: list[Series] = []
        seen: set[str] = set()
        for item in items or []:
            s = self._series(item)
            if s and s.slug not in seen:
                seen.add(s.slug)
                out.append(s)
        return out

    def search(self, net: Network, session: SessionState, query: str) -> list[Series]:
        return self._cards(self._api(net, session, "/api/movies/search", q=query))

    def suggest(self, net: Network, session: SessionState, query: str) -> list[Series]:
        return self.search(net, session, query)

    def browse(self, net: Network, session: SessionState, kind: str) -> list[Series]:
        media = {"dizi": "tv", "film": "movie"}.get(kind)
        if not media:
            return []
        data = self._api(net, session, "/api/library/browse", type=media, page=1)
        return self._cards(data.get("results"))

    def home_rows(self, net: Network, session: SessionState) -> list[dict]:
        data = self._api(net, session, "/api/library/home-feed")
        rows = []
        for row in data.get("rows") or []:
            items = self._cards(row.get("items"))
            if items:
                rows.append({"title": row.get("title") or "Öne çıkanlar", "items": items})
        return rows

    def get_episodes(self, net: Network, session: SessionState, series: Series) -> list[Episode]:
        kind, tmdb_id = _parse_tmdb(series.slug)
        path = f"{session.base_url}/api/movies/by-tmdb/{tmdb_id}"
        if kind == "movie":
            return [Episode(season=1, number=1, url=f"{path}?type=movie", title=series.name)]
        items = self._api(net, session, f"/api/movies/by-tmdb/{tmdb_id}/episodes")
        # season=0/episode=0 kaydı bölüm değil, dizinin kendi kartı.
        episodes = {(i["season"], i["episode"]) for i in items
                    if i.get("season", 0) >= 1 and i.get("episode", 0) >= 1
                    and i.get("isActive", True)}
        return [Episode(season=s, number=n,
                        url=f"{path}?season={s}&episode={n}&type=tv")
                for s, n in sorted(episodes)]

    def _plays(self, net: Network, url: str, referer: str) -> bool:
        try:
            r = net.get(url, referer=referer, headers=media_headers(referer),
                        retries=1, timeout=_PROBE_TIMEOUT)
        except Exception:   # ölü kaynak hangi hatayla düşerse düşsün sıradakine geç
            return False
        return r.status_code == 200 and r.text.lstrip().startswith("#EXTM3U")

    def build_stream(self, net: Network, session: SessionState,
                     episode: Episode, series: Series) -> MergedStream:
        base = session.base_url
        data = net.get(episode.url, referer=base + "/",
                       headers={**media_headers(base + "/"), "Accept": "application/json"}).json()
        if data.get("isPremium"):
            raise ExtractError("dizisol: bu içerik üyelere özel.")

        sources = sorted((s for s in data.get("sources") or [] if s.get("isActive", True)),
                         key=lambda s: s.get("priority", 99))
        # Üst düzey kayıt çoğu zaman listede de var; yoksa son çare olarak dene.
        sources.append({"m3u8Url": data.get("m3u8Url"), "subtitleTr": data.get("subtitleTr")})

        tried: set[str] = set()
        for src in sources:
            target = _source_target(src.get("m3u8Url"), base)
            if not target or target[0] in tried:
                continue
            url, referer = target
            tried.add(url)
            if not self._plays(net, url, referer):
                continue
            _, tracks = get_av_urls(net, session, url, referer, "best")
            audios = [AudioSource(url=t.url, referer=referer, lang=(t.lang or "und"),
                                  name=(t.name or "Ses"), is_turkish=t.is_turkish)
                      for t in tracks]
            for i, a in enumerate(audios):
                a.is_default = i == 0
            sub = src.get("subtitleTr") or data.get("subtitleTr")
            return MergedStream(video_master_url=url, video_referer=referer, audios=audios,
                                subtitle_url=urljoin(base + "/", sub) if sub else None,
                                subtitle_referer=base + "/")
        raise ExtractError(f"dizisol: çalışan kaynak yok ({len(tried)} kaynak denendi).")


register(DizisolAdapter())
