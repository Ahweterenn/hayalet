"""Aksiyon katmanı: tarayıcıda (hls.js) izleme, ffmpeg ile indirme.

İkisi de yerel HLS proxy üzerinden SessionState'teki cookie + user-agent +
referer ile CDN'e bağlanır (anti-bot için kritik). Ayrı Türkçe .vtt varsa
softsub olarak muxlanır (indirme) ya da HLS altyazı kanalına enjekte edilir
(izleme, bkz. proxy.py).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hayalet import config
from hayalet.core import logs, utils
from hayalet.core.extractor import StreamInfo
from hayalet.core.network import Network
from hayalet.core.session import SessionState


_LANG3 = {"tr": "tur", "en": "eng", "de": "ger", "fr": "fre", "es": "spa",
          "it": "ita", "ru": "rus", "ar": "ara", "ja": "jpn", "ko": "kor",
          "zh": "chi", "pt": "por", "nl": "dut", "pl": "pol"}


def _lang3(code: str) -> str:
    """ISO 639-1 (2 harf) → 639-2/B (3 harf); zaten 3 harfliyse aynen döner."""
    c = (code or "").strip().lower().split("-")[0]
    if len(c) == 3:
        return c
    return _LANG3.get(c, "")


def _require(tool: str) -> str | None:
    found = shutil.which(tool)
    if found:
        return found
    # PATH'te yoksa bilinen kurulum konumlarına bak (kurulum PATH'e eklemeyebilir)
    extra = {
        "ffmpeg": [r"C:\ffmpeg\bin\ffmpeg.exe"],
    }
    for cand in extra.get(tool, []):
        if Path(cand).exists():
            return cand
    return None


def _say(progress, msg: str) -> None:
    """Live progress varken normal print imleci bozar; progress.console'a yönlendir."""
    if progress is not None:
        progress.console.print(msg)
    else:
        print(msg)


def _ffprobe_path(ffmpeg_path: str) -> str | None:
    """ffmpeg'in yanındaki ffprobe'u dener, yoksa PATH'e düşer."""
    probe = Path(ffmpeg_path).with_name("ffprobe" + Path(ffmpeg_path).suffix)
    if probe.exists():
        return str(probe)
    return shutil.which("ffprobe")


def _ffprobe_duration(ffmpeg_path: str, url: str) -> float:
    """HLS toplam süresini ffprobe ile (segment indirmeden, EXTINF toplamı) alır."""
    import json as _json
    probe = _ffprobe_path(ffmpeg_path)
    if not probe:
        return 0.0
    try:
        out = subprocess.run(
            [probe, "-v", "quiet", "-print_format", "json", "-show_format",
             "-allowed_extensions", "ALL", url],
            capture_output=True, text=True, timeout=60).stdout
        return float((_json.loads(out).get("format") or {}).get("duration") or 0)
    except Exception:
        return 0.0


