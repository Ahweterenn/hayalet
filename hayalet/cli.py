"""video-cli — Dizipal için tarayıcısız (headless) Python CLI.

Kullanım:
  python -m hayalet                     # interaktif menü (arama → dizi → sezon → bölüm → kalite)
  python -m hayalet --search "house of the dragon"        # aramayı listeler
  python -m hayalet --search "..." --series 1 --season 1 --episode 1 --action extract
  python -m hayalet --search "..." --series 1 --season 1 --episode 1 --action watch
  python -m hayalet --search "..." --series 1 --season 1 --episodes 1,2,3 --action download
"""
from __future__ import annotations

import argparse
import sys

# Windows konsolunda Türkçe/emoji için UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from rich.console import Console
from rich.panel import Panel
from rich.progress import (BarColumn, Progress, SpinnerColumn,
                           TaskProgressColumn, TextColumn, TimeRemainingColumn)
from rich.text import Text

from hayalet import config
from hayalet.core import (actions, catalog, extractor, logs, m3u8_parser, menu,
                         merge, resolver, utils)
from hayalet.core.network import Network
from hayalet.core.session import SessionState

console = Console()


def _banner():
    """Başlangıç başlığı — modern, kırmızı vurgulu."""
    title = Text()
    title.append("  ▎", style="bright_red")
    title.append("hayalet", style="bold bright_white")
    title.append("  tarayıcısız dizi/film aracı", style="dim")
    console.print(Panel(title, border_style="bright_red", padding=(0, 1)))


def _series_out_dir(series):
    """İndirmeleri downloads/<Dizi>/ altına klasörler (dosya adları zaten SxxExx)."""
    return config.DOWNLOAD_DIR / utils.safe_filename(series.name)


def _existing_file(out_dir, series, ep):
    """Bu bölüm daha önce inmiş mi? (.mkv/.mp4, >1MB) → dosya yolu | None."""
    safe = utils.safe_filename(f"{series.name} - S{ep.season:02d}E{ep.number:02d}")
    for ext in (".mkv", ".mp4"):
        f = out_dir / f"{safe}{ext}"
        try:
            if f.exists() and f.stat().st_size > 1_000_000:
                return f
        except OSError:
            pass
    return None


def _run_downloads(net, session, series, eps, out_dir):
    """Bir geçiş: ilk→son sırayla indirir. Dönüş: (ok, failed, skipped) bölüm listeleri."""
    total = len(eps)
    ok, failed, skipped = [], [], []
    with Progress(
        SpinnerColumn(style="bright_red"),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=None, style="grey30",
                  complete_style="bright_red", finished_style="green"),
        TaskProgressColumn(),
        TextColumn("[cyan]{task.fields[speed]}"),
        TimeRemainingColumn(compact=True),
        console=console,
        expand=True,
    ) as progress:
        overall = progress.add_task(f"Genel  (0/{total})", total=total, speed="")
        cur = progress.add_task("Sırada…", total=100, speed="")
        for idx, ep in enumerate(eps, 1):
            title = f"{series.name} - S{ep.season:02d}E{ep.number:02d}"
            existing = _existing_file(out_dir, series, ep)
            if existing:                     # zaten inmiş → atla (kaldığın yerden devam)
                progress.console.print(f"[dim]⏭  Atlandı (zaten var): {existing.name}[/dim]")
                logs.log.info("skip existing: %s", existing.name)
                skipped.append(ep)
                progress.update(overall, advance=1,
                                description=f"Genel  ({idx}/{total})")
                continue

            # Ağ takılması bir bölümü düşürürse bir kez daha dene (URL'leri tazeleyerek)
            rc = 1
            for attempt in range(2):
                progress.reset(cur, total=100, speed="", description=(
                    f"Çözülüyor: {ep.label}" if attempt == 0
                    else f"Tekrar deneniyor: {ep.label}"))
                try:
                    merged = merge.build_merged(net, session, ep, series)
                    rc = actions.download(session, net, merged, title, out_dir=out_dir,
                                          progress=progress, task_id=cur)
                except extractor.ExtractError as e:
                    progress.console.print(f"[yellow]Atlandı[/] {ep.label}: {e}")
                    logs.log.warning("extract failed %s: %s", title, e)
                    rc = 1
                    break
                if rc == 0:
                    break
                logs.log.warning("download rc=%s %s (deneme %d)", rc, title, attempt + 1)
            (ok if rc == 0 else failed).append(ep)
            progress.update(cur, completed=100, speed="")
            progress.update(overall, advance=1,
                            description=f"Genel  ({idx}/{total})")
        progress.update(cur, description="Bitti", completed=100)
    return ok, failed, skipped


