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
import shutil
import sys
import threading
import time
from pathlib import Path

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
                         personas, prefs, resolver, utils, verifier)
from hayalet.core.network import Network
from hayalet.core.session import SessionState
from hayalet.core.sites import SITES, search_site

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


def _out_dir(series, base: Path | None = None):
    """İndirme klasörü. Dizi → <base>/<Dizi>/ (bölümler bir arada); film →
    doğrudan <base> (tek dosya, ayrı klasör açmaya gerek yok). base verilmezse
    varsayılan Downloads klasörü (config.DOWNLOAD_DIR) kullanılır — interaktif
    akışta kullanıcı _download_queue'da farklı bir ana klasör seçebilir."""
    base = base or config.DOWNLOAD_DIR
    if catalog.is_movie(series):
        return base
    return base / utils.safe_filename(series.name)


def _file_title(series, ep):
    """Çıktı dosyasının adı (uzantısız). Dizi bölümü → 'Dizi - SxxExx';
    film → sadece filmin adı (tek dosya)."""
    if catalog.is_movie(series):
        return series.name
    return f"{series.name} - S{ep.season:02d}E{ep.number:02d}"


def _existing_file(out_dir, series, ep):
    """Bu bölüm/film daha önce TAM olarak inmiş mi? (.mkv/.mp4) → dosya yolu | None.

    Salt boyuta bakmak yetmez: kesilen bir indirme de büyük olabilir (bkz.
    actions.is_complete_download) — ffprobe ile gerçek tamlık doğrulanır, aksi
    halde bozuk bir dosya "zaten indirilmiş" sanılıp yanlışlıkla atlanabilir."""
    safe = utils.safe_filename(_file_title(series, ep))
    for ext in (".mkv", ".mp4"):
        f = out_dir / f"{safe}{ext}"
        if actions.is_complete_download(f):
            return f
    return None


class _ResizeGuard:
    """rich'in Live görüntüsü, önceki karenin genişliğine göre hesapladığı satır
    sayısını kullanarak eskisini siler; Ctrl+scroll ile yazı boyutu değiştirmek de
    (Windows Terminal'de) bir resize sayılır ve bu hesap o anda geçersiz kalır →
    ekranda çakışan/yarım satırlar ("çorba") birikir. Terminal boyutu değiştiğinde
    tam ekran temizleyip yeniden çizerek bu artıkları giderir."""

    def __init__(self, progress):
        self._progress = progress
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        last = shutil.get_terminal_size()
        while not self._stop.wait(0.3):
            cur = shutil.get_terminal_size()
            if cur != last:
                last = cur
                try:
                    self._progress.console.clear()
                    self._progress.refresh()
                except Exception:
                    pass

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()


def _run_downloads(net, session, adapter, series, eps, out_dir, contexts=None):
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
    ) as progress, _ResizeGuard(progress):
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
                    
                    v_ok, v_err = verifier.verify_episode(net, series.name, ep.season, ep.number, merged)
                    if not v_ok:
                        progress.stop()
                        
                        if contexts and len(contexts) > 1:
                            progress.console.print(f"[bold red]⚠ UYARI ({ep.label}):[/] {v_err}")
                            progress.console.print("[cyan]Otomatik olarak diğer sitelerde tam bölüm aranıyor...[/cyan]")
                            new_m, new_a, new_n, new_s = _try_fallback(contexts, series, ep, adapter.name)
                            if new_m:
                                progress.console.print(f"[green]✔ Tam bölüm bulundu:[/] {new_a.name}")
                                merged = new_m
                                v_ok = True
                                
                        if not v_ok:
                            if not contexts or len(contexts) <= 1:
                                progress.console.print(f"[bold red]⚠ UYARI ({ep.label}):[/] {v_err}")
                            from rich.prompt import Confirm
                            if not Confirm.ask("Yine de (eksik/kısa) indirmek istiyor musunuz?", default=False):
                                rc = 1
                                progress.start()
                                break
                        progress.start()
                        
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


