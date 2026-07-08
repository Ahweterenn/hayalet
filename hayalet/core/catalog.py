"""Dizipal'e özel arama + dizi/sezon/bölüm gezinme.

Arama: POST /bg/searchcontent (JSON döner).
Bölümler: /series/<slug> sayfasındaki /bolum/<...-SxE> linklerinden türetilir.

Site-bağımsız veri modelleri (Series/Episode) ve saf liste yardımcıları
core/models.py'ye taşındı; burada geriye dönük uyumluluk için re-export
edilir (mevcut `from hayalet.core.catalog import Episode, Series` importları
değişmeden çalışmaya devam eder).
"""
from __future__ import annotations

import re

from hayalet import config
from hayalet.core.models import (Episode, Series, episodes_in_season,
                                 match_episode, next_episode, seasons_of)
from hayalet.core.network import Network
from hayalet.core.session import SessionState

__all__ = ["Series", "Episode", "search", "suggest", "is_movie", "get_episodes",
          "is_dubbed", "base_title", "find_counterpart", "find_original_counterpart",
          "match_episode", "next_episode", "seasons_of", "episodes_in_season"]


# --- Arama ----------------------------------------------------------------
# cValue, ana sayfadan kazınan bir arama token'ı — her aramada sayfayı yeniden
# çekmek yerine process başına bir kez alınır (siteye giden gereksiz isteği
# azaltır). Domain değişirse (nadir) otomatik yeniden çekilir.
_cvalue_cache: dict[str, str] = {}


def _cvalue(net: Network, session: SessionState) -> str:
    cached = _cvalue_cache.get(session.base_url)
    if cached is not None:
        return cached
    home = net.get(session.base_url).text
    m = re.search(r'name="cValue"\s+value="([^"]+)"', home)
    cvalue = m.group(1) if m else ""
    _cvalue_cache[session.base_url] = cvalue
    return cvalue


def search(net: Network, session: SessionState, query: str) -> list[Series]:
    cvalue = _cvalue(net, session)

    resp = net.post(
        session.base_url + config.SEARCH_ENDPOINT,
        data={"searchterm": query, "cValue": cvalue},
        referer=session.base_url,
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    try:
        data = resp.json()
    except Exception:
        import json
        data = json.loads(resp.text)

    results = (data.get("data") or {}).get("result") or []
    out: list[Series] = []
    for r in results:
        slug = r.get("used_slug") or ""
        name = r.get("object_name") or slug
        typ = r.get("used_type") or "Series"
        if slug:
            out.append(Series(name=name, slug=slug, type=typ))
    return out


def suggest(net: Network, session: SessionState, query: str, limit: int = 5) -> list[Series]:
    """Arama sonuç vermediğinde olası eşleşmeleri döndürür ("şunu mu demek istedin?").

    Sorgudaki kelimeleri (en uzundan başlayarak) tek tek arayıp bulunan adayları
    orijinal sorguya benzerliğine göre sıralar — yazım hatalarına karşı basit tolerans.
    """
    import difflib

    words = sorted((w for w in re.split(r"\s+", query.strip()) if len(w) >= 3),
                   key=len, reverse=True)

    candidates: dict[str, Series] = {}
    for w in words:
        for r in search(net, session, w):
            candidates.setdefault(r.slug, r)
        if candidates:
            break
    if not candidates:
        return []

    def score(r: Series) -> float:
        return difflib.SequenceMatcher(None, r.name.lower(), query.lower()).ratio()

    ranked = sorted(candidates.values(), key=score, reverse=True)
    return [r for r in ranked if score(r) >= 0.4][:limit]


_MOVIE_TYPES = {"movies", "movie", "film"}


def is_movie(series: "Series") -> bool:
    return series.type.lower() in _MOVIE_TYPES


# --- Bölümler -------------------------------------------------------------
def get_episodes(net: Network, session: SessionState, series: Series) -> list[Episode]:
    if is_movie(series):
        # Movie'lerde /bolum/ alt sayfası yok — kaynak (data-rm-k) doğrudan
        # filmin kendi sayfasında. Tek "bölüm" olarak filmin URL'sini döndür.
        return [Episode(season=1, number=1, url=series.url(session.base_url),
                        title=series.name)]

    html = net.get(series.url(session.base_url), referer=session.base_url).text

    # Tüm /bolum/ linklerini topla
    links = re.findall(r'href="([^"]*?/bolum/[a-z0-9\-]+)"', html, re.I)
    sxe_re = re.compile(config.SELECTORS["sxe_from_slug"], re.I)

    seen: set[str] = set()
    episodes: list[Episode] = []
    for link in links:
        url = link if link.startswith("http") else session.base_url + \
            ("" if link.startswith("/") else "/") + link
        if url in seen:
            continue
        seen.add(url)
        slug = url.rsplit("/bolum/", 1)[-1]
        mm = sxe_re.search(slug)
        if not mm:
            continue
        episodes.append(Episode(
            season=int(mm.group(1)),
            number=int(mm.group(2)),
            url=url,
        ))

    episodes.sort(key=lambda e: (e.season, e.number))
    return episodes


_DUB_RE = re.compile(r"\b(t[uü]rk[cç]e\s*dublaj|dublaj)\b", re.I)


def is_dubbed(series: "Series") -> bool:
    return bool(_DUB_RE.search(series.name))


def base_title(name: str) -> str:
    """'... Türkçe Dublaj' -> '...' (dublaj etiketini temizler)."""
    return _DUB_RE.sub("", name).strip(" -–—·")


def find_counterpart(net: Network, session: SessionState, series: "Series",
                     want_dubbed: bool) -> "Series | None":
    """Bir dizinin karşı sürümünü (dublaj↔orijinal) arama ile bulur.

    want_dubbed=True  → dublajlı muadili ara (orijinalden yola çıkarak)
    want_dubbed=False → orijinal/altyazılı muadili ara (dublajdan yola çıkarak)
    """
    import difflib

    base = base_title(series.name)
    if not base:
        return None
    cands = [r for r in search(net, session, base)
             if r.type == series.type and is_dubbed(r) == want_dubbed
             and r.slug != series.slug]
    if not cands:
        return None

    def score(r: Series) -> float:
        return difflib.SequenceMatcher(
            None, base_title(r.name).lower(), base.lower()).ratio()

    cands.sort(key=score, reverse=True)
    # Anlamlı bir eşleşme yoksa boş dön
    return cands[0] if score(cands[0]) >= 0.6 else None


def find_original_counterpart(net: Network, session: SessionState,
                              dubbed: "Series") -> "Series | None":
    """Dublajlı bir dizinin orijinal/altyazılı muadilini bulur (geriye dönük ad)."""
    return find_counterpart(net, session, dubbed, want_dubbed=False)


