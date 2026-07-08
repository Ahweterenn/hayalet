"""Dizipal site adapter'ı — ince sarmalayıcı, sıfır mantık değişikliği.

Tüm gerçek iş zaten `core/catalog.py`, `core/resolver.py`, `core/merge.py`'de
var; bu dosya sadece onları SiteAdapter arayüzüne uydurur. Var olan Dizipal
davranışının birebir aynı kalmasını garantilemek için buraya yeni mantık
EKLENMEZ — iki istisna dışında: (1) çoklu-site aramada sonucun kaynağını ayırt
etmek için Series.site damgalama, (2) iş bölümü gereği "Movies" tipi sonuçların
elenmesi (filmler hdfilmcehennemi'nin işi — bkz. search/suggest).
"""
from __future__ import annotations

from hayalet import config
from hayalet.core import catalog, merge, resolver
from hayalet.core.merge import MergedStream
from hayalet.core.models import Episode, Series
from hayalet.core.network import Network
from hayalet.core.session import SessionState
from hayalet.core.sites import register


class DizipalAdapter:
    name = "dizipal"
    known_domain = config.KNOWN_DOMAIN

    def resolve_domain(self, net: Network, session: SessionState,
                       override: str | None = None, use_cache: bool = True) -> str:
        return resolver.resolve(net, session, override=override, use_cache=use_cache)

    def search(self, net: Network, session: SessionState, query: str) -> list[Series]:
        # İş bölümü: filmler hdfilmcehennemi'nin işi, dizipal yalnızca dizi döndürür
        # (Dizipal'in kendi "Movies" kategorisi elenir — aksi halde çift-site
        # aramada aynı film iki kaynaktan da çıkıp kafa karıştırırdı).
        results = [r for r in catalog.search(net, session, query) if not catalog.is_movie(r)]
        for r in results:
            r.site = self.name
        return results

    def suggest(self, net: Network, session: SessionState, query: str) -> list[Series]:
        results = [r for r in catalog.suggest(net, session, query) if not catalog.is_movie(r)]
        for r in results:
            r.site = self.name
        return results

    def get_episodes(self, net: Network, session: SessionState,
                     series: Series) -> list[Episode]:
        return catalog.get_episodes(net, session, series)

    def build_stream(self, net: Network, session: SessionState,
                     episode: Episode, series: Series) -> MergedStream:
        return merge.build_merged(net, session, episode, series)


register(DizipalAdapter())