def _download_queue(net, session, adapter, series, eps, interactive=True, contexts=None):
    """Kuyruğu indirir; sonunda özet gösterir ve (interaktifse) başarısızları tekrar sorar.

    interaktif modda indirmeden önce hedef ANA klasör sorulur (varsayılan: son
    kullanılan ana klasör, yoksa Downloads) — kabul edilen klasör bir sonraki
    sefer için prefs.json'a kaydedilir. Non-interaktif (--search otomasyonu)
    modda soru sorulmaz, eski sabit Downloads davranışı aynen korunur.
    """
    eps = sorted(eps, key=lambda e: (e.season, e.number))

    base = None
    if interactive:
        remembered = prefs.load().get("download_dir")
        default_base = remembered or str(config.DOWNLOAD_DIR)
        hint_name = None if catalog.is_movie(series) else series.name
        chosen = menu.prompt_download_dir(default_base, hint_name)
        if chosen is None:
            console.print("[dim]İndirme iptal edildi.[/]")
            return
        base = Path(chosen)
        prefs.save(download_dir=str(base))

    out_dir = _out_dir(series, base)
    ok, failed, skipped = _run_downloads(net, session, adapter, series, eps, out_dir, contexts=contexts)

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
        r_ok, failed, r_skip = _run_downloads(net, session, adapter, series, retry, out_dir, contexts=contexts)
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


def _save_resume(series, ep, position: float = 0.0, duration: float = 0.0) -> None:
    if catalog.is_movie(series):
        return                                        # tek seferlik izleme, hatırlamaya gerek yok
    prefs.save(resume={"series_name": series.name, "series_slug": series.slug,
                       "series_type": series.type, "site": series.site,
                       "season": ep.season, "episode": ep.number,
                       "position": position, "duration": duration})


def _resume_position(series, ep) -> float:
    """Bu bölüm için daha önce kaydedilmiş dakika-dakika konumu varsa döner
    (yoksa/eşleşmiyorsa/anlamsızsa 0.0). "Kaldığın yerden devam"ın saniye düzeyi:
    bölüm/sezon eşleşmesi prefs.resume ile karşılaştırılır."""
    r = prefs.load().get("resume")
    if not r or r.get("site") != series.site or r.get("series_slug") != series.slug:
        return 0.0
    if r.get("season") != ep.season or r.get("episode") != ep.number:
        return 0.0
    pos = float(r.get("position") or 0)
    dur = float(r.get("duration") or 0)
    if pos < 20:
        return 0.0                                    # çok az izlenmiş → baştan başlat
    if dur and (dur - pos) < 60:
        return 0.0                                    # bitmişe yakın → tekrar baştan başlat
    return pos


# --- İnteraktif akış ------------------------------------------------------
def _movie_flow(net, session, adapter, series, ep, contexts=None) -> bool:
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
            _download_queue(net, session, adapter, series, [ep], contexts=contexts)
        elif act == "search":
            return False                               # yeni aramaya dön
        else:                                          # exit
            return True


