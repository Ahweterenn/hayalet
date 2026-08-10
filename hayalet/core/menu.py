"""questionary tabanlı hiyerarşik menü: arama → dizi/film → (sezon → mod → bölüm)."""
from __future__ import annotations

import questionary
from rich.console import Console

from hayalet.core import catalog, sites
from hayalet.core.catalog import Episode, Series

_console = Console()

_STYLE = questionary.Style([
    ("qmark", "fg:#ff3130 bold"),
    ("pointer", "fg:#ff3130 bold"),
    ("highlighted", "fg:#ff3130 bold"),
    ("answer", "fg:#00b894 bold"),
])

_TYPE_TR = {"series": "Dizi", "movies": "Film", "movie": "Film", "film": "Film"}


def _type_tr(t: str) -> str:
    """Ham tür etiketini Türkçeleştirir (Series→Dizi, Movies/Film→Film)."""
    return _TYPE_TR.get((t or "").lower(), t or "")


def prompt_resume_series(resume: dict) -> Series | None:
    """Kayıtlı 'kaldığın yerden devam' işaretini onaylatır; onaylanırsa Series döner
    (arama yapılmadan, kayıtlı slug'dan doğrudan kurulur)."""
    label = f"{resume['series_name']} · S{int(resume['season']):02d}E{int(resume['episode']):02d}"
    position = float(resume.get("position") or 0)
    if position >= 20:
        label += f"  ({int(position) // 60:02d}:{int(position) % 60:02d})"
    ok = questionary.confirm(f"Kaldığın yerden devam et: {label}?",
                             default=True, style=_STYLE).ask()
    if not ok:
        return None
    return Series(name=resume["series_name"], slug=resume["series_slug"],
                 type=resume.get("series_type", "Series"),
                 site=resume.get("site", "dizipal"))


def _series_label(r: Series, show_site: bool) -> str:
    label = f"{r.name}  ·  {_type_tr(r.type)}"
    return f"{label}  ·  {r.site}" if show_site else label


def prompt_search_series(contexts: dict) -> Series | None:
    """contexts: site adı -> (Network, SessionState). Birden fazla site aktifse
    (varsayılan interaktif mod) hepsi paralel aranır, sonuç listesinde site adı
    da gösterilir; tek site aktifse (--site ile sabitlenmiş) eski davranış aynen
    korunur."""
    show_site = len(contexts) > 1
    while True:
        query = questionary.text("🔎 Dizi/film ara:", style=_STYLE).ask()
        if query is None:
            return None
        query = query.strip()
        if not query:
            continue
        with _console.status("[bright_red]Aranıyor…", spinner="dots"):
            results = sites.search_all(contexts, query)
        header = f"{len(results)} sonuç bulundu:"
        if not results:
            with _console.status("[bright_red]Öneri aranıyor…", spinner="dots"):
                suggestions = sites.suggest_all(contexts, query)
            if not suggestions:
                _console.print("  [yellow]Sonuç yok, tekrar deneyin.[/]")
                continue
            results = suggestions
            header = "Sonuç yok. Şunu mu demek istediniz?"
        choices = [questionary.Choice(_series_label(r, show_site), value=r) for r in results]
        choices.append(questionary.Choice("↩ Yeniden ara", value="__again__"))
        pick = questionary.select(header, choices=choices, style=_STYLE).ask()
        if pick is None:
            return None
        if pick == "__again__":
            continue
        return pick


ALL_SEASONS = "__all__"


def prompt_season(episodes: list[Episode], default_season: int | None = None) -> "int | str | None":
    """Sezon seç; birden çok sezon varsa 'Tüm dizi' seçeneğini de sunar."""
    seasons = catalog.seasons_of(episodes)
    if len(seasons) == 1:
        return seasons[0]
    choices = [questionary.Choice("⭐ Tüm sezonlar (tüm dizi)", value=ALL_SEASONS)]
    choices += [questionary.Choice(f"Sezon {s}", value=s) for s in seasons]
    default = next((c for c, s in zip(choices[1:], seasons) if s == default_season), None)
    return questionary.select("Sezon seç:", choices=choices, default=default,
                              style=_STYLE).ask()


def prompt_movie_action(name: str) -> str:
    """Film için sade menü (sezon/bölüm yok). Dönüş: 'watch'|'download'|'search'|'exit'."""
    ans = questionary.select(
        f"🎬 {name}",
        choices=[
            questionary.Choice("▶  İzle", value="watch"),
            questionary.Choice("⬇  İndir", value="download"),
            questionary.Choice("🔁 Yeni arama", value="search"),
            questionary.Choice("🚪 Çıkış", value="exit"),
        ],
        style=_STYLE,
    ).ask()
    return ans or "exit"


def prompt_mode() -> str | None:
    return questionary.select(
        "Ne yapmak istersin?",
        choices=[
            questionary.Choice("▶  İzle (tek bölüm)", value="watch"),
            questionary.Choice("⬇  İndir (çoklu bölüm)", value="download"),
            questionary.Choice("↩ Geri (yeni dizi/film)", value=None),
        ],
        style=_STYLE,
    ).ask()


_BACK = "__back__"


def prompt_single_episode(episodes: list[Episode],
                          default_episode: int | None = None) -> Episode | None:
    choices = [questionary.Choice(e.label, value=e) for e in episodes]
    choices.append(questionary.Choice("↩ Geri", value=_BACK))
    default = next((c for c, e in zip(choices, episodes)
                    if e.number == default_episode), None)
    ans = questionary.select("Bölüm seç:", choices=choices, default=default,
                             style=_STYLE).ask()
    return None if ans in (None, _BACK) else ans


def prompt_multi_episodes(episodes: list[Episode]) -> list[Episode] | None:
    picks = questionary.checkbox(
        "Bölümleri seç  (boşluk=işaretle · a=tüm sezon · i=tersle · enter=onayla):",
        choices=[questionary.Choice(e.label, value=e) for e in episodes],
        style=_STYLE,
    ).ask()
    return picks or None


def prompt_continue(series_name: str | None = None) -> str:
    """Aksiyon sonrası: aynı dizide devam / yeni arama / çıkış. Dönüş: 'same'|'search'|'exit'."""
    choices = []
    if series_name:
        choices.append(questionary.Choice(f"↩ {series_name} içinde devam et", value="same"))
    choices.append(questionary.Choice("🔁 Yeni arama", value="search"))
    choices.append(questionary.Choice("🚪 Çıkış", value="exit"))
    ans = questionary.select("Sırada ne var?", choices=choices, style=_STYLE).ask()
    return ans or "exit"


def prompt_retry_failed(n: int) -> bool:
    """İndirme kuyruğu sonunda başarısız bölümleri tekrar denemeyi sorar."""
    return bool(questionary.confirm(
        f"{n} bölüm başarısız oldu. Sadece başarısızları tekrar dene?",
        default=True, style=_STYLE,
    ).ask())


def prompt_download_dir(default_base: str, series_name: str | None) -> str | None:
    """İndirme öncesi ANA klasörü sorar (dizi/film adı alt klasörü otomatik
    eklenir, bkz. cli._out_dir) — varsayılan önceden dolu gelir, Enter ile
    aynen kabul edilir. None dönerse kullanıcı iptal etti (Ctrl+C/Esc)."""
    hint = f"  (altına '{series_name}/' klasörü açılır)" if series_name else ""
    ans = questionary.path(
        f"İndirilecek ana klasör{hint}:", default=default_base,
        only_directories=True, style=_STYLE,
    ).ask()
    return ans or None
