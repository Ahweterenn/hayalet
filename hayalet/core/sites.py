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


SITES: dict[str, SiteAdapter] = {}


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


def search_all(contexts: dict[str, tuple[Network, SessionState]], query: str) -> list[Series]:
    """Tüm sitelerde paralel arar, sonuçları birleştirir (her Series.site dolu)."""
    return _run_all("search", contexts, query)


def suggest_all(contexts: dict[str, tuple[Network, SessionState]], query: str) -> list[Series]:
    """search_all sonuç vermediğinde tüm sitelerde paralel öneri arar."""
    return _run_all("suggest", contexts, query)