def _try_fallback(contexts: dict, series, ep, skip_site: str):
    """Diğer sitelerde aynı dizinin aynı bölümünü arayıp, tam süreli kaynağı bulmaya çalışır."""
    if not contexts or len(contexts) <= 1: return None, None, None, None

    def check_site(site_name, f_net, f_session):
        if site_name == skip_site: return None
        f_adapter = SITES[site_name]
        try:
            results = f_adapter.search(f_net, f_session, series.name)
            if not results: return None
            f_series = next((s for s in results if s.name.lower() == series.name.lower()), None)
            if not f_series: return None
            
            f_episodes = f_adapter.get_episodes(f_net, f_session, f_series)
            f_ep = catalog.match_episode(f_episodes, ep.season, ep.number)
            if not f_ep: return None
            
            f_merged = f_adapter.build_stream(f_net, f_session, f_ep, f_series)
            ok, err = verifier.verify_episode(f_net, series.name, ep.season, ep.number, f_merged)
            if ok:
                return f_merged, f_adapter, f_net, f_session
        except Exception:
            pass
        return None

    import concurrent.futures
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(contexts))
    try:
        futs = [ex.submit(check_site, name, ctx[0], ctx[1]) for name, ctx in contexts.items()]
        for fut in concurrent.futures.as_completed(futs):
            res = fut.result()
            if res:
                return res
        return None, None, None, None
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def _watch_flow(net, session, adapter, series, episodes, start_ep, contexts=None) -> None:
    """Dizi bölümünü tarayıcıda oynatır; sonraki bölümlere geçiş tarayıcı içinde
    otomatik olur (terminale dönmeden). `advance`, tarayıcı /next'e istek atınca
    (bölüm bitince/⏭ düğmesi) proxy tarafından çağrılır ve sıradaki bölümü çözer.
    Ölü kaynaklı bölümler sessizce atlanır; her bölümde 'kaldığın yer' kaydedilir.
    """
    # state["ep"]/state["gen"] proxy'nin ayrı HTTP thread'lerinden (advance() /next
    # isteğinde, on_progress /progress isteğinde) eşzamanlı erişilebilir — state_lock
    # bu ikisini birbirine göre atomikleştirir. Bu YETMEZ tek başına: build_stream()
    # (advance() içindeki ağ çağrısı) saniyeler sürebiliyor ve tarayıcının periyodik
    # /progress ping'i TAM o sırada, state["ep"] henüz YENİ bölüme geçmemişken/geçmişken
    # eski bölümün neredeyse-bitmiş zaman damgasıyla gelebiliyor (canlı test edilip
    # doğrulandı: "sonraki bölüm" ile ilerlenen bölümler kaldığın-yerden-devam'da hiç
    # izlenmemiş gibi görünüyordu). Bu yüzden her /progress bildirimi bir nesil (gen)
    # numarası taşır; state["gen"] yalnızca advance() bir bölümü GERÇEKTEN başarıyla
    # çözünce artırılır ve on_progress, numarası uyuşmayan (bölüm geçişi sırasında
    # gönderilmiş, artık bayat) ping'leri sessizce yok sayar.
    state = {"ep": start_ep, "gen": 0}
    state_lock = threading.Lock()

    def advance():
        # Not: bu, proxy'nin HTTP thread'inde çalışır. Terminal input() ile
        # beklediğinden net/session'ı eşzamanlı kullanan başka bir şey yoktur.
        # cur: deneme sırasında ölü bölümleri atlayarak ilerleyen YEREL işaretçi —
        # state["ep"] yalnızca GERÇEKTEN çalışan bir bölüm bulununca güncellenir,
        # aksi halde ölü bir bölümde sonsuz döngüye girilirdi (next_episode hep aynı
        # ölü bölümü döndürür).
        cur = state["ep"]
        while True:
            nxt = catalog.next_episode(episodes, cur)
            if not nxt:
                return None                        # dizinin sonu
            try:
                merged = adapter.build_stream(net, session, nxt, series)
            except extractor.ExtractError as e:
                logs.log.warning("izleme sonraki atlandı %s: %s", nxt.label, e)
                cur = nxt                          # ölü kaynak → bir sonrakini dene
                continue
            with state_lock:
                state["ep"] = nxt
                state["gen"] += 1
                gen = state["gen"]
                _save_resume(series, nxt)          # yeni bölüm → konum sıfırlanır
            has_next = catalog.next_episode(episodes, nxt) is not None
            return {"merged": merged, "title": f"{series.name} · {nxt.label}",
                    "has_next": has_next, "gen": gen}

    def on_progress(t: float, d: float, g: int) -> None:
        # Proxy'nin HTTP thread'inden birkaç saniyede bir çağrılır. g, tarayıcının
        # o anda yüklü olduğunu SANDIĞI bölümün nesil numarası — state["gen"]'le
        # uyuşmuyorsa bu ping bayattır (bölüm geçişi sırasında gönderilmiş), yok say.
        with state_lock:
            if g != state["gen"]:
                return
            _save_resume(series, state["ep"], position=t, duration=d)

    console.print(f"[cyan]Kaynak çözülüyor:[/] {start_ep.label}")
    try:
        merged = adapter.build_stream(net, session, start_ep, series)
    except extractor.ExtractError as e:
        console.print(f"[red]Hata:[/] {e}")
        return

    console.print("[dim]Bölüm süresi doğrulanıyor...[/dim]")
    v_ok, v_err = verifier.verify_episode(net, series.name, start_ep.season, start_ep.number, merged)
    if not v_ok:
        if contexts and len(contexts) > 1:
            console.print(f"[bold red]⚠ UYARI:[/] {v_err}")
            console.print("[cyan]Otomatik olarak diğer sitelerde tam bölüm aranıyor...[/cyan]")
            new_m, new_a, new_n, new_s = _try_fallback(contexts, series, start_ep, adapter.name)
            if new_m:
                console.print(f"[green]✔ Tam bölüm bulundu:[/] {new_a.name}")
                merged = new_m
                adapter = new_a
                net = new_n
                session = new_s
                v_ok = True

        if not v_ok:
            if not contexts or len(contexts) <= 1:
                console.print(f"[bold red]⚠ UYARI:[/] {v_err}")
            from rich.prompt import Confirm
            if not Confirm.ask("Yine de (eksik/kısa) bölümü izlemek istiyor musunuz?", default=False):
                return

    resume_at = _resume_position(series, start_ep)   # sıfırlanmadan ÖNCE oku
    if resume_at > 0:
        console.print(f"[dim]↻ Kaldığın yerden devam: "
                      f"{int(resume_at) // 60:02d}:{int(resume_at) % 60:02d}[/dim]")
    _save_resume(series, start_ep)
    has_next = catalog.next_episode(episodes, start_ep) is not None
    actions.watch(session, net, merged, f"{series.name} · {start_ep.label}",
                  advance=advance if has_next else None,
                  resume_at=resume_at, on_progress=on_progress)


