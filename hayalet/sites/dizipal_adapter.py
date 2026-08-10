"""Dizipal site adapter'ı — ince sarmalayıcı, sıfır mantık değişikliği.

Tüm gerçek iş zaten `core/catalog.py`, `core/resolver.py`, `core/merge.py`'de
var; bu dosya sadece onları SiteAdapter arayüzüne uydurur. Var olan Dizipal
davranışının birebir aynı kalmasını garantilemek için buraya yeni mantık
EKLENMEZ — iki istisna dışında: (1) çoklu-site aramada sonucun kaynağını ayırt
etmek için Series.site damgalama, (2) iş bölümü gereği "Movies" tipi sonuçların
elenmesi (filmler hdfilmcehennemi'nin işi — bkz. search/suggest).
"""
from __future__ import annotations

import html as _html
import re
from urllib.parse import urlparse

from hayalet import config
from hayalet.core import catalog, merge, resolver
from hayalet.core.merge import MergedStream
from hayalet.core.models import Episode, Series
from hayalet.core.network import Network
from hayalet.core.session import SessionState
from hayalet.core.sites import register


# --- katalog gezinme -------------------------------------------------------
# Ana sayfadaki "Trend Diziler" şeridi BİLEREK kullanılmıyor: o kartlarda yalnız
# poster ve sıra numarası var, yapım adı hiç geçmiyor (canlı doğrulandı) — poster
# göstermediğimiz için adsız kart işe yaramaz. Dizi listeleme sayfasındaki
# kartlarda ise ad `title="... izle"` niteliğinde duruyor.
_LIST_PATH = "/yabanci-dizi-izle"
_CARD_RE = re.compile(
    r'<a[^>]+href="([^"]*?/series/[a-z0-9\-]+)"[^>]*?title="([^"]*?)\s*izle"', re.S)
_HOME_ROW_LIMIT = 20


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

    def browse(self, net: Network, session: SessionState, kind: str) -> list[Series]:
        """Dizi listeleme sayfası (arama yapmadan katalog).

        Yalnızca "dizi": Dizipal'in film kategorisi search/suggest'te de eleniyor
        (filmler hdfilmcehennemi'nin işi), gezinmede de tutarlı kalsın.
        """
        if kind != "dizi":
            return []
        page = net.get(session.base_url + _LIST_PATH, referer=session.base_url).text
        out: list[Series] = []
        seen: set[str] = set()
        for href, title in _CARD_RE.findall(page):
            slug = urlparse(href).path.strip("/")
            name = _html.unescape(title).strip()
            if not slug or not name or slug in seen:
                continue
            seen.add(slug)
            out.append(Series(name=name, slug=slug, type="Series", site=self.name))
        return out

    def home_rows(self, net: Network, session: SessionState) -> list[dict]:
        items = self.browse(net, session, "dizi")[:_HOME_ROW_LIMIT]
        return [{"title": "Diziler", "items": items}] if items else []

    def get_episodes(self, net: Network, session: SessionState,
                     series: Series) -> list[Episode]:
        return catalog.get_episodes(net, session, series)

    def build_stream(self, net: Network, session: SessionState,
                     episode: Episode, series: Series) -> MergedStream:
        return merge.build_merged(net, session, episode, series)


register(DizipalAdapter())