def is_complete_download(path: Path) -> bool:
    """Bir indirilmiş dosyanın GERÇEKTEN tam olup olmadığını doğrular.

    Sadece boyuta (ya da ffprobe -show_format'ın raporladığı süreye) bakmak
    yetmez: MKV, toplam süreyi dosyanın BAŞINDAKİ SegmentInfo'da tutar — ağ
    kopması/Ctrl+C ile ORTASINDA kesilen bir MKV bile "declared" süreyi doğru
    raporlayabilir (ampirik olarak doğrulandı: 15sn'lik bir dosyanın ilk %60'ı
    bile duration=15.0 döner). Bu yüzden dosyanın gerçekten iddia ettiği SONUNA
    kadar uzandığı, sona yakın bir kare çekmeyi deneyerek doğrulanır — kesilen
    dosyalarda (MP4'te moov atom eksikliği, MKV'de "File ended prematurely")
    ffmpeg ya sıfırdan farklı çıkış kodu verir ya da stderr'e yazar; tam
    dosyalarda ikisi de temizdir.
    """
    try:
        if not path.exists() or path.stat().st_size < 1_000_000:
            return False
    except OSError:
        return False
    ff = _require("ffmpeg")
    if not ff:
        return True                     # ffmpeg yoksa eski (yalnız boyut) davranışına düş
    try:
        r = subprocess.run(
            [ff, "-v", "error", "-sseof", "-2", "-i", str(path),
             "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30)
        return r.returncode == 0 and not r.stderr.strip()
    except Exception:
        return False


def _fmt_speed(bps: float) -> str:
    """bayt/sn → okunur hız (ör. '12.3 MB/s'); 0/negatifse boş."""
    if bps <= 0:
        return ""
    val = float(bps)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024:
            return f"{val:.1f} {unit}/s"
        val /= 1024
    return f"{val:.1f} TB/s"


def _run_ffmpeg_progress(cmd: list[str], duration: float, on_update) -> tuple[int, str]:
    """ffmpeg'i -progress ile çalıştırır; yüzde + anlık hız hesaplayıp on_update(pct, bps).

    Hız, total_size (yazılan bayt) deltasından ~0.5sn pencerelerle bulunur.
    Dönüş: (returncode, hata_kuyruğu). stderr geçici dosyaya yazılır (deadlock olmasın).
    """
    import tempfile
    import time
    from collections import deque
    errf = tempfile.TemporaryFile()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                         text=True, bufsize=1)
    # Proxy segmentleri toplu gönderdiğinden total_size patlama-durma şeklinde artar.
    # ~2 sn'lik kayan pencere ortalaması + ilerleme yokken son hızı koruma → hız
    # göstergesi titremez/kaybolmaz.
    samples: deque = deque()
    speed = 0.0
    try:
        for line in p.stdout:
            line = line.strip()
            if line.startswith("total_size="):
                try:
                    sz = int(line.split("=", 1)[1])
                except ValueError:
                    continue                       # "N/A"
                now = time.monotonic()
                samples.append((now, sz))
                while len(samples) > 1 and now - samples[0][0] > 2.0:
                    samples.popleft()
                if len(samples) >= 2:
                    dt = samples[-1][0] - samples[0][0]
                    db = samples[-1][1] - samples[0][1]
                    if dt > 0 and db > 0:          # yalnız ilerleme varsa güncelle
                        speed = db / dt            # (duraklamada son hız korunur)
            elif line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue                       # erken "N/A"
                pct = min(99.9, us / 1_000_000 / duration * 100) if duration > 0 else 0.0
                on_update(pct, speed)
            elif line == "progress=end":
                on_update(100.0, speed)
    finally:
        p.wait()
    err = ""
    if p.returncode != 0:
        try:
            errf.seek(0)
            err = errf.read().decode("utf-8", "replace")[-600:]
        except Exception:
            pass
    errf.close()
    return p.returncode, err


# --- İZLE (yerel HLS proxy + tarayıcı/hls.js) ----------------------------
# Tarayıcı localhost'taki impersonating proxy'ye bağlanır; proxy master'ı (video +
# ses + enjekte edilen Türkçe altyazı) curl-cffi ile çekip verir. Oynatma
# proxy'nin sunduğu hls.js sayfasında (/player.html) yapılır.
def _build_watch_master(proxy, net: Network, session: SessionState, merged):
    """Birleştirilmiş akıştan, proxy'de barındırılan sentetik HLS master URL'si
    kurar. Dönüş: (yerel_master_url, ses_sayısı, altyazı_var_mı).

    Aynı proxy içinde birden çok kez çağrılabilir (bölümden bölüme geçişte yeni
    master kurmak için) — bu yüzden watch()'tan ayrı bir yardımcıdır.
    """
    from hayalet.core.m3u8_parser import list_variants
    from hayalet.core.proxy import build_master_playlist, build_subs_playlist

    # Video kalite varyantları (kalite seçimi için); yoksa tek media playlist
    try:
        variants = list_variants(net, session, merged.video_master_url,
                                 merged.video_referer)
    except Exception:
        variants = []
    if variants:
        vv = [(proxy.proxied(v.url, "m3u8", referer=merged.video_referer),
               v.bandwidth, v.height) for v in variants]
    else:
        vv = [(proxy.proxied(merged.video_master_url, "m3u8",
                             referer=merged.video_referer), 3000000, 0)]

    audios = [(proxy.proxied(a.url, "m3u8", referer=a.referer),
               a.name, a.lang, a.is_default) for a in merged.audios]

    subtitle = None
    if merged.subtitle_url:
        vtt = proxy.proxied(merged.subtitle_url, "vtt",
                            referer=merged.subtitle_referer)
        subs_local = proxy.virtual(build_subs_playlist(vtt), "m3u8")
        subtitle = (subs_local, "Türkçe", "tr")

    # Harici ses ve altyazı yoksa sentetik master üretmek bazı Media3
    # sürümlerinde codec/uyarlama özniteliklerini kaybettirip siyah ekran
    # oluşturabiliyor. Orijinal master'ı proxy'nin güvenli URL rewrite'ı ile
    # kullanmak bu kaynaklarda daha uyumludur.
    if not audios and subtitle is None:
        return proxy.proxied(merged.video_master_url, "m3u8",
                             referer=merged.video_referer), 0, False

    master_text = build_master_playlist(vv, audios, subtitle)
    return proxy.virtual(master_text, "m3u8"), len(merged.audios), bool(subtitle)


def watch(session: SessionState, net: Network, merged, title: str,
          advance=None, resume_at: float = 0.0, on_progress=None) -> int:
    """Birleştirilmiş akışı tarayıcıda (hls.js) oynatır.

    Dublaj + orijinal sesleri ve Türkçe altyazıyı içeren sentetik bir HLS master
    kurulur; hls.js sayfası ses/altyazı/kalite seçicilerini otomatik doldurur.

    advance verilirse (dizi izlerken): tarayıcı bölüm bitince ya da "Sonraki
    bölüm" düğmesine basınca proxy'nin /next ucuna istek atar; advance() çağrılıp
    sıradaki bölümün kaynağı AYNI proxy'de kurulur ve tarayıcıya döndürülür —
    kullanıcı terminale hiç dönmeden bir sonraki bölüme geçer. advance() dönüşü:
    {"merged": MergedStream, "title": str, "has_next": bool, "gen": int} ya da
    None (bitti). "gen", tarayıcının /progress bildirimlerine iliştirdiği nesil
    sayacıdır (bkz. on_progress) — advance() her başarılı geçişte bunu bir
    ARTIRMALIDIR (0'dan başlayarak), aksi halde bölüm-geçişi sırasında gelen bayat
    bir /progress bildirimi yanlış bölümün kaydını kirletebilir (canlı test edilip
    doğrulanmış bir hataydı: build_stream'in ağ gecikmesi sırasında tarayıcının
    periyodik ping'i eski bölümün neredeyse-bitmiş zaman damgasını yeni bölümün
    kaydına yazabiliyordu).

    resume_at: kaldığın yerden devam saniyesi (yalnızca ilk yüklemede uygulanır).
    on_progress: verilirse proxy'nin /progress ucuna gelen (currentTime, duration,
    nesil) bildirimlerinde birkaç saniyede bir çağrılır — çağıran bunu kalıcılığa
    (prefs) bağlar VE nesil eşleşmiyorsa görmezden gelmelidir (bkz. cli._watch_flow).
    """
    import webbrowser

    from hayalet.core.proxy import HLSProxy, _b64

    proxy = HLSProxy(session, merged.video_referer).start()
    local_master, n_aud, has_sub = _build_watch_master(proxy, net, session, merged)
    if on_progress is not None:
        proxy.progress_cb = on_progress

    # Sonraki bölüm köprüsü: /next'e gelen isteği advance()'e bağlar (bkz. proxy).
    has_next = advance is not None
    if advance is not None:
        def _next_handler():
            nxt = advance()                 # ağ işi burada (tarayıcı isteğinin thread'inde)
            if not nxt:
                return None
            src2, _, _ = _build_watch_master(proxy, net, session, nxt["merged"])
            return {"src": src2, "title": nxt["title"],
                    "hasNext": bool(nxt["has_next"])}
        proxy.next_handler = _next_handler

    player_url = (f"{proxy.base}/player.html"
                  f"?u={_b64(local_master)}&t={_b64(title)}")
    if has_next:
        player_url += "&n=1"
    if resume_at > 0:
        player_url += f"&s={resume_at:.1f}"

    print(f"[▶] Tarayıcıda açılıyor: {title}  "
          f"({n_aud} ses, altyazı {'var' if has_sub else 'yok'})")
    if has_next:
        print("    Bölüm bitince sonraki bölüm tarayıcıda otomatik açılır "
              "(veya ⏭ düğmesi).")
    opened = webbrowser.open(player_url)
    if not opened:
        print(f"    Tarayıcı açılamadı; elle aç:\n    {player_url}")
    try:
        input("[i] İzlemeyi bitirince buraya dönüp Enter'a bas (yayını kapatır)... ")
    except Exception:
        import time
        time.sleep(1800)
    finally:
        proxy.stop()
    return 0


# --- İNDİR (proxy + ffmpeg) ----------------------------------------------
def download(session: SessionState, net: Network, merged, title: str,
             out_dir: Path | None = None, progress=None, task_id=None) -> int:
    """Birleştirilmiş akışı (video + tüm sesler + Türkçe altyazı) tek ffmpeg
    geçişinde mux'layarak indirir. Her ses kanalı ayrı giriş; dil/başlık etiketli.

    progress/task_id verilirse (rich.Progress) o bölümün yüzde barı güncellenir.
    """
    ff = _require("ffmpeg")
    out_dir = out_dir or config.DOWNLOAD_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = utils.safe_filename(title)

    if not ff:
        _say(progress, "[!] ffmpeg bulunamadı (indirme için gerekli).")
        _say(progress, f"    Elle indirmek için master m3u8:\n    {merged.video_master_url}")
        return 1

    from hayalet.core.proxy import HLSProxy
    proxy = HLSProxy(session, merged.video_referer).start()

    # Türkçe altyazıyı yerel dosyaya indir (aynı ffmpeg geçişinde gömülür)
    sub_path: Path | None = None
    if merged.subtitle_url:
        try:
            sub_path = config.CACHE_DIR / f"{safe}.tr.vtt"
            sub_path.parent.mkdir(parents=True, exist_ok=True)
            sub_path.write_bytes(net.get(
                merged.subtitle_url, referer=merged.subtitle_referer).content)
        except Exception:
            sub_path = None

    # Video girişi: master m3u8 birden çok kalite içeriyorsa ffmpeg VARSAYILAN
    # olarak İLK varyantı seçer — bu, bazı sitelerde (ör. hdfilmcehennemi) en
    # DÜŞÜK kalitedir. Her zaman EN YÜKSEK kaliteyi indirmek için en iyi varyantı
    # elle çözüp veriyoruz (list_variants çözünürlüğe göre azalan sıralı).
    video_url = merged.video_master_url
    try:
        from hayalet.core.m3u8_parser import list_variants
        _vars = list_variants(net, session, merged.video_master_url,
                              merged.video_referer)
        if _vars:
            video_url = _vars[0].url
    except Exception:
        pass

    out_file = out_dir / f"{safe}.mp4"
    try:
        # input 0 = video (en yüksek kalite varyantı), sonra her ses kaynağı ayrı giriş
        cmd = [ff, "-y", "-allowed_extensions", "ALL", "-i",
               proxy.proxied(video_url, "m3u8", referer=merged.video_referer)]
        for a in merged.audios:
            cmd += ["-allowed_extensions", "ALL", "-i",
                    proxy.proxied(a.url, "m3u8", referer=a.referer)]

        si = None                            # altyazı girdi indeksi
        if sub_path:
            si = 1 + len(merged.audios)
            cmd += ["-i", str(sub_path)]

        # --- map: video + tüm sesler (+ altyazı) ---
        cmd += ["-map", "0:v:0"]
        for i in range(len(merged.audios)):
            cmd += ["-map", f"{i + 1}:a:0"]
        # AAC/MP3 ayrımı: 'aac_adtstoasc' sadece AAC'de geçerli; kopya modunda
        # ffmpeg gerekli bitstream filtresini otomatik ekler → elle vermiyoruz.
        cmd += ["-c:v", "copy", "-c:a", "copy"]

        # --- ses kanalı etiketleri (dil + başlık) ---
        for oi, a in enumerate(merged.audios):
            lang = _lang3(a.lang) or ("tur" if a.is_turkish else "und")
            cmd += [f"-metadata:s:a:{oi}", f"language={lang}",
                    f"-metadata:s:a:{oi}", f"title={a.name}"]
        if len(merged.audios) > 1:
            cmd += ["-disposition:a:0", "default"]   # ilk ses = varsayılan
            out_file = out_dir / f"{safe}.mkv"       # çoklu ses için mkv güvenli

        if si is not None:                  # altyazıyı softsub olarak göm → mkv
            cmd += ["-map", f"{si}:0", "-c:s", "srt",
                    "-metadata:s:s:0", "language=tur", "-metadata:s:s:0", "title=Türkçe"]
            out_file = out_dir / f"{safe}.mkv"

        # ffmpeg -y ile DOĞRUDAN hedef ada yazmak yerine geçici bir .part dosyasına
        # yazıp yalnızca başarıyla bitince yeniden adlandırıyoruz: ağ kopması/Ctrl+C
        # ile kesilen bir indirme, hedef adda yarım/bozuk bir dosya bırakıp bir
        # sonraki çalıştırmada "zaten indirilmiş" sanılıp yanlışlıkla atlanmasın diye
        # (bkz. cli._existing_file / actions.is_complete_download).
        tmp_file = out_file.with_name(out_file.name + ".part")
        cmd += [str(tmp_file)]
        info = f"({len(merged.audios)} ses, altyazı {'var' if sub_path else 'yok'})"
        err = ""
        if progress is not None and task_id is not None:
            # Yüzde barı için toplam süreyi ffprobe ile al, sonra -progress ile izle.
            dur = _ffprobe_duration(ff, proxy.proxied(
                video_url, "m3u8", referer=merged.video_referer))
            pcmd = ([ff, "-y", "-hide_banner", "-loglevel", "error",
                     "-progress", "pipe:1", "-nostats"] + cmd[2:])
            progress.update(task_id, description=f"⬇ {out_file.name} {info}")
            # Hız için ffmpeg'in total_size'ı değil, proxy'nin origin'den fiilen
            # okuduğu bayt/sn'yi kullanıyoruz: ffmpeg -c copy ile segmentleri tek
            # seferde (tamponlanmış) aldığından total_size patlama-durma şeklinde
            # ilerliyor ve gerçek ağ hızını yanlış yansıtıyordu.
            rc, err = _run_ffmpeg_progress(
                pcmd, dur,
                lambda pct, bps: progress.update(
                    task_id, completed=pct, speed=_fmt_speed(proxy.recent_speed())))
        else:
            print(f"[⬇] İndiriliyor: {out_file.name}  {info}")
            rc = subprocess.run(cmd).returncode
    finally:
        proxy.stop()
        if sub_path:
            try:
                sub_path.unlink(missing_ok=True)
            except Exception:
                pass

    if rc == 0:
        try:
            tmp_file.replace(out_file)          # atomik: yalnızca şimdi "tam" görünür
        except OSError as e:
            _say(progress, f"[!] İndirilen dosya yeniden adlandırılamadı: {e}")
            logs.log.error("download rename FAIL: %s -> %s | %s",
                           tmp_file.name, out_file.name, e)
            return 1
        _say(progress, f"[✔] Tamamlandı: {out_file.name}")
        logs.log.info("download ok: %s (%d ses, altyazı=%s)",
                      out_file.name, len(merged.audios), bool(sub_path))
    else:
        tmp_file.unlink(missing_ok=True)        # yarım/bozuk .part → sonraki taramada görünmesin
        _say(progress, f"[!] İndirme başarısız (ffmpeg kodu {rc}).")
        if err:
            _say(progress, f"[dim]{err.strip().splitlines()[-1] if err.strip() else ''}[/dim]")
        logs.log.error("download FAIL rc=%s: %s | %s",
                       rc, out_file.name, (err or "").strip().replace("\n", " ")[-300:])
    return rc