def run_interactive(contexts: dict):
    """contexts: site adı -> (Network, SessionState). Birden fazla site aktifse
    arama hepsinde birden yapılır; seçilen sonucun `site`'ına göre o siteye ait
    net/session/adapter her turda yeniden çözülür (bkz. Series.site)."""
    resume = prefs.load().get("resume")
    while True:
        default_season = None
        default_episode = None
        if resume:
            series = menu.prompt_resume_series(resume)
            if series:
                default_season = resume.get("season")
                default_episode = resume.get("episode")
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
                ep = menu.prompt_single_episode(season_eps, default_episode=default_episode)
                default_episode = None
                if ep is None:
                    continue                          # "Geri" → mod seçimine dön
                # Sonraki bölüme geçiş artık tarayıcıda: bölüm bitince (veya ⏭
                # düğmesi) otomatik geçilir, kullanıcı terminale dönmez.
                _watch_flow(net, session, adapter, series, episodes, ep, contexts=contexts)
            else:
                eps = menu.prompt_multi_episodes(season_eps)
                if eps:
                    _download_queue(net, session, adapter, series, eps, contexts=contexts)

            action = menu.prompt_continue(series.name)
            if action == "exit":
                return
            stay_in_series = (action == "same")


# --- Non-interaktif akış --------------------------------------------------
def run_cli(net, session, args, adapter):
    # Otomasyon akışı da interaktif menüyle aynı toleranslı arama katmanını
    # kullanır; aksi halde CLI'de çalışan sorgu mobil/menüde farklı sonuç verir.
    results = search_site(adapter, net, session, args.search)
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
    if args.series < 0 or args.series >= len(results):
        console.print(f"[red]Geçersiz sonuç indeksi: {args.series}[/]")
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

    if args.action in ("watch", "download"):
        console.print("[dim]Bölüm süresi doğrulanıyor...[/dim]")
        v_ok, v_err = verifier.verify_episode(net, series.name, ep.season, ep.number, merged)
        if not v_ok:
            console.print(f"[bold red]⚠ UYARI:[/] {v_err}")
            from rich.prompt import Confirm
            if not Confirm.ask(f"Yine de {args.action} işlemini başlatmak istiyor musunuz?", default=False):
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
        resume_at = _resume_position(series, ep)
        if resume_at > 0:
            console.print(f"[dim]↻ Kaldığın yerden devam: "
                          f"{int(resume_at) // 60:02d}:{int(resume_at) % 60:02d}[/dim]")
        _save_resume(series, ep)
        actions.watch(session, net, merged, f"{series.name} · {ep.label}",
                      resume_at=resume_at,
                      on_progress=lambda t, d, g: _save_resume(series, ep, position=t, duration=d))
    elif args.action == "download":
        actions.download(session, net, merged, _file_title(series, ep),
                         out_dir=_out_dir(series))


