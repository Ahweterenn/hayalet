"""Arama + dizi/sezon/bölüm gezinme.

Arama: POST /bg/searchcontent (JSON döner).
Bölümler: /series/<slug> sayfasındaki /bolum/<...-SxE> linklerinden türetilir.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from hayalet import config
from hayalet.core.network import Network
from hayalet.core.session import SessionState


@dataclass
class Series:
    name: str
    slug: str          # örn. "series/house-md-turkce-dublaj"
    type: str = "Series"

    def url(self, base_url: str) -> str:
        return f"{base_url}/{self.slug.lstrip('/')}"


@dataclass
class Episode:
    season: int
    number: int
    url: str
    title: str = ""

    @property
    def label(self) -> str:
        return self.title or f"Sezon {self.season} · Bölüm {self.number}"


# --- Arama ----------------------------------------------------------------
def search(net: Network, session: SessionState, query: str) -> list[Series]:
    home = net.get(session.base_url).text
    m = re.search(r'name="cValue"\s+value="([^"]+)"', home)
    cvalue = m.group(1) if m else ""

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


# --- Bölümler -------------------------------------------------------------
def get_episodes(net: Network, session: SessionState, series: Series) -> list[Episode]:
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


def match_episode(episodes: list[Episode], season: int, number: int) -> "Episode | None":
    """Aynı (sezon, bölüm); bulunamazsa aynı bölüm numarası."""
    return (next((e for e in episodes if e.season == season and e.number == number), None)
            or next((e for e in episodes if e.number == number), None))


def seasons_of(episodes: list[Episode]) -> list[int]:
    return sorted({e.season for e in episodes})


def episodes_in_season(episodes: list[Episode], season: int) -> list[Episode]:
    return [e for e in episodes if e.season == season]
