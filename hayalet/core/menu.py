"""questionary tabanlı hiyerarşik menü: arama → dizi → sezon → mod → bölüm(ler) → kalite."""
from __future__ import annotations

import questionary

from hayalet.core import catalog
from hayalet.core.catalog import Episode, Series
from hayalet.core.m3u8_parser import Variant
from hayalet.core.network import Network
from hayalet.core.session import SessionState

_STYLE = questionary.Style([
    ("qmark", "fg:#ff3130 bold"),
    ("pointer", "fg:#ff3130 bold"),
    ("highlighted", "fg:#ff3130 bold"),
    ("answer", "fg:#00b894 bold"),
])


def prompt_search_series(net: Network, session: SessionState) -> Series | None:
    while True:
        query = questionary.text("Dizi/film ara:", style=_STYLE).ask()
        if query is None:
            return None
        query = query.strip()
        if not query:
            continue
        results = catalog.search(net, session, query)
        if not results:
            print("  Sonuç yok, tekrar deneyin.")
            continue
        choices = [questionary.Choice(f"{r.name}  ·  {r.type}", value=r) for r in results]
        choices.append(questionary.Choice("↩ Yeniden ara", value="__again__"))
        pick = questionary.select("Sonuçlar:", choices=choices, style=_STYLE).ask()
        if pick is None:
            return None
        if pick == "__again__":
            continue
        return pick


ALL_SEASONS = "__all__"


def prompt_season(episodes: list[Episode]) -> "int | str | None":
    """Sezon seç; birden çok sezon varsa 'Tüm dizi' seçeneğini de sunar."""
    seasons = catalog.seasons_of(episodes)
    if len(seasons) == 1:
        return seasons[0]
    choices = [questionary.Choice("⭐ Tüm sezonlar (tüm dizi)", value=ALL_SEASONS)]
    choices += [questionary.Choice(f"Sezon {s}", value=s) for s in seasons]
    return questionary.select("Sezon seç:", choices=choices, style=_STYLE).ask()


def prompt_mode() -> str | None:
    return questionary.select(
        "Ne yapmak istersin?",
        choices=[
            questionary.Choice("▶  İzle (tek bölüm)", value="watch"),
            questionary.Choice("⬇  İndir (çoklu bölüm)", value="download"),
        ],
        style=_STYLE,
    ).ask()


def prompt_single_episode(episodes: list[Episode]) -> Episode | None:
    return questionary.select(
        "Bölüm seç:",
        choices=[questionary.Choice(e.label, value=e) for e in episodes],
        style=_STYLE,
    ).ask()


def prompt_multi_episodes(episodes: list[Episode]) -> list[Episode] | None:
    picks = questionary.checkbox(
        "Bölümleri seç  (boşluk=işaretle · a=tüm sezon · i=tersle · enter=onayla):",
        choices=[questionary.Choice(e.label, value=e) for e in episodes],
        style=_STYLE,
    ).ask()
    return picks or None


def prompt_continue() -> bool:
    """Aksiyon sonrası: yeni arama mı, çıkış mı?"""
    ans = questionary.select(
        "Sırada ne var?",
        choices=[
            questionary.Choice("🔁 Yeni arama", value=True),
            questionary.Choice("🚪 Çıkış", value=False),
        ],
        style=_STYLE,
    ).ask()
    return bool(ans)


def prompt_retry_failed(n: int) -> bool:
    """İndirme kuyruğu sonunda başarısız bölümleri tekrar denemeyi sorar."""
    return bool(questionary.confirm(
        f"{n} bölüm başarısız oldu. Sadece başarısızları tekrar dene?",
        default=True, style=_STYLE,
    ).ask())


def prompt_quality(variants: list[Variant]) -> Variant | None:
    if len(variants) <= 1:
        return variants[0] if variants else None
    return questionary.select(
        "Hangi kaliteyi istiyorsunuz?",
        choices=[questionary.Choice(v.label, value=v) for v in variants],
        style=_STYLE,
    ).ask()
