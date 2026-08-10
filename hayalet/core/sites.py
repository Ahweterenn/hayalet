"""Site adapter arayüzü + kayıt defteri.

Her site (Dizipal, hdfilmcehennemi, ...) bu Protocol'ü karşılayan bir adapter
sağlar; `cli.py` hangi siteyle çalıştığını bilmeden (search/get_episodes/
build_stream) çağırır. Ortak katmanlar (proxy, ffmpeg mux, menü akışı, oturum/
ağ, kimlik rotasyonu) hiçbir adapter'a bağımlı değildir, hiç değişmez.

Adapter'lar `hayalet/sites/` altında yaşar ve import edildiklerinde kendilerini
`register()` ile buraya kaydederler (bkz. hayalet/sites/__init__.py).
"""
from __future__ import annotations

import concurrent.futures
from typing import Protocol

from hayalet.core import query as q
from hayalet.core.merge import MergedStream
from hayalet.core.models import Episode, Series
from hayalet.core.network import Network
from hayalet.core.session import SessionState


class SiteAdapter(Protocol):
    name: str
    known_domain: str

    def resolve_domain(self, net: Network, session: SessionState,
                       override: str | None = None, use_cache: bool = True) -> str: ...

    def search(self, net: Network, session: SessionState, query: str) -> list[Series]: ...

    def suggest(self, net: Network, session: SessionState, query: str) -> list[Series]: ...

    def get_episodes(self, net: Network, session: SessionState,
                     series: Series) -> list[Episode]: ...

    def build_stream(self, net: Network, session: SessionState,
                     episode: Episode, series: Series) -> MergedStream: ...

    # --- isteğe bağlı: katalog gezinme ------------------------------------
    # Aşağıdaki ikisi Protocol'ün zorunlu parçası DEĞİL (bkz. home_rows/browse
    # yardımcıları): destekleyen adapter uygular, desteklemeyen hiç yazmaz ve
    # arayüz o siteyi gezinme listelerinde atlar. Böylece Dizipal gibi yalnızca
    # arama sunan bir site için sahte/boş uygulama yazmak gerekmiyor.


SITES: dict[str, SiteAdapter] = {}


def home_rows(contexts: dict[str, tuple[Network, SessionState]]) -> list[dict]:
    """Ana sayfa rafları: [{"title": "Nette İlk", "items": [Series, ...]}, ...].

    Destekleyen her siteden paralel toplanır; desteklemeyen ya da hata veren
    site sessizce atlanır (bir sitenin çökmesi ana sayfayı boşaltmasın).
    """
    return _collect(contexts, "home_rows")


def browse(contexts: dict[str, tuple[Network, SessionState]], kind: str) -> list[Series]:
    """kind = "dizi" | "film" — katalog listeleme sayfaları, arama olmadan."""
    return _collect(contexts, "browse", kind)


def _collect(contexts: dict[str, tuple[Network, SessionState]],
             method: str, *args) -> list:
    out: list = []
    ready = {n: c for n, c in contexts.items() if hasattr(SITES[n], method)}
    if not ready:
        return out
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ready)) as ex:
        futs = [ex.submit(getattr(SITES[n], method), net, session, *args)
                for n, (net, session) in ready.items()]
        for fut in concurrent.futures.as_completed(futs):
            try:
                out.extend(fut.result() or [])
            except Exception:
                pass
    return out


def register(adapter: SiteAdapter) -> None:
    SITES[adapter.name] = adapter


# --- Çoklu-site paralel arama ----------------------------------------------
# `contexts`: site adı -> o sitenin (Network, SessionState) çifti. Siteler
# birbirinden tamamen bağımsız Network/SessionState kullanır (paylaşılan
# mutable state yok), o yüzden eşzamanlı çağrı güvenlidir.
def _run_all(method_name: str, contexts: dict[str, tuple[Network, SessionState]],
            query: str) -> list[Series]:
    if not contexts:
        return []
    results: list[Series] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(contexts)) as ex:
        futs = {
            ex.submit(getattr(SITES[name], method_name), net, session, query): name
            for name, (net, session) in contexts.items()
        }
        for fut in concurrent.futures.as_completed(futs):
            try:
                results.extend(fut.result())
            except Exception:
                pass  # bir site başarısız olursa diğerinin sonucu yine gösterilsin
    return results


# Sonuç sayısı bunun altındaysa liste "zayıf" sayılır ve yazım varyantları da
# denenir — asıl sorgu bir şeyler bulmuş olsa bile.
_ENOUGH_RESULTS = 5


def search_site(adapter: SiteAdapter, net: Network, session: SessionState,
                term: str) -> list[Series]:
    """Tek sitede "toleranslı" arama: gerekirse alternatif yazımları da dener.

    Sitelerin arama backend'i harfi harfine eşleşme arıyor (canlı: `spider-man`
    sonuç veriyor, `spiderman` hiçbir şey döndürmüyor). Asıl sorgu yeterince iyi
    bir sonuç vermezse `query.variants` ile bir avuç makul yazım **paralel**
    denenir ve birleşik liste benzerliğe göre sıralanıp eşiğin altı atılır —
    yani ağ genişler ama liste çöple dolmaz. Sorgu ilk denemede tuttuğunda
    (yaygın durum) hiç ek istek yapılmaz.
    """
    results = adapter.search(net, session, term)
    # Varyantları atlamak için TEK bir iyi sonuç yetmiyor: "spiderman" sitede
    # sadece "Vjeran Tomic: The Spider-Man of Paris"i getiriyordu — başlık
    # sorguyu içerdiği için puanı yüksek çıkıyor ama asıl aranan filmler listede
    # yok. O yüzden ek koşul: liste zaten doyurucu uzunlukta olmalı.
    if len(results) >= _ENOUGH_RESULTS and q.best_score(term, results) >= q.GOOD_SCORE:
        return q.rank(term, results)

    alts = q.variants(term)
    if alts:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(alts)) as ex:
            for fut in [ex.submit(adapter.search, net, session, a) for a in alts]:
                try:
                    results.extend(fut.result())
                except Exception:
                    pass  # varyantın başarısızlığı asıl sonucu götürmesin

    seen: set[str] = set()
    unique = [r for r in results
              if not (r.slug in seen or seen.add(r.slug))]
    return q.rank(term, unique)


def search_all(contexts: dict[str, tuple[Network, SessionState]], query: str) -> list[Series]:
    """Tüm sitelerde paralel arar, sonuçları birleştirir (her Series.site dolu).

    Her site kendi içinde `search_site` ile varyant denemesi yapar; birleşik
    liste en sonunda tek seferde sorguya benzerliğe göre sıralanır (birebir
    eşleşmeler, hangi siteden gelirse gelsin, en üstte)."""
    if not contexts:
        return []
    results: list[Series] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(contexts)) as ex:
        futs = [ex.submit(search_site, SITES[name], net, session, query)
                for name, (net, session) in contexts.items()]
        for fut in concurrent.futures.as_completed(futs):
            try:
                results.extend(fut.result())
            except Exception:
                pass  # bir site başarısız olursa diğerinin sonucu yine gösterilsin
    return q.rank(query, results)


def suggest_all(contexts: dict[str, tuple[Network, SessionState]], query: str) -> list[Series]:
    """search_all sonuç vermediğinde tüm sitelerde paralel öneri arar."""
    return q.rank(query, _run_all("suggest", contexts, query))
