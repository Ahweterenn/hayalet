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
import concurrent.futures
import random
import sys
import time

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
from rich.table import Table
from rich.text import Text

from hayalet import config, sites  # noqa: F401 — sites: adapter'ları kaydettirir
from hayalet.core import (actions, catalog, extractor, logs, m3u8_parser, menu,
                         personas, prefs, resolver, utils)
from hayalet.core.network import Network
from hayalet.core.session import SessionState
from hayalet.core.sites import SITES

console = Console()


def _banner():
    """Başlangıç başlığı — modern, kırmızı vurgulu."""
    title = Text()
    title.append("  ▎", style="bright_red")
    title.append("hayalet", style="bold bright_white")
    title.append("  tarayıcısız dizi/film aracı", style="dim")
    console.print(Panel(title, border_style="bright_red", padding=(0, 1)))


def _results_table(results, title: str) -> Table:
    """Arama sonucu / öneri listesi için hizalı, başlıklı tablo (--series ile seçilecek indeks dahil)."""
    table = Table(title=title, title_style="bold", border_style="grey30",
                 header_style="bold bright_red", expand=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("İsim")
    table.add_column("Tür", style="cyan")
    table.add_column("Slug", style="dim")
    for i, r in enumerate(results):
        table.add_row(str(i), r.name, r.type, r.slug)
    return table


def _out_dir(series):
    """İndirme klasörü. Dizi → <Downloads>/<Dizi>/ (bölümler bir arada); film →
    doğrudan <Downloads> (tek dosya, ayrı klasör açmaya gerek yok)."""
    if catalog.is_movie(series):
        return config.DOWNLOAD_DIR
    return config.DOWNLOAD_DIR / utils.safe_filename(series.name)


def _file_title(series, ep):
    """Çıktı dosyasının adı (uzantısız). Dizi bölümü → 'Dizi - SxxExx';
    film → sadece filmin adı (tek dosya)."""
    if catalog.is_movie(series):
        return series.name
    return f"{series.name} - S{ep.season:02d}E{ep.number:02d}"


def _existing_file(out_dir, series, ep):
    """Bu bölüm/film daha önce inmiş mi? (.mkv/.mp4, >1MB) → dosya yolu | None."""
    safe = utils.safe_filename(_file_title(series, ep))
    for ext in (".mkv", ".mp4"):
        f = out_dir / f"{safe}{ext}"
        try:
            if f.exists() and f.stat().st_size > 1_000_000:
                return f
        except OSError:
            pass
    return None


def _run_downloads(net, session, adapter, series, eps, out_dir):
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
        started_any = False
        for idx, ep in enumerate(eps, 1):
            title = _file_title(series, ep)
            existing = _existing_file(out_dir, series, ep)
            if existing:                     # zaten inmiş → atla (kaldığın yerden devam)
                progress.console.print(f"[dim]⏭  Atlandı (zaten var): {existing.name}[/dim]")
                logs.log.info("skip existing: %s", existing.name)
                skipped.append(ep)
                progress.update(overall, advance=1,
                                description=f"Genel  ({idx}/{total})")
                continue

            if started_any:
                # Bölümler arasına rastgele kısa bir bekleme koyar; art arda onlarca
                # bölümün sıfır aralıkla çekilmesi, siteye "script" gibi görünen
                # düzenli/mekanik bir örüntü bırakır. Toplam indirme süresine göre
                # ihmal edilebilir ama örüntüyü kırar.
                time.sleep(random.uniform(1.5, 4.5))
            started_any = True

            # Ağ takılması bir bölümü düşürürse bir kez daha dene (URL'leri tazeleyerek)
            rc = 1
            for attempt in range(2):
                progress.reset(cur, total=100, speed="", description=(
                    f"Çözülüyor: {ep.label}" if attempt == 0
                    else f"Tekrar deneniyor: {ep.label}"))
                try:
                    merged = adapter.build_stream(net, session, ep, series)
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


def _download_queue(net, session, adapter, series, eps, interactive=True):
    """Kuyruğu indirir; sonunda özet gösterir ve (interaktifse) başarısızları tekrar sorar."""
    eps = sorted(eps, key=lambda e: (e.season, e.number))
    out_dir = _out_dir(series)
    ok, failed, skipped = _run_downloads(net, session, adapter, series, eps, out_dir)

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
        r_ok, failed, r_skip = _run_downloads(net, session, adapter, series, retry, out_dir)
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


def _save_resume(series, ep) -> None:
    if catalog.is_movie(series):
        return                                        # tek seferlik izleme, hatırlamaya gerek yok
    prefs.save(resume={"series_name": series.name, "series_slug": series.slug,
                       "series_type": series.type, "site": series.site,
                       "season": ep.season, "episode": ep.number})


# --- İnteraktif akış ------------------------------------------------------
def _movie_flow(net, session, adapter, series, ep) -> bool:
    """Film akışı: sezon/bölüm seçimi yok (tek dosya). İzle/İndir'i doğrudan
    filmin üzerinde uygular. Dönüş: True → kullanıcı çıkışı seçti."""
    while True:
        act = menu.prompt_movie_action(series.name)
        if act == "watch":
            console.print(f"[cyan]Kaynak çözülüyor:[/] {series.name}")
            try:
                merged = adapter.build_stream(net, session, ep, series)
                actions.watch(session, net, merged, series.name)
            except extractor.ExtractError as e:
                console.print(f"[red]Hata:[/] {e}")
        elif act == "download":
            _download_queue(net, session, adapter, series, [ep])
        elif act == "search":
            return False                               # yeni aramaya dön
        else:                                          # exit
            return True


def run_interactive(contexts: dict):
    """contexts: site adı -> (Network, SessionState). Birden fazla site aktifse
    arama hepsinde birden yapılır; seçilen sonucun `site`'ına göre o siteye ait
    net/session/adapter her turda yeniden çözülür (bkz. Series.site)."""
    resume = prefs.load().get("resume")
    while True:
        default_season = None
        if resume:
            series = menu.prompt_resume_series(resume)
            if series:
                default_season = resume.get("season")
            else:
                series = menu.prompt_search_series(contexts)
            resume = None                              # yalnızca ilk turda öner
        else:
            series = menu.prompt_search_series(contexts)
        if not series:
            return                                    # aramada iptal → çık
        if series.site not in contexts:
            # Kaydedilmiş "kaldığın yerden devam" başka bir --site sabitlemesinde
            # oluşmuş olabilir; o site şu an aktif değilse nazikçe uyarıp devam et.
            console.print(f"[red]✕ '{series.site}' sitesi şu an aktif değil.[/]")
            continue
        net, session = contexts[series.site]
        adapter = SITES[series.site]
        load_msg = (f"[bright_red]{series.name}[/] yükleniyor…" if catalog.is_movie(series)
                    else f"[bright_red]{series.name}[/] bölümleri yükleniyor…")
        with console.status(load_msg, spinner="dots"):
            episodes = adapter.get_episodes(net, session, series)
        if not episodes:
            console.print("[red]✕ İçerik bulunamadı.[/]")
            continue

        # Film: sezon/bölüm yok — sade İzle/İndir menüsü (bkz. _movie_flow).
        if catalog.is_movie(series):
            if _movie_flow(net, session, adapter, series, episodes[0]):
                return                                 # çıkış
            continue                                   # yeni arama

        # Aynı dizi içinde kal: kullanıcı "devam et" dedikçe yeniden aramaya
        # dönmeden mod/sezon/bölüm seçimine geri gelinir.
        stay_in_series = True
        while stay_in_series:
            # Sıra: önce ne yapılacağı (izle/indir), sonra sezon, sonra bölüm(ler)
            mode = menu.prompt_mode()
            if mode is None:
                break                                 # geri → dizi menüsünden çık

            season = menu.prompt_season(episodes, default_season=default_season)
            default_season = None
            if season is None:
                continue                              # geri → aynı dizide mod seçimine dön
            # 'Tüm dizi' seçildiyse bütün sezonlar; değilse tek sezon (ilk→son sıralı)
            season_eps = (episodes if season == menu.ALL_SEASONS
                          else catalog.episodes_in_season(episodes, season))

            if mode == "watch":
                ep = menu.prompt_single_episode(season_eps)
                if ep is None:
                    continue                          # "Geri" → mod seçimine dön
                # İzleme bitip (Enter'a basılıp) döndükten sonra bir sonraki bölüm sorulur;
                # "evet" ise aynı akışla o bölüme geçilir (dizinin tamamı üzerinden, sezon sınırı aşılır).
                while ep:
                    console.print(f"[cyan]Kaynak çözülüyor:[/] {ep.label}")
                    try:
                        # Adapter, sitesine göre tek kaynak ya da (Dizipal'de) dublaj +
                        # orijinal kaynakları tek akışta birleştirir (ses/altyazı seçmeli).
                        merged = adapter.build_stream(net, session, ep, series)
                        actions.watch(session, net, merged, f"{series.name} · {ep.label}")
                        _save_resume(series, ep)
                    except extractor.ExtractError as e:
                        console.print(f"[red]Hata:[/] {e}")
                        break
                    nxt = catalog.next_episode(episodes, ep)
                    if not nxt or not menu.prompt_next_episode(nxt):
                        break
                    ep = nxt
            else:
                eps = menu.prompt_multi_episodes(season_eps)
                if eps:
                    _download_queue(net, session, adapter, series, eps)

            action = menu.prompt_continue(series.name)
            if action == "exit":
                return
            stay_in_series = (action == "same")


# --- Non-interaktif akış --------------------------------------------------
def run_cli(net, session, args, adapter):
    results = adapter.search(net, session, args.search)
    if not results:
        suggestions = adapter.suggest(net, session, args.search)
        if suggestions:
            console.print(_results_table(suggestions, "Sonuç yok. Şunu mu demek istediniz?"))
            console.print("[dim]Önerilen adla --search'ü tekrar çalıştırın.[/]")
        else:
            console.print("[red]Sonuç yok.[/]")
        return
    if args.series is None:
        console.print(_results_table(results, "Arama sonuçları"))
        return
    series = results[args.series]
    episodes = adapter.get_episodes(net, session, series)
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
            _download_queue(net, session, adapter, series, eps, interactive=False)
        return

    ep = find_ep(args.episode) if args.episode else (episodes[0] if episodes else None)
    if not ep:
        console.print("[red]Bölüm bulunamadı.[/]")
        return
    try:
        merged = adapter.build_stream(net, session, ep, series)
    except extractor.ExtractError as e:
        console.print(f"[red]Hata:[/] {e}")
        return

    if args.action == "extract":
        variants = m3u8_parser.list_variants(net, session, merged.video_master_url,
                                             merged.video_referer)
        v = choose_variant(variants, args.quality or prefs.load().get("quality", "best")) \
            if variants else None
        if v:
            prefs.save(quality=str(v.height) if v.height else "best")
        url = v.url if v else merged.video_master_url
        console.print(f"[green]master m3u8:[/] {merged.video_master_url}")
        console.print(f"[green]seçili kalite:[/] {url}")
        console.print(f"[green]referer:[/] {merged.video_referer}")
        console.print(f"[green]TR altyazı:[/] {merged.subtitle_url}")
    elif args.action == "watch":
        actions.watch(session, net, merged, f"{series.name} · {ep.label}")
        _save_resume(series, ep)
    elif args.action == "download":
        actions.download(session, net, merged, _file_title(series, ep),
                         out_dir=_out_dir(series))


def _connect(adapter, override_domain, use_cache, tor_proxy):
    """Bir adapter için kimlik+Network/SessionState kurar ve domain'i çözer.
    Başarısızsa resolver.ResolverError yükseltir (çağıran yakalar)."""
    persona = personas.random_persona()
    session = SessionState(base_url=adapter.known_domain, referer=adapter.known_domain,
                           user_agent=persona["user_agent"],
                           impersonate=persona["impersonate"])
    if tor_proxy:
        session.proxy = tor_proxy
    net = Network(session)
    adapter.resolve_domain(net, session, override=override_domain, use_cache=use_cache)
    return net, session, persona


def _connect_single(adapter, args, tor_proxy):
    """Tek site için bağlantı kurar, durum/başarı mesajını basar. Başarısızsa
    hata mesajını basıp None döner (çağıran bununla çıkışı anlar)."""
    with console.status("[bright_red]Bağlantı hazırlanıyor…", spinner="dots"):
        try:
            net, session, persona = _connect(adapter, args.domain, not args.no_cache, tor_proxy)
        except resolver.ResolverError as e:
            console.print(f"  [red]✕[/] {e}")
            return None
    tor_suffix = " · Tor" if session.proxy else ""
    console.print(f"  [green]●[/] Bağlantı hazır  "
                  f"[dim]({adapter.name} · {persona['label']}{tor_suffix})[/]\n")
    return net, session


def main():
    p = argparse.ArgumentParser(description="hayalet — çoklu site headless CLI (tarayıcısız)")
    p.add_argument("--site", choices=sorted(SITES.keys()), default=None,
                   help="Hangi site kullanılacak. Belirtilmezse: interaktif modda TÜM "
                        "siteler aynı anda aranır; --search ile otomasyon modunda "
                        "dizipal varsayılan.")
    p.add_argument("--domain", help="Domaini elle belirt (resolver'ı atla) — "
                                    "yalnızca --site ile birlikte anlamlı")
    p.add_argument("--search", help="Arama sorgusu (non-interaktif)")
    p.add_argument("--series", type=int, help="Arama sonucu indeksi")
    p.add_argument("--season", type=int, help="Sezon numarası")
    p.add_argument("--episode", type=int, help="Tek bölüm numarası")
    p.add_argument("--episodes", help="Çoklu bölüm: '1,2,3'")
    p.add_argument("--quality", default=None,
                   help="best | worst | 1080 | 720 ... (verilmezse son kullanılan hatırlanır)")
    p.add_argument("--action", choices=["extract", "watch", "download"],
                   default="extract")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--tor", action="store_true",
                   help="Trafiği yerel Tor SOCKS proxy'sinden (127.0.0.1:9050) geçir — "
                        "gerçek IP'yi gizler ama site Tor çıkışlarını sık engelleyebilir; "
                        "Tor çalışmıyorsa otomatik olarak normal bağlantıya döner")
    args = p.parse_args()

    logs.setup()
    logs.log.info("başlangıç · site=%s action=%s search=%s",
                  args.site or "auto", args.action if args.search else "interactive",
                  args.search or "-")

    # Her çalıştırmada rastgele bir cihaz/tarayıcı kimliği: bu aracı çalıştıran
    # herkesin aynı sabit UA+TLS imzasını göndermesini önler (bkz. personas.py).
    # Her site kendi kimliğini/oturumunu alır (--tor tümüne uygulanır).
    tor_proxy = None
    if args.tor:
        import socket
        try:
            with socket.create_connection(("127.0.0.1", 9050), timeout=2):
                pass
            tor_proxy = "socks5h://127.0.0.1:9050"
        except OSError:
            console.print("[yellow]⚠ Tor SOCKS portu (127.0.0.1:9050) yanıt vermiyor — "
                          "Tor Browser'ı veya tor servisini başlatıp tekrar deneyin. "
                          "Şimdilik normal bağlantıyla devam ediliyor.[/]")

    _banner()
    nets: list[Network] = []
    try:
        if args.search:
            # Otomasyon: her zaman tek site (belirtilmezse dizipal).
            adapter = SITES[args.site or "dizipal"]
            result = _connect_single(adapter, args, tor_proxy)
            if result is None:
                return
            net, session = result
            nets.append(net)
            run_cli(net, session, args, adapter)

        elif args.site:
            # İnteraktif, kullanıcı tek siteye sabitlemiş.
            adapter = SITES[args.site]
            result = _connect_single(adapter, args, tor_proxy)
            if result is None:
                return
            net, session = result
            nets.append(net)
            run_interactive({adapter.name: (net, session)})

        else:
            # İnteraktif, varsayılan: tüm siteler paralel bağlanır, arama hepsinde
            # birden yapılır (bkz. menu.prompt_search_series / Series.site).
            if args.domain:
                console.print("[yellow]⚠ --domain yalnızca --site ile birlikte anlamlı, "
                              "yoksayıldı.[/]")
            contexts: dict[str, tuple[Network, SessionState]] = {}
            labels = []
            with console.status("[bright_red]Bağlantı hazırlanıyor (tüm siteler)…",
                                spinner="dots"):
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(SITES)) as ex:
                    futs = {ex.submit(_connect, ad, None, not args.no_cache, tor_proxy): name
                           for name, ad in SITES.items()}
                    for fut in concurrent.futures.as_completed(futs):
                        name = futs[fut]
                        try:
                            net, session, persona = fut.result()
                        except resolver.ResolverError as e:
                            console.print(f"  [yellow]⚠ {name}: {e}[/]")
                            continue
                        contexts[name] = (net, session)
                        nets.append(net)
                        labels.append(f"{name} · {persona['label']}")
            if not contexts:
                console.print("  [red]✕ Hiçbir siteye bağlanılamadı.[/]")
                return
            tor_suffix = " · Tor" if tor_proxy else ""
            console.print(f"  [green]●[/] Bağlantı hazır  "
                          f"[dim]({', '.join(labels)}{tor_suffix})[/]\n")
            run_interactive(contexts)

    except KeyboardInterrupt:
        console.print("\n[dim]İptal edildi.[/]")
    finally:
        for net in nets:
            net.close()


if __name__ == "__main__":
    main()