def _download_queue(net, session, series, eps, interactive=True):
    """Kuyruğu indirir; sonunda özet gösterir ve (interaktifse) başarısızları tekrar sorar."""
    eps = sorted(eps, key=lambda e: (e.season, e.number))
    out_dir = _series_out_dir(series)
    ok, failed, skipped = _run_downloads(net, session, series, eps, out_dir)

    while True:
        parts = [f"[green]✔ {len(ok)} başarılı[/]"]
        if skipped:
            parts.append(f"[dim]⏭ {len(skipped)} atlandı[/]")
        if failed:
            fl = ", ".join(f"S{e.season:02d}E{e.number:02d}" for e in failed)
            parts.append(f"[red]✕ {len(failed)} başarısız ({fl})[/]")
        console.print(f"\n  " + "  ·  ".join(parts) + f"\n  [dim]{out_dir}[/]\n")
        logs.log.info("queue done: ok=%d failed=%d skipped=%d",
                      len(ok), len(failed), len(skipped))

        if not (failed and interactive):
            break
        if not menu.prompt_retry_failed(len(failed)):
            break
        retry = failed
        r_ok, failed, r_skip = _run_downloads(net, session, series, retry, out_dir)
        ok += r_ok


# --- Kalite seçimi --------------------------------------------------------
def choose_variant(variants, quality: str | None):
    if not variants:
        return None
    if not quality or quality == "best":
        return variants[0]
    if quality == "worst":
        return variants[-1]
    # sayısal yükseklik (720, 1080...)
    try:
        want = int(quality.rstrip("p"))
        for v in variants:
            if v.height == want:
                return v
    except ValueError:
        pass
    return variants[0]


def _extract_with_fallback(net, session, episode, series):
    """Kaynak ölü/park edilmişse (genelde Türkçe Dublaj) otomatik olarak
    aynı içeriğin orijinal/altyazılı sürümüne geçer."""
    try:
        return extractor.extract_stream(net, session, episode.url)
    except extractor.DeadSourceError:
        if not (series and catalog.is_dubbed(series)):
            raise
        alt = catalog.find_original_counterpart(net, session, series)
        if not alt:
            raise
        alt_eps = catalog.get_episodes(net, session, alt)
        m = catalog.match_episode(alt_eps, episode.season, episode.number)
        if not m:
            raise
        console.print(
            f"[yellow]Dublaj kaynağı ölü → orijinal/altyazılı sürüme geçildi:[/] "
            f"{alt.name} (S{m.season}E{m.number})"
        )
        return extractor.extract_stream(net, session, m.url)


def resolve_stream_url(net, session, episode, quality, interactive: bool, series=None):
    stream = _extract_with_fallback(net, session, episode, series)
    variants = m3u8_parser.list_variants(net, session, stream.m3u8_url, stream.referer)
    if not variants:
        return stream, stream.m3u8_url
    if interactive:
        v = menu.prompt_quality(variants)
    else:
        v = choose_variant(variants, quality)
    return stream, (v.url if v else stream.m3u8_url)


# --- İnteraktif akış ------------------------------------------------------
def run_interactive(net, session):
    while True:
        series = menu.prompt_search_series(net, session)
        if not series:
            return                                    # aramada iptal → çık
        with console.status(f"[bright_red]{series.name}[/] bölümleri yükleniyor…",
                            spinner="dots"):
            episodes = catalog.get_episodes(net, session, series)
        if not episodes:
            console.print("[red]✕ Bölüm bulunamadı.[/]")
            continue

        # Sıra: önce ne yapılacağı (izle/indir), sonra sezon, sonra bölüm(ler)
        mode = menu.prompt_mode()
        if mode is None:
            continue                                  # geri → yeni arama

        season = menu.prompt_season(episodes)
        if season is None:
            continue
        # 'Tüm dizi' seçildiyse bütün sezonlar; değilse tek sezon (ilk→son sıralı)
        season_eps = (episodes if season == menu.ALL_SEASONS
                      else catalog.episodes_in_season(episodes, season))

        if mode == "watch":
            ep = menu.prompt_single_episode(season_eps)
            if ep:
                console.print(f"[cyan]Kaynak çözülüyor:[/] {ep.label}")
                try:
                    # Dublaj + orijinal kaynakları tek akışta birleştir (ses/altyazı seçmeli).
                    merged = merge.build_merged(net, session, ep, series)
                    actions.watch(session, net, merged, f"{series.name} · {ep.label}")
                except extractor.ExtractError as e:
                    console.print(f"[red]Hata:[/] {e}")
        else:
            eps = menu.prompt_multi_episodes(season_eps)
            if eps:
                _download_queue(net, session, series, eps)

        if not menu.prompt_continue():
            return


