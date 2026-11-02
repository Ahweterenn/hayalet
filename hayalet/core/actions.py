"""Aksiyon katmanı: tarayıcıda (hls.js) izleme, ffmpeg ile indirme.

İkisi de yerel HLS proxy üzerinden SessionState'teki cookie + user-agent +
referer ile CDN'e bağlanır (anti-bot için kritik). Ayrı Türkçe .vtt varsa
softsub olarak muxlanır (indirme) ya da HLS altyazı kanalına enjekte edilir
(izleme, bkz. proxy.py).
"""
from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

from hayalet import config
from hayalet.core import logs, subsync, utils
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


_SYNC_JOIN_S = 180      # hizalama ölçümü için indirme sonrası en fazla bekleme

# Geçici ".part" adına yazarken ffmpeg'e verilecek konteyner (bkz. download()).
_CONTAINER = {".mkv": "matroska", ".mp4": "mp4"}


def _shift_embedded_sub(ff: str, out_file: Path, sub_text: str,
                        res: "subsync.SyncResult", progress=None) -> None:
    """Ölçülen kaymayı indirilmiş dosyadaki altyazıya uygular.

    Video ve ses KOPYALANIR (yeniden kodlama yok), yalnız altyazı akışı
    kaydırılmış sürümüyle değiştirilir — bu yüzden saniyeler sürer. Yeni dosya
    önce geçici ada yazılır, ancak ffmpeg başarıyla bitince yerine geçer:
    yarıda kesilme elde bozuk bir dosya bırakmasın.
    """
    tmp_vtt = config.CACHE_DIR / f"{out_file.stem}.shift.vtt"
    tmp_out = out_file.with_name(f"{out_file.stem}.sync{out_file.suffix}")
    try:
        tmp_vtt.parent.mkdir(parents=True, exist_ok=True)
        tmp_vtt.write_text(subsync.shift_vtt(sub_text, res.offset, res.rate),
                           encoding="utf-8")
        r = subprocess.run(
            [ff, "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(out_file), "-i", str(tmp_vtt),
             "-map", "0:v", "-map", "0:a", "-map", "1:0",
             "-c:v", "copy", "-c:a", "copy", "-c:s", "srt",
             "-metadata:s:s:0", "language=tur",
             "-metadata:s:s:0", "title=Türkçe",
             str(tmp_out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 or not tmp_out.exists():
            _say(progress, "[!] Altyazı kaydırması uygulanamadı, dosya olduğu "
                           "gibi bırakıldı.")
            logs.log.warning("altyazı kaydırma FAIL rc=%s: %s",
                             r.returncode, (r.stderr or "").strip()[-200:])
            return
        tmp_out.replace(out_file)
        if res.rate != 1.0:
            _say(progress, f"[✔] Altyazı zamanla kayıyordu (kare hızı farkı, oran "
                           f"{res.rate:.4f}); sese göre düzeltildi.")
        else:
            _say(progress, f"[✔] Altyazı sese göre {res.offset:+.1f} sn kaydırıldı "
                           f"(puan {res.score:.2f}).")
        logs.log.info("altyazı kaydırıldı: %s | %s", out_file.name, res.reason)
    except Exception as e:
        logs.log.warning("altyazı kaydırma atlandı: %s", e)
    finally:
        for p in (tmp_vtt, tmp_out):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass


def _start_subsync(proxy, session: SessionState, merged) -> None:
    """Altyazı-ses hizalamasını ARKA PLANDA ölçer, sonucu `proxy.subsync`e yazar.

    Oynatma beklemez: ölçüm sürerken tarayıcı altyazıyı olduğu gibi gösterir,
    sonuç gelince yüklü cue'ları yerinde kaydırır (bkz. proxy._PLAYER_TEMPLATE).
    Ölçüm yapılamıyorsa (ffmpeg yok, altyazı yok) "hazır, kayma 0" yazılır ki
    tarayıcı boşuna yoklamaya devam etmesin.

    Altyazıyı ÇAĞIRANIN `Network`'ü ile değil kendi kopyasıyla indirir: tek bir
    curl-cffi istemcisi thread'ler arasında güvenli değil ve bu iş /next'i
    işleyen tarayıcı thread'iyle aynı anda çalışabiliyor.
    """
    proxy.subsync = {"ready": False, "offset": 0.0}
    ff = _require("ffmpeg")
    if not ff or not merged.subtitle_url or not merged.audios:
        proxy.subsync = {"ready": True, "offset": 0.0}
        return

    def _olc() -> None:
        net2 = None
        try:
            from dataclasses import replace
            net2 = Network(replace(session, cookies=dict(session.cookies)))
            sub_text = net2.get(merged.subtitle_url,
                                referer=merged.subtitle_referer).text
            ses = next((a for a in merged.audios if not a.is_turkish),
                       merged.audios[0])
            pr = subprocess.run(
                subsync.probe_cmd(ff, proxy.proxied(ses.url, "m3u8",
                                                    referer=ses.referer)),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=900)
            res = subsync.analyze(pr.stderr, sub_text)
            logs.log.info("izleme altyazı hizalama: %s", res.reason)
            proxy.subsync = {"ready": True,
                             "offset": res.offset if res.apply else 0.0,
                             "rate": res.rate if res.apply else 1.0}
        except Exception as e:
            logs.log.info("izleme altyazı hizalama atlandı: %s", e)
            proxy.subsync = {"ready": True, "offset": 0.0}
        finally:
            if net2 is not None:
                try:
                    net2.close()
                except Exception:
                    pass

    threading.Thread(target=_olc, daemon=True).start()


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
def _video_first_pts(net: Network, merged) -> float | None:
    """Videonun ilk parçasındaki başlangıç zaman damgası (sn); okunamazsa None.

    Damga parçanın ilk paketlerinde olduğu için yalnız başı istenir. Anlık ağ
    hatasına karşı bir kez daha denenir; olmazsa not eklenmez (eski davranış).
    """
    import m3u8
    from hayalet.core.proxy import first_pts
    try:
        u, ref = merged.video_master_url, merged.video_referer
        pl = m3u8.loads(net.get(u, referer=ref).text, uri=u)
        if pl.is_variant and pl.playlists:
            u = pl.playlists[0].absolute_uri
            pl = m3u8.loads(net.get(u, referer=ref).text, uri=u)
        seg = pl.segments[0].absolute_uri
    except Exception:
        return None
    for _ in range(2):
        try:
            return first_pts(net.get(seg, referer=ref,
                                     headers={"Range": "bytes=0-131071"}).content)
        except Exception:
            pass
    return None


def _build_watch_master(proxy, net: Network, session: SessionState, merged,
                        sub_text: str | None = None):
    """Birleştirilmiş akıştan, proxy'de barındırılan sentetik HLS master URL'si
    kurar. Dönüş: (yerel_master_url, ses_sayısı, altyazı_var_mı).

    Aynı proxy içinde birden çok kez çağrılabilir (bölümden bölüme geçişte yeni
    master kurmak için) — bu yüzden watch()'tan ayrı bir yardımcıdır.
    """
    from hayalet.core.m3u8_parser import list_variants
    from hayalet.core.proxy import build_master_playlist, build_subs_playlist

    # Düz progressive dosya (ör. anime fansub MP4'ü): HLS değil → variant/proxy/
    # sentetik master yok. CDN açık olduğundan tarayıcıya DOĞRUDAN adres verilir;
    # oynatıcı (loadStream) .mp4'ü native <video> ile oynatır. Altyazı/ses kanalı
    # olmadığı için (gömülü) ses sayısı 0, altyazı yok.
    if getattr(merged, "kind", "hls") == "file":
        return merged.video_master_url, 0, False

    # Video kalite varyantları (kalite seçimi için); yoksa tek media playlist
    try:
        variants = list_variants(net, session, merged.video_master_url,
                                 merged.video_referer)
    except Exception:
        variants = []
    if variants:
        vv = [(proxy.proxied(v.url, "m3u8", referer=merged.video_referer),
               v.bandwidth, v.height, v.codecs) for v in variants]
    else:
        vv = [(proxy.proxied(merged.video_master_url, "m3u8",
                             referer=merged.video_referer), 3000000, 0)]

    audios = [(proxy.proxied(a.url, "m3u8", referer=a.referer),
               a.name, a.lang, a.is_default) for a in merged.audios]

    subtitle = None
    if merged.subtitle_url:
        # sub_text verilirse altyazı uzaktan çekilmek yerine HAZIR METİN olarak
        # proxy'de barındırılır — hizalama ölçümü bitince kaydırılmış altyazıyla
        # yeni bir master kurmak için (Android yolu, bkz. hayalet_app.subsync_*).
        if sub_text is not None:
            vtt = proxy.virtual(sub_text, "vtt")
        else:
            vtt = proxy.proxied(merged.subtitle_url, "vtt",
                                referer=merged.subtitle_referer)
            # Altyazının 0'ı videonun ilk PTS'sine eşlensin (X-TIMESTAMP-MAP).
            # Yoksa oynatıcı tahmin ediyor: ölçüldü, hls.js'te 1,4 sn, Media3'te
            # ~0,7 sn erken. Bölüme özgü, her yeni bölümde yeniden okunur.
            proxy.vtt_pts = _video_first_pts(net, merged)
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
    # Düz dosya kaynağında ayrı altyazı yok (gömülü) → hizalama ölçümü anlamsız
    # ve HLS varsayan probe zincirini kırardı; atla.
    if getattr(merged, "kind", "hls") != "file":
        _start_subsync(proxy, session, merged)
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
            _start_subsync(proxy, session, nxt["merged"])   # kayma bölüme özgü
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

    # Düz progressive dosya (anime fansub MP4'ü): HLS/proxy/çoklu ses/altyazı yok.
    # CDN açık olduğundan ffmpeg kaynağı DOĞRUDAN okur; tek geçişte kopyalar.
    if getattr(merged, "kind", "hls") == "file":
        out_file = out_dir / f"{safe}.mp4"
        tmp_file = out_file.with_name(out_file.name + ".part")
        base_cmd = [ff, "-y", "-i", merged.video_master_url,
                    "-c", "copy", "-f", "mp4", str(tmp_file)]
        info = "(düz MP4)"
        err = ""
        if progress is not None and task_id is not None:
            dur = _ffprobe_duration(ff, merged.video_master_url)
            pcmd = ([ff, "-y", "-hide_banner", "-loglevel", "error",
                     "-progress", "pipe:1", "-nostats"] + base_cmd[2:])
            progress.update(task_id, description=f"⬇ {out_file.name} {info}")
            rc, err = _run_ffmpeg_progress(
                pcmd, dur, lambda pct, bps: progress.update(task_id, completed=pct))
        else:
            print(f"[⬇] İndiriliyor: {out_file.name}  {info}")
            rc = subprocess.run(base_cmd).returncode
        if rc == 0:
            try:
                tmp_file.replace(out_file)
            except OSError as e:
                _say(progress, f"[!] İndirilen dosya yeniden adlandırılamadı: {e}")
                return 1
            _say(progress, f"[✔] Tamamlandı: {out_file.name}")
            logs.log.info("download ok (file): %s", out_file.name)
        else:
            tmp_file.unlink(missing_ok=True)
            _say(progress, f"[!] İndirme başarısız (ffmpeg kodu {rc}).")
            logs.log.error("download FAIL (file) rc=%s: %s | %s",
                           rc, out_file.name, (err or "").strip()[-300:])
        return rc

    from hayalet.core.proxy import HLSProxy
    proxy = HLSProxy(session, merged.video_referer).start()

    # Türkçe altyazıyı yerel dosyaya indir (aynı ffmpeg geçişinde gömülür).
    # Birkaç kez deneniyor: altyazılar ayrı bir CDN'de duruyor ve o hostlar
    # ARALIKLI kesiliyor (ölçüldü: aynı adres bir denemede TLS el sıkışmasında
    # reset, biraz sonra sorunsuz). Hata SESSİZCE yutulmamalı — eskiden öyleydi
    # ve kullanıcı altyazısız indirdiğini fark etmiyordu.
    sub_path: Path | None = None
    sub_text = ""
    if merged.subtitle_url:
        son_hata: Exception | None = None
        for _ in range(3):
            try:
                raw = net.get(merged.subtitle_url,
                              referer=merged.subtitle_referer).content
                sub_path = config.CACHE_DIR / f"{safe}.tr.vtt"
                sub_path.parent.mkdir(parents=True, exist_ok=True)
                sub_path.write_bytes(raw)
                sub_text = raw.decode("utf-8", "replace")
                break
            except Exception as e:
                son_hata, sub_path = e, None
        if sub_path is None:
            _say(progress, "[!] Altyazı indirilemedi, ALTYAZISIZ devam ediliyor "
                           f"({type(son_hata).__name__}).")
            logs.log.warning("altyazı indirilemedi: %s | %s",
                             merged.subtitle_url, son_hata)

    # Altyazı-ses hizalaması indirmeyle PARALEL ölçülür (bkz. core/subsync.py).
    # Önüne konsaydı her indirmeye ~70 sn eklerdi; paralel çalışınca ölçülen
    # olağan durumda (altyazı zaten hizalı) ek maliyet SIFIR, yalnız gerçek bir
    # kayma bulunursa sonunda altyazı yeniden mux'lanıyor.
    sync_sonuc: dict = {}
    sync_thread: threading.Thread | None = None
    if sub_text and merged.audios:
        hiza_ses = next((a for a in merged.audios if not a.is_turkish),
                        merged.audios[0])

        def _olc() -> None:
            try:
                aurl = proxy.proxied(hiza_ses.url, "m3u8",
                                     referer=hiza_ses.referer)
                pr = subprocess.run(subsync.probe_cmd(ff, aurl),
                                    capture_output=True, text=True,
                                    encoding="utf-8", errors="replace",
                                    timeout=900)
                sync_sonuc["r"] = subsync.analyze(pr.stderr, sub_text)
            except Exception as e:                  # ölçüm asla indirmeyi bozmaz
                logs.log.info("altyazı hizalama ölçümü atlandı: %s", e)

        sync_thread = threading.Thread(target=_olc, daemon=True)
        sync_thread.start()

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
    rc = 1                      # finally'de tanımsız kalmasın (hizalama beklemesi)
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
        # ffmpeg konteyneri DOSYA UZANTISINDAN seçer. Geçici ad ".part" ile
        # bittiği için seçemiyor ("Unable to choose an output format") ve
        # indirme daha başlamadan ölüyordu. Formatı elle vermek şart.
        cmd += ["-f", _CONTAINER.get(out_file.suffix.lower(), "matroska"),
                str(tmp_file)]
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
        # Ölçüm proxy üzerinden ses okuyor: proxy'yi durdurmadan önce bitmesini
        # bekle. Yalnız indirme başarılıysa beklenir; başarısızsa düzeltilecek
        # bir dosya zaten yok.
        if sync_thread is not None and rc == 0:
            sync_thread.join(timeout=_SYNC_JOIN_S)
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
        res = sync_sonuc.get("r")
        if res is not None:
            logs.log.info("altyazı hizalama: %s", res.reason)
            if res.apply:
                _shift_embedded_sub(ff, out_file, sub_text, res, progress)
    else:
        tmp_file.unlink(missing_ok=True)        # yarım/bozuk .part → sonraki taramada görünmesin
        _say(progress, f"[!] İndirme başarısız (ffmpeg kodu {rc}).")
        if err:
            _say(progress, f"[dim]{err.strip().splitlines()[-1] if err.strip() else ''}[/dim]")
        logs.log.error("download FAIL rc=%s: %s | %s",
                       rc, out_file.name, (err or "").strip().replace("\n", " ")[-300:])
    return rc
