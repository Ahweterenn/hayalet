"""Odanın içinden katalog: arama, bölüm listesi, akış çözme, sonraki bölüm.

Ev sahibi içerik seçmek için odadan çıkmak zorunda kalmasın diye oda sunucusu
hayalet'in site adaptörlerine buradan erişiyor. İstemciye (tarayıcıya) hiçbir
site adresi ya da iç nesne gitmez; her sonuç ve bölüm opak bir `ref` ile
temsil edilir, ref'in neye karşılık geldiğini yalnız sunucu bilir. Böylece bir
misafir elle hazırladığı bir ref ile sunucuya keyfî bir adres çözdüremez.

Ağ bağlamı dışarıdan verilir (`contexts`: tüm sitelerin bağlantıları,
`context_for`: tek sitenin bağlantısı). Android köprüsü kendi oturum
tablosunu, masaüstü CLI kendi bağlantılarını verir; testler sahte adaptör.
"""
from __future__ import annotations

import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

from hayalet.core.models import Episode, Series, next_episode

# Bellekte tutulan ref sayısı. Arama başına ~20 sonuç + açılan dizilerin
# bölümleri; bir izleme akşamı için rahat yeter, sınırsız büyümez.
_MAX_REFS = 3000
_MAX_RESULTS = 24
# Akış adresleri belirteçli; eski ön çözüm kullanılmasın.
_PREFETCH_TTL = 600.0


@dataclass
class Resolved:
    """Odaya konacak çözülmüş akış ve onu oynatmak için gereken kimlik."""

    url: str
    referer: str
    subtitles: list = field(default_factory=list)      # [{"url","label"}]
    user_agent: str = ""
    impersonate: str = ""
    cookies: dict = field(default_factory=dict)
    now: dict = field(default_factory=dict)            # ekranda gösterilecek bilgi


class CatalogError(Exception):
    pass


def _is_movie(series: Series) -> bool:
    from hayalet.core import catalog as core_catalog
    return core_catalog.is_movie(series)


def _episode_label(ep: Episode, movie: bool, full: bool = False) -> str:
    """Listede "3. Bölüm" (sezon zaten seçili), başlıkta "1. Sezon 3. Bölüm"."""
    if movie:
        return "Film"
    if ep.title:
        return ep.title
    return f"{ep.season}. Sezon {ep.number}. Bölüm" if full else f"{ep.number}. Bölüm"