# --- Non-interaktif akış --------------------------------------------------
def run_cli(net, session, args):
    results = catalog.search(net, session, args.search)
    if not results:
        console.print("[red]Sonuç yok.[/]")
        return
    if args.series is None:
        for i, r in enumerate(results):
            console.print(f"  [{i}] {r.name}  · {r.type}  ({r.slug})")
        return
    series = results[args.series]
    episodes = catalog.get_episodes(net, session, series)
    console.print(f"[bold]{series.name}[/] — {len(episodes)} bölüm, "
                  f"sezonlar {catalog.seasons_of(episodes)}")

    if args.season is not None:
        episodes = catalog.episodes_in_season(episodes, args.season)

    def find_ep(num):
        return next((e for e in episodes if e.number == num), None)

    if args.action == "download" and args.episodes:
        nums = [int(x) for x in args.episodes.split(",")]
        eps = [e for e in (find_ep(n) for n in nums) if e]
        if eps:
            _download_queue(net, session, series, eps, interactive=False)
        return

    ep = find_ep(args.episode) if args.episode else (episodes[0] if episodes else None)
    if not ep:
        console.print("[red]Bölüm bulunamadı.[/]")
        return
    try:
        stream, url = resolve_stream_url(net, session, ep, args.quality, False, series=series)
    except extractor.ExtractError as e:
        console.print(f"[red]Hata:[/] {e}")
        return

    if args.action == "extract":
        console.print(f"[green]master m3u8:[/] {stream.m3u8_url}")
        console.print(f"[green]seçili kalite:[/] {url}")
        console.print(f"[green]referer:[/] {stream.referer}")
        console.print(f"[green]TR altyazı:[/] {stream.subtitle_url}")
    elif args.action == "watch":
        merged = merge.build_merged(net, session, ep, series)
        actions.watch(session, net, merged, f"{series.name} · {ep.label}")
    elif args.action == "download":
        merged = merge.build_merged(net, session, ep, series)
        actions.download(session, net, merged,
                         f"{series.name} - S{ep.season:02d}E{ep.number:02d}",
                         out_dir=_series_out_dir(series))


def main():
    p = argparse.ArgumentParser(description="Dizipal headless CLI (tarayıcısız)")
    p.add_argument("--domain", help="Domaini elle belirt (resolver'ı atla)")
    p.add_argument("--search", help="Arama sorgusu (non-interaktif)")
    p.add_argument("--series", type=int, help="Arama sonucu indeksi")
    p.add_argument("--season", type=int, help="Sezon numarası")
    p.add_argument("--episode", type=int, help="Tek bölüm numarası")
    p.add_argument("--episodes", help="Çoklu bölüm: '1,2,3'")
    p.add_argument("--quality", default="best", help="best | worst | 1080 | 720 ...")
    p.add_argument("--action", choices=["extract", "watch", "download"],
                   default="extract")
    p.add_argument("--player", choices=["auto", "potplayer", "vlc", "mpv"],
                   help="İzleme oynatıcısı (varsayılan: auto)")
    p.add_argument("--no-cache", action="store_true")
    args = p.parse_args()

    if args.player:
        config.PLAYER = args.player

    logs.setup()
    logs.log.info("başlangıç · action=%s search=%s",
                  args.action if args.search else "interactive", args.search or "-")

    session = SessionState()
    net = Network(session)

    _banner()
    with console.status("[bright_red]Bağlantı hazırlanıyor…", spinner="dots"):
        resolver.resolve(net, session, override=args.domain,
                         use_cache=not args.no_cache)
    console.print("  [green]●[/] Bağlantı hazır\n")

    try:
        if args.search:
            run_cli(net, session, args)
        else:
            run_interactive(net, session)
    except KeyboardInterrupt:
        console.print("\n[dim]İptal edildi.[/]")
    finally:
        net.close()


if __name__ == "__main__":
    main()