def _doctor_site(adapter, args, tor_proxy) -> list[tuple[str, bool, str]]:
    """Bir site için sağlık kontrolü zinciri: domain -> arama -> bölüm listesi ->
    uçtan uca extract. "Site mi bozuldu, bizim kod mu bozuldu" sorusunu doğrudan
    yanıtlar — canlı/runtime bir kontrol, core/*_test gibi saf-mantık testlerinden
    farklı. Dönüş: sıralı (adım_adı, ok, detay) listesi."""
    steps: list[tuple[str, bool, str]] = []
    override = args.domain if args.site else None
    try:
        net, session, persona = _connect(adapter, override, not args.no_cache, tor_proxy,
                                         args.cf_cookie, args.user_agent)
    except resolver.ResolverError as e:
        steps.append(("Domain", False, str(e)))
        return steps
    steps.append(("Domain", True, f"{session.base_url}  ({persona['label']})"))

    try:
        try:
            # Prefix-benzeri eşleşme yapan site arama backend'leri için hemen her
            # kataloğun bir şeyle eşleşmesi beklenen, jenerik (adapter-özel olmayan)
            # yoklama terimleri. Amaç belirli bir içerik değil, "arama mekanizması
            # gerçekten sonuç üretiyor mu" (bkz. bu oturumda yaşanan: domain bayatladı,
            # istek 200 döndü ama sonuç hep boştu — tam da bunu yakalamak için).
            probe_terms = ["a", "e", "2024"]
            results: list = []
            matched_term = None
            for term in probe_terms:
                results = adapter.search(net, session, term)
                if results:
                    matched_term = term
                    break
            if not results:
                steps.append(("Arama", False,
                              f"Denenen terimler ({', '.join(probe_terms)}) hiç sonuç vermedi — "
                              "site kataloğu boş görünüyor ya da arama backend'i bozuk."))
                return steps
            steps.append(("Arama", True, f"'{matched_term}' → {len(results)} sonuç "
                                         f"(ör. {results[0].name})"))
        except Exception as e:
            steps.append(("Arama", False, f"{type(e).__name__}: {e}"))
            return steps

        # Tek bir başlığa bakmak YANILTIYOR: arama sonucunun ilki pekâlâ "yakında
        # gelecek" (henüz yayınlanmamış, dolayısıyla kaynağı olmayan) bir yapım
        # olabilir — canlı olarak yaşandı: hdfilmcehennemi'nin ilk sonucu vizyona
        # girmemiş bir filmdi ve "kaynak butonu bulunamadı" hatası, adapter sapasağlam
        # çalışırken kırmızı raporlandı. Bu yüzden birkaç aday sırayla denenir ve
        # yalnızca HEPSİ başarısız olursa arıza bildirilir.
        last_list_err: str | None = None
        last_extract_err: str | None = None
        listed: tuple | None = None          # (series, episodes) — ilk listelenebilen
        for series in results[:3]:
            try:
                episodes = adapter.get_episodes(net, session, series)
            except Exception as e:
                last_list_err = f"{series.name}: {type(e).__name__}: {e}"
                continue
            if not episodes:
                last_list_err = f"'{series.name}' için hiç bölüm/kaynak bulunamadı."
                continue
            if listed is None:
                listed = (series, episodes)
            try:
                merged = adapter.build_stream(net, session, episodes[0], series)
            except (extractor.ExtractError, Exception) as e:
                detail = str(e) if isinstance(e, extractor.ExtractError) \
                    else f"{type(e).__name__}: {e}"
                last_extract_err = f"{series.name}: {detail}"
                continue
            if not merged.video_master_url:
                last_extract_err = f"{series.name}: master URL boş döndü"
                continue
            steps.append(("Bölüm/kaynak listesi", True,
                          f"{series.name}: {len(episodes)} adet"))
            steps.append(("Uçtan uca extract", True,
                          f"{series.name} → {merged.video_master_url[:60]}…"))
            return steps

        if listed is None:
            steps.append(("Bölüm/kaynak listesi", False,
                          last_list_err or "hiçbir adayda bölüm/kaynak yok."))
            return steps
        steps.append(("Bölüm/kaynak listesi", True,
                      f"{listed[0].name}: {len(listed[1])} adet"))
        steps.append(("Uçtan uca extract", False,
                      f"{last_extract_err} (denenen {min(len(results), 3)} adayın "
                      f"hiçbirinde kaynak çözülemedi)"))
        return steps
    finally:
        net.close()