class Catalog:
    def __init__(self, contexts: Callable[[], dict],
                 context_for: Callable[[str], tuple]):
        self._contexts = contexts
        self._context_for = context_for
        self._refs: "OrderedDict[str, tuple]" = OrderedDict()
        self._lock = threading.Lock()
        # Dizinin bölüm listesi: sonraki bölümü bulmak için tekrar indirmeyelim.
        self._episodes: dict[str, list[Episode]] = {}
        # Önceden çözülen akışlar: (site, slug, sezon, bölüm) -> (zaman, Future)
        self._streams: dict[tuple, tuple] = {}
        self._pf_pool = None

    # --- ref tablosu -------------------------------------------------------
    def _put(self, kind: str, *obj) -> str:
        ref = secrets.token_urlsafe(9)
        with self._lock:
            self._refs[ref] = (kind, *obj)
            while len(self._refs) > _MAX_REFS:
                self._refs.popitem(last=False)
        return ref

    def _get(self, ref: str, kind: str) -> tuple:
        with self._lock:
            item = self._refs.get(str(ref or ""))
        if not item or item[0] != kind:
            raise CatalogError("bu içerik artık listede yok, yeniden ara")
        return item[1:]

    # --- dış yüz -----------------------------------------------------------
    def search(self, query: str) -> list[dict]:
        from hayalet.core import sites
        q = str(query or "").strip()[:80]
        if len(q) < 2:
            return []
        results = sites.search_all(self._contexts(), q)[:_MAX_RESULTS]
        return [self._series_item(s) for s in results]

    def detail(self, series_ref: str) -> dict:
        """Dizinin sezon/bölüm listesi; film ise tek "bölüm"."""
        from hayalet.core import sites
        (series,) = self._get(series_ref, "series")
        net, session = self._context_for(series.site)
        adapter = sites.SITES[series.site]
        episodes = adapter.get_episodes(net, session, series)
        if not episodes:
            raise CatalogError("bu içeriğin bölümü bulunamadı")
        key = f"{series.site}:{series.slug}"
        self._episodes[key] = episodes
        movie = _is_movie(series)
        seasons: "OrderedDict[int, list]" = OrderedDict()
        for ep in sorted(episodes, key=lambda e: (e.season, e.number)):
            seasons.setdefault(ep.season, []).append({
                "ref": self._put("episode", series, ep),
                "season": ep.season, "number": ep.number,
                "label": _episode_label(ep, movie)})
        return {**self._series_item(series, ref=series_ref),
                "seasons": [{"season": s, "episodes": eps}
                            for s, eps in seasons.items()]}

    def episode_refs(self, series: Series, episodes: list[Episode]) -> list[str]:
        """Uygulamanın zaten elindeki bölüm listesini kaydeder, her bölüm
        için ref döndürür (sıra korunur). "Sonraki bölüm" bu listeyle çalışır."""
        self._episodes[f"{series.site}:{series.slug}"] = list(episodes)
        return [self._put("episode", series, ep) for ep in episodes]

    def resolve(self, episode_ref: str) -> Resolved:
        """Bölümün akışını çözer (saniyeler sürebilir)."""
        from hayalet.core import sites
        series, ep = self._get(episode_ref, "episode")
        net, session = self._context_for(series.site)
        stream = self._take_prefetched(series, ep) or             sites.SITES[series.site].build_stream(net, session, ep, series)
        movie = _is_movie(series)
        subs = ([{"url": stream.subtitle_url, "label": "Türkçe"}]
                if getattr(stream, "subtitle_url", None) else [])
        return Resolved(
            url=stream.video_master_url, referer=stream.video_referer,
            subtitles=subs, user_agent=session.user_agent,
            impersonate=session.impersonate,
            cookies=dict(session.cookies or {}),
            now={"kind": "hayalet", "ref": episode_ref,
                 "title": series.name,
                 "subtitle": "" if movie else _episode_label(ep, False, full=True),
                 "poster": series.poster_url or "",
                 "hasNext": self._next(series, ep) is not None,
                 # Kalıcı kimlik (ref yalnız bu oturumda geçerli): uygulama
                 # birlikte izleme geçmişine bunu yazar, sonra arama yapmadan
                 # yeniden açar (hayalet_app.episodes_key ile aynı alanlar).
                 "key": {"site": series.site, "slug": series.slug,
                         "type": series.type, "name": series.name,
                         "poster": series.poster_url or "",
                         "season": ep.season, "number": ep.number}})

    def prefetch_next(self, episode_ref: str) -> None:
        """Sıradaki bölümün akışını arka planda çözer; resolve() hazır
        sonucu alır. Tek işçi: aynı anda birden fazla ön çözüm olmasın."""
        import concurrent.futures
        import time
        from hayalet.core import sites
        from hayalet.core.network import Network
        try:
            series, ep = self._get(episode_ref, "episode")
        except CatalogError:
            return
        nxt = self._next(series, ep)
        if nxt is None:
            return
        key = (series.site, series.slug, nxt.season, nxt.number)
        hit = self._streams.get(key)
        if hit and time.time() - hit[0] < _PREFETCH_TTL:
            return
        _net, session = self._context_for(series.site)
        if self._pf_pool is None:
            self._pf_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # Kendi istemcisiyle: paylaşılan curl-cffi istemcisi aynı anda iki
        # iş parçacığından kullanılamaz.
        self._streams[key] = (time.time(), self._pf_pool.submit(
            sites.SITES[series.site].build_stream, Network(session), session, nxt, series))

    def _take_prefetched(self, series: Series, ep: Episode):
        import time
        hit = self._streams.pop((series.site, series.slug, ep.season, ep.number), None)
        if not hit or time.time() - hit[0] >= _PREFETCH_TTL:
            return None
        try:
            return hit[1].result()   # sürüyorsa bekler: baştan çözmekten hızlı
        except Exception:
            return None

    def next_of(self, episode_ref: str) -> str | None:
        """Sıradaki bölümün ref'i; son bölümse ya da bilinmiyorsa None."""
        try:
            series, ep = self._get(episode_ref, "episode")
        except CatalogError:
            return None
        nxt = self._next(series, ep)
        return self._put("episode", series, nxt) if nxt else None

    def _next(self, series: Series, ep: Episode) -> Episode | None:
        eps = self._episodes.get(f"{series.site}:{series.slug}")
        return next_episode(eps, ep) if eps else None

    def _series_item(self, s: Series, ref: str | None = None) -> dict:
        return {"ref": ref or self._put("series", s), "title": s.name,
                "year": s.year or "", "kind": "film" if _is_movie(s) else "dizi",
                "site": s.site, "poster": s.poster_url or ""}
