"""Site bağımsız veri modelleri — hiçbir sitenin özel mantığını içermez.

`Series`/`Episode`, tüm site adapter'larının ortak "değer nesneleri"; buradaki
liste yardımcıları da (match_episode/next_episode/seasons_of/episodes_in_season)
saf liste işlemleri olduğundan her sitede aynen kullanılır.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Series:
    name: str
    slug: str          # örn. "series/house-md-turkce-dublaj"
    type: str = "Series"
    site: str = ""      # bu sonucu üreten adapter'ın adı (bkz. core/sites.py SITES)

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


def match_episode(episodes: list[Episode], season: int, number: int) -> "Episode | None":
    """Aynı (sezon, bölüm); bulunamazsa aynı bölüm numarası."""
    return (next((e for e in episodes if e.season == season and e.number == number), None)
            or next((e for e in episodes if e.number == number), None))


def next_episode(episodes: list[Episode], current: Episode) -> "Episode | None":
    """Sezon sınırını da aşarak (dizinin tamamı üzerinden) bir sonraki bölümü bulur."""
    ordered = sorted(episodes, key=lambda e: (e.season, e.number))
    try:
        idx = next(i for i, e in enumerate(ordered)
                   if e.season == current.season and e.number == current.number)
    except StopIteration:
        return None
    return ordered[idx + 1] if idx + 1 < len(ordered) else None


def seasons_of(episodes: list[Episode]) -> list[int]:
    return sorted({e.season for e in episodes})


def episodes_in_season(episodes: list[Episode], season: int) -> list[Episode]:
    return [e for e in episodes if e.season == season]