def _run_doctor(args, tor_proxy) -> None:
    """--doctor: ağ gerektiren canlı sağlık kontrolü. --site verilmezse tüm
    kayıtlı siteler sırayla kontrol edilir."""
    console.print(Panel("[bold]Sağlık kontrolü[/]", border_style="bright_red",
                        padding=(0, 1)))

    ff = actions._require("ffmpeg")
    console.print(f"  {'[green]✔[/]' if ff else '[red]✕[/]'} ffmpeg  "
                  f"{'[dim](' + ff + ')[/]' if ff else '[dim]— PATH’te veya bilinen konumlarda yok (indirme çalışmaz)[/]'}")

    try:
        import webbrowser
        webbrowser.get()
        console.print("  [green]✔[/] Sistem tarayıcısı çözülebiliyor")
        browser_ok = True
    except Exception as e:
        console.print(f"  [red]✕[/] Sistem tarayıcısı bulunamadı: {e}")
        browser_ok = False
    console.print()

    if not args.site and args.domain:
        console.print("[yellow]⚠ --domain yalnızca --site ile birlikte anlamlı, yoksayıldı.[/]\n")

    site_names = [args.site] if args.site else sorted(SITES.keys())
    any_fail = not ff or not browser_ok
    for name in site_names:
        adapter = SITES[name]
        console.print(f"[bold]{adapter.name}[/]")
        with console.status(f"[bright_red]{adapter.name}: kontrol ediliyor…", spinner="dots"):
            steps = _doctor_site(adapter, args, tor_proxy)
        for label, ok, detail in steps:
            mark = "[green]✔[/]" if ok else "[red]✕[/]"
            console.print(f"  {mark} {label}: {detail}")
            if not ok:
                any_fail = True
        console.print()

    if any_fail:
        console.print("[yellow]⚠ Bazı kontroller başarısız — yukarıdaki satırlarda detay var.[/]")
    else:
        console.print("[green]✔ Tüm kontroller geçti.[/]")


def _parse_cf_cookie(raw: str) -> dict[str, str]:
    """--cf-cookie değerini cookie sözlüğüne çevirir.

    Hem çıplak değer ("abc123"), hem "cf_clearance=abc123", hem de tarayıcının
    Copy-as-cURL çıktısındaki gibi ";" ile ayrılmış tam cookie başlığı kabul edilir.
    """
    out: dict[str, str] = {}
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return out
    if "=" not in raw:
        return {"cf_clearance": raw}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _connect(adapter, override_domain, use_cache, tor_proxy,
             cf_cookie: str | None = None, ua_override: str | None = None):
    """Bir adapter için kimlik+Network/SessionState kurar ve domain'i çözer.
    Başarısızsa resolver.ResolverError yükseltir (çağıran yakalar)."""
    persona = personas.random_persona()
    session = SessionState(base_url=adapter.known_domain, referer=adapter.known_domain,
                           user_agent=persona["user_agent"],
                           impersonate=persona["impersonate"])
    # cf_clearance cookie'si VERİLDİĞİ tarayıcının UA'sına + IP'sine bağlıdır:
    # rastgele persona UA'sıyla gönderilirse Cloudflare cookie'yi geçersiz sayar.
    # Bu yüzden --cf-cookie ile --user-agent birlikte kullanılmalı; UA verilmişse
    # persona rotasyonu bilerek devre dışı bırakılır.
    #
    # Cookie her çalıştırmada elle yapıştırılmasın diye siteye göre saklanır: bir kez
    # --cf-cookie verilir, sonraki çalıştırmalar onu (UA'sıyla birlikte) kendiliğinden
    # yükler. Süresi dolduğunda site yine challenge döndürür — o noktada kayıt silinir
    # ve kullanıcıdan yenisi istenir (bkz. aşağıdaki ChallengeError yakalaması).
    if cf_cookie:
        session.cookies.update(_parse_cf_cookie(cf_cookie))
        if ua_override:
            prefs.save_cf(adapter.known_domain, cf_cookie, ua_override)
    else:
        rec = prefs.load_cf(adapter.known_domain)
        if rec:
            session.cookies.update(_parse_cf_cookie(rec["cookie"]))
            ua_override = ua_override or rec.get("user_agent")
            age_h = (time.time() - rec.get("ts", 0)) / 3600
            persona = {**persona,
                       "label": f"kayıtlı cf cookie · {age_h:.1f} saat önce alındı"}

    if ua_override:
        session.user_agent = ua_override
        if not persona["label"].startswith("kayıtlı"):
            persona = {**persona, "label": "elle verilen UA"}
    if tor_proxy:
        session.proxy = tor_proxy
    net = Network(session)
    try:
        adapter.resolve_domain(net, session, override=override_domain, use_cache=use_cache)
    except Exception as e:
        # Saklanan cookie'nin ömrü dolduysa site yine challenge döndürür. Bayat kaydı
        # burada silmezsek sonraki her çalıştırma da aynı ölü cookie'yle denenir ve
        # kullanıcı sebebini göremez.
        if not cf_cookie and prefs.load_cf(adapter.known_domain) and \
                "challenge" in str(e).lower():
            prefs.forget_cf(adapter.known_domain)
            raise resolver.ResolverError(
                f"Kayıtlı cf_clearance cookie'sinin süresi dolmuş (silindi). "
                f"Tarayıcıda {adapter.known_domain} adresini açıp yeni cf_clearance "
                f"değerini --cf-cookie ile bir kez daha ver."
            ) from None
        raise
    return net, session, persona


