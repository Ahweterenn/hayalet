"""Aksiyon katmanı: mpv ile izleme, yt-dlp ile indirme.

Her iki araca da SessionState'ten cookie + user-agent + referer aktarılır
(anti-bot için kritik). Ayrı Türkçe .vtt varsa mpv'ye --sub-file, indirmede
sidecar dosya olarak eklenir.
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
        "mpv": [r"C:\Program Files\MPV Player\mpv.exe",
                r"C:\Program Files\mpv\mpv.exe"],
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


def _ffprobe_duration(ffmpeg_path: str, url: str) -> float:
    """HLS toplam süresini ffprobe ile (segment indirmeden, EXTINF toplamı) alır."""
    import json as _json
    probe = Path(ffmpeg_path).with_name("ffprobe" + Path(ffmpeg_path).suffix)
    if not probe.exists():
        p = shutil.which("ffprobe")
        if not p:
            return 0.0
        probe = Path(p)
    try:
        out = subprocess.run(
            [str(probe), "-v", "quiet", "-print_format", "json", "-show_format",
             "-allowed_extensions", "ALL", url],
            capture_output=True, text=True, timeout=60).stdout
        return float((_json.loads(out).get("format") or {}).get("duration") or 0)
    except Exception:
        return 0.0


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
def watch(session: SessionState, net: Network, merged, title: str,
          player: str = "auto") -> int:
    """Birleştirilmiş akışı tarayıcıda (hls.js) oynatır.

    Dublaj + orijinal sesleri ve Türkçe altyazıyı içeren sentetik bir HLS master
    kurulur; hls.js sayfası ses/altyazı/kalite seçicilerini otomatik doldurur.
    """
    import webbrowser

    from hayalet.core.m3u8_parser import list_variants
    from hayalet.core.proxy import (HLSProxy, build_subs_playlist, _b64)

    proxy = HLSProxy(session, merged.video_referer).start()

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

    from hayalet.core.proxy import build_master_playlist
    master_text = build_master_playlist(vv, audios, subtitle)
    local_master = proxy.virtual(master_text, "m3u8")
    player_url = (f"{proxy.base}/player.html"
                  f"?u={_b64(local_master)}&t={_b64(title)}")

    print(f"[▶] Tarayıcıda açılıyor: {title}  "
          f"({len(merged.audios)} ses, altyazı {'var' if subtitle else 'yok'})")
    opened = webbrowser.open(player_url)
    if not opened:
        print(f"    Tarayıcı açılamadı; elle aç:\n    {player_url}")
    try:
        input("[i] İzleme bitince buraya dönüp Enter'a bas (yayını kapatır)... ")
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

    out_file = out_dir / f"{safe}.mp4"
    try:
        # input 0 = video (master), sonra her ses kaynağı ayrı giriş
        cmd = [ff, "-y", "-allowed_extensions", "ALL", "-i",
               proxy.proxied(merged.video_master_url, "m3u8",
                             referer=merged.video_referer)]
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

        cmd += [str(out_file)]
        info = f"({len(merged.audios)} ses, altyazı {'var' if sub_path else 'yok'})"
        err = ""
        if progress is not None and task_id is not None:
            # Yüzde barı için toplam süreyi ffprobe ile al, sonra -progress ile izle.
            dur = _ffprobe_duration(ff, proxy.proxied(
                merged.video_master_url, "m3u8", referer=merged.video_referer))
            pcmd = ([ff, "-y", "-hide_banner", "-loglevel", "error",
                     "-progress", "pipe:1", "-nostats"] + cmd[2:])
            progress.update(task_id, description=f"⬇ {out_file.name} {info}")
            rc, err = _run_ffmpeg_progress(
                pcmd, dur,
                lambda pct, bps: progress.update(
                    task_id, completed=pct, speed=_fmt_speed(bps)))
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
        _say(progress, f"[✔] Tamamlandı: {out_file.name}")
        logs.log.info("download ok: %s (%d ses, altyazı=%s)",
                      out_file.name, len(merged.audios), bool(sub_path))
    else:
        _say(progress, f"[!] İndirme başarısız (ffmpeg kodu {rc}).")
        if err:
            _say(progress, f"[dim]{err.strip().splitlines()[-1] if err.strip() else ''}[/dim]")
        logs.log.error("download FAIL rc=%s: %s | %s",
                       rc, out_file.name, (err or "").strip().replace("\n", " ")[-300:])
    return rc