def _connect_single(adapter, args, tor_proxy):
    """Tek site için bağlantı kurar, durum/başarı mesajını basar. Başarısızsa
    hata mesajını basıp None döner (çağıran bununla çıkışı anlar)."""
    with console.status("[bright_red]Bağlantı hazırlanıyor…", spinner="dots"):
        try:
            net, session, persona = _connect(adapter, args.domain, not args.no_cache,
                                             tor_proxy, args.cf_cookie, args.user_agent)
        except resolver.ResolverError as e:
            console.print(f"  [red]✕[/] {e}")
            return None
    tor_suffix = " [dim](Tor)[/]" if session.proxy else ""
    console.print(f"  [green]●[/] Bağlantı hazır{tor_suffix}\n")
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
    p.add_argument("--cf-cookie", default=None,
                   help="Cloudflare JS challenge'ını aşmak için tarayıcıdan alınan "
                        "cf_clearance cookie'si ('cf_clearance=...' ya da çıplak değer). "
                        "AYNI IP'den ve --user-agent ile tarayıcının UA'sı verilerek "
                        "kullanılmalı; cookie birkaç saat sonra geçersizleşir.")
    p.add_argument("--user-agent", default=None,
                   help="User-Agent'ı elle sabitle (persona rotasyonunu devre dışı "
                        "bırakır). --cf-cookie ile birlikte zorunlu.")
    p.add_argument("--doctor", action="store_true",
                   help="Sağlık kontrolü: domain/arama/bölüm-listesi/uçtan-uca-extract "
                        "zincirini canlı test edip 'site mi bozuldu, bizim kod mu bozuldu' "
                        "sorusunu yanıtlar. --site verilmezse tüm siteler kontrol edilir; "
                        "diğer arama/aksiyon bayrakları bu modda yoksayılır.")
    args = p.parse_args()

    if args.cf_cookie and not args.user_agent:
        p.error("--cf-cookie tek başına işe yaramaz: cf_clearance cookie'si onu "
                "veren tarayıcının User-Agent'ına bağlıdır. --user-agent ile "
                "tarayıcının UA'sını da ver.")

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

    if args.doctor:
        _run_doctor(args, tor_proxy)
        return

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
            with console.status("[bright_red]Bağlantı hazırlanıyor (tüm siteler)…",
                                spinner="dots"):
                ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(SITES))
                try:
                    futs = {ex.submit(_connect, ad, None, not args.no_cache, tor_proxy,
                                      args.cf_cookie, args.user_agent): name
                           for name, ad in SITES.items()}
                    for fut in concurrent.futures.as_completed(futs, timeout=5.0):
                        name = futs[fut]
                        try:
                            net, session, _persona = fut.result()
                        except resolver.ResolverError as e:
                            console.print(f"  [yellow]⚠ {name}: {e}[/]")
                            continue
                        except Exception as e:
                            console.print(f"  [yellow]⚠ {name}: Bağlantı hatası ({e})[/]")
                            continue
                        contexts[name] = (net, session)
                        nets.append(net)
                except concurrent.futures.TimeoutError:
                    console.print("  [yellow]⚠ Bazı siteler zaman aşımına uğradı (atlanıyor)[/]")
                finally:
                    ex.shutdown(wait=False, cancel_futures=True)
            if not contexts:
                console.print("  [red]✕ Hiçbir siteye bağlanılamadı.[/]")
                return
            tor_suffix = " [dim](Tor)[/]" if tor_proxy else ""
            console.print(f"  [green]●[/] Bağlantı hazır{tor_suffix}\n")
            run_interactive(contexts)

    except KeyboardInterrupt:
        console.print("\n[dim]İptal edildi.[/]")
    finally:
        for net in nets:
            net.close()


if __name__ == "__main__":
    main()
