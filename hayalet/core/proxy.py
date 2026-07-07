"""Yerel 'impersonating' HLS proxy — izleme için ses+görüntü+altyazı sorununu çözer.

CDN, TLS taklidi olmayan istemcileri (mpv/ffmpeg/vlc/potplayer) 403 ile engelliyor;
ses ayrı rendition; segmentler .jpg gibi gizli. Çözüm: oynatıcı localhost'taki bu
proxy'ye bağlanır, proxy tüm playlist/segment isteklerini curl-cffi (impersonate) ile
çekip verir. Playlist URL'leri proxy'ye yönlendirilir; segmentler .ts olarak sunulur.

Ayrıca Türkçe altyazı, master'a bir HLS SUBTITLES kanalı olarak enjekte edilir; böylece
her oynatıcı altyazıyı native (DEFAULT) görür — ayrı bir CLI parametresine gerek kalmaz.
"""
from __future__ import annotations

import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from curl_cffi import requests as creq

from hayalet.core.session import SessionState

# hls.js yerel kopyası (izleme sayfası dış CDN'e bağımlı olmasın). Dosya yoksa
# handler CDN'e yönlendirir (güvenlik ağı).
_ASSETS = Path(__file__).resolve().parent.parent / "assets"
_HLS_CDN = "https://cdn.jsdelivr.net/npm/hls.js@1.5.15/dist/hls.min.js"
_hls_cache: bytes | None = None


def _hls_js() -> bytes | None:
    global _hls_cache
    if _hls_cache is None:
        try:
            _hls_cache = (_ASSETS / "hls.min.js").read_bytes()
        except Exception:
            _hls_cache = b""
    return _hls_cache or None


def _b64(u: str) -> str:
    return base64.urlsafe_b64encode(u.encode()).decode()


def _unb64(s: str) -> str:
    return base64.urlsafe_b64decode(s.encode()).decode()


def build_subs_playlist(vtt_local_url: str) -> str:
    """Tek .vtt'yi saran VOD altyazı media playlist'i (vtt zaten proxy URL'si)."""
    return (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:99999\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PLAYLIST-TYPE:VOD\n"
        f"#EXTINF:99999.0,\n{vtt_local_url}\n#EXT-X-ENDLIST\n"
    )


def build_master_playlist(video_variants, audios, subtitle=None) -> str:
    """Birden çok kaynaktan sentetik HLS master (tüm URL'ler proxy'lenmiş olmalı).

    video_variants: [(local_url, bandwidth, height)]  (en az bir tane)
    audios:         [(local_url, name, lang, is_default)]  (0+; boşsa ses videoda gömülü)
    subtitle:       (local_subs_playlist_url, name, lang) | None
    """
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    aud_grp = 'AUDIO="aud"' if audios else ""
    sub_grp = 'SUBTITLES="subs"' if subtitle else ""

    for i, (url, name, lang, is_def) in enumerate(audios):
        lines.append(
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",'
            f'NAME="{name}",LANGUAGE="{lang or "und"}",'
            f'DEFAULT={"YES" if is_def else "NO"},AUTOSELECT=YES,'
            f'URI="{url}"'
        )
    if subtitle:
        surl, sname, slang = subtitle
        lines.append(
            '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",'
            f'NAME="{sname}",LANGUAGE="{slang or "und"}",'
            f'DEFAULT=NO,AUTOSELECT=YES,FORCED=NO,URI="{surl}"'
        )

    for url, bw, height in video_variants:
        attrs = f"BANDWIDTH={bw or 3000000}"
        if height:
            attrs += f",RESOLUTION={int(height*16/9)}x{height}"
        for g in (aud_grp, sub_grp):
            if g:
                attrs += "," + g
        lines.append("#EXT-X-STREAM-INF:" + attrs)
        lines.append(url)
    return "\n".join(lines) + "\n"


class HLSProxy:
    def __init__(self, session: SessionState, referer: str,
                 subtitle_url: str | None = None,
                 subtitle_referer: str | None = None):
        self.session = session
        self.referer = referer
        self.subtitle_url = subtitle_url
        self.subtitle_referer = subtitle_referer or referer
        self._virtual: dict[int, tuple[str, str]] = {}
        self._vcount = 0
        self._lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def virtual(self, text: str, kind: str = "m3u8") -> str:
        """Sentetik bir playlist'i barındır → yerel URL döndür (hls.js/ffmpeg çeker)."""
        with self._lock:
            vid = self._vcount
            self._vcount += 1
            self._virtual[vid] = (text, kind)
        return f"{self.base}/v.{kind}?i={vid}"

    def start(self) -> "HLSProxy":
        self._thread.start()
        return self

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
        except Exception:
            pass

    def proxied(self, real_url: str, kind: str = "ts",
                referer: str | None = None) -> str:
        # CDN segmentleri .jpg gibi gizliyor; oynatıcılar reddediyor. Proxy URL'sine
        # sahte uzantı (.m3u8 / .ts / .vtt) vererek kabul edilmesini sağlarız.
        # referer verilirse (çok-kaynaklı birleştirme) URL'ye gömülür; alt istekler
        # bu referer'ı devralır.
        u = f"{self.base}/s.{kind}?u={_b64(real_url)}"
        if referer:
            u += f"&r={_b64(referer)}"
        return u

    def _player_page(self, src: str, title: str) -> str:
        """Tarayıcıda HLS oynatan sayfa (hls.js) + ses/altyazı/kalite seçicileri.

        Tarayıcılar .m3u8'i çoğunlukla native oynatamaz; hls.js segmentleri yerel
        proxy'den çeker (proxy CDN'e impersonate ile gider). Altyazı master'a
        SUBTITLES kanalı olarak enjekte edilir; ayrı ses rendition'ları ve kalite
        varyantları master'da olduğu gibi kaldığından hls.js üzerinden seçilebilir.
        """
        head = (
            "<!doctype html><html lang=\"tr\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>İzle</title><style>"
            "*{box-sizing:border-box}"
            "html,body{margin:0;height:100%;background:#000;overflow:hidden;"
            "font-family:system-ui,'Segoe UI',sans-serif}"
            "#wrap{position:fixed;inset:0}"
            "video{width:100%;height:100%;background:#000}"
            "#bar{position:fixed;top:0;left:0;right:0;display:flex;gap:12px;"
            "align-items:center;flex-wrap:wrap;padding:10px 14px;color:#fff;"
            "background:linear-gradient(#000c,#0000);opacity:0;"
            "transition:opacity .25s;z-index:5}"
            "#wrap:hover #bar,#bar:focus-within{opacity:1}"
            "#bar .t{font-weight:600;margin-right:auto;max-width:48vw;overflow:hidden;"
            "text-overflow:ellipsis;white-space:nowrap}"
            "#bar label{font-size:12px;opacity:.75;margin-right:5px}"
            "#bar select{background:rgba(20,20,20,.85);color:#fff;border:1px solid #555;"
            "border-radius:6px;padding:5px 8px;font-size:13px;outline:none;cursor:pointer}"
            ".grp{display:flex;align-items:center}.hidden{display:none}"
            "</style></head><body><div id=\"wrap\">"
            "<video id=\"v\" controls autoplay playsinline></video>"
            "<div id=\"bar\"><span class=\"t\" id=\"ttl\"></span>"
            "<span class=\"grp\" id=\"g-audio\"><label>Ses</label>"
            "<select id=\"audio\"></select></span>"
            "<span class=\"grp\" id=\"g-sub\"><label>Altyazı</label>"
            "<select id=\"sub\"></select></span>"
            "<span class=\"grp\" id=\"g-quality\"><label>Kalite</label>"
            "<select id=\"quality\"></select></span>"
            "</div></div>"
            "<script src=\"/hls.js\"></script>"
            "<script>"
        )
        cfg = "var src=" + json.dumps(src) + ",title=" + json.dumps(title) + ";"
        body = (
            "var v=document.getElementById('v');"
            "var $=function(i){return document.getElementById(i)};"
            "$('ttl').textContent=title;document.title=title;"
            "function fill(sel,items,cur){sel.innerHTML='';items.forEach(function(it){"
            "var o=document.createElement('option');o.value=it.v;o.textContent=it.l;"
            "if(String(it.v)===String(cur))o.selected=true;sel.appendChild(o);});}"
            "function grp(id,show){$(id).classList.toggle('hidden',!show);}"
            "if(window.Hls&&Hls.isSupported()){"
            "var h=new Hls({subtitleDisplay:true});h.loadSource(src);h.attachMedia(v);"
            "function refreshQuality(){var lv=h.levels||[];if(lv.length>1){"
            "var items=[{v:-1,l:'Otomatik'}];lv.forEach(function(L,i){"
            "items.push({v:i,l:(L.height?L.height+'p':(Math.round((L.bitrate||0)/1000)+'k'))});});"
            "fill($('quality'),items,h.autoLevelEnabled?-1:h.currentLevel);grp('g-quality',true);"
            "}else{grp('g-quality',false);}}"
            "function refreshAudio(){var at=h.audioTracks||[];if(at.length>1){"
            "var items=at.map(function(T,i){return {v:i,l:(T.name||T.lang||('Ses '+(i+1)))};});"
            "fill($('audio'),items,h.audioTrack);grp('g-audio',true);"
            "}else{grp('g-audio',false);}}"
            "function refreshSub(){var st=h.subtitleTracks||[];"
            "var items=[{v:-1,l:'Kapalı'}];st.forEach(function(T,i){"
            "items.push({v:i,l:(T.name||T.lang||('Altyazı '+(i+1)))});});"
            "fill($('sub'),items,h.subtitleDisplay?h.subtitleTrack:-1);grp('g-sub',true);}"
            "$('quality').onchange=function(){h.currentLevel=parseInt(this.value,10);};"
            "$('audio').onchange=function(){h.audioTrack=parseInt(this.value,10);};"
            "$('sub').onchange=function(){var i=parseInt(this.value,10);"
            "h.subtitleDisplay=(i>=0);h.subtitleTrack=i;};"
            "h.on(Hls.Events.MANIFEST_PARSED,function(){v.play().catch(function(){});"
            "refreshQuality();refreshAudio();refreshSub();});"
            "h.on(Hls.Events.AUDIO_TRACKS_UPDATED,refreshAudio);"
            "h.on(Hls.Events.SUBTITLE_TRACKS_UPDATED,refreshSub);"
            "h.on(Hls.Events.LEVEL_SWITCHED,function(e,d){"
            "if($('quality').options.length)$('quality').value=String(h.autoLevelEnabled?-1:d.level);});"
            "}else if(v.canPlayType('application/vnd.apple.mpegurl')){"
            "v.src=src;v.addEventListener('loadedmetadata',function(){v.play().catch(function(){});});"
            "$('bar').classList.add('hidden');"
            "}else{document.body.innerHTML='<p style=\"color:#fff;padding:1em\">"
            "Taray\\u0131c\\u0131 HLS oynatam\\u0131yor.</p>';}"
            "</script></body></html>"
        )
        return head + cfg + body

    def _subs_playlist(self) -> str:
        """Tek .vtt'yi saran, VOD altyazı media playlist'i."""
        vtt = self.proxied(self.subtitle_url, "vtt", referer=self.subtitle_referer)
        return build_subs_playlist(vtt)

    def _rewrite(self, text: str, base_url: str, referer: str | None = None) -> str:
        # Master playlist ise URL'ler alt-playlist (m3u8); media playlist ise segment (ts).
        # Dikkat: media playlist'lerde '#EXT-X-MEDIA-SEQUENCE' var → sadece STREAM-INF'e bak.
        # referer alt istekler için devralınır (çok-kaynaklı birleştirmede kritik).
        is_master = "#EXT-X-STREAM-INF" in text
        seg_kind = "m3u8" if is_master else "ts"
        inject_subs = is_master and bool(self.subtitle_url)

        out = []
        for line in text.splitlines():
            if line.startswith("#EXT-X-STREAM-INF") and inject_subs:
                if "SUBTITLES=" not in line:
                    line = line.rstrip() + ',SUBTITLES="subs"'
                out.append(line)
            elif line.startswith("#"):
                line = re.sub(
                    r'URI="([^"]+)"',
                    lambda m: 'URI="' + self.proxied(
                        urljoin(base_url, m.group(1)), "m3u8", referer=referer) + '"',
                    line,
                )
                out.append(line)
            elif line.strip():
                out.append(self.proxied(
                    urljoin(base_url, line.strip()), seg_kind, referer=referer))
            else:
                out.append(line)

        if inject_subs:
            media = ('#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Türkçe",'
                     'LANGUAGE="tr",DEFAULT=YES,AUTOSELECT=YES,FORCED=NO,'
                     f'URI="{self.base}/subs.m3u8"')
            # #EXTM3U'dan hemen sonra ekle
            for i, ln in enumerate(out):
                if ln.startswith("#EXTM3U"):
                    out.insert(i + 1, media)
                    break
            else:
                out.insert(0, media)
        return "\n".join(out)

    def _handler(proxy):
        session = proxy.session
        # Oynatıcının segmenti/playlist'i yarıda bırakması (seek, kapatma, sonraki
        # segmente geçiş) socket'i resetler — bunlar normal, traceback basmayalım.
        _CONN_ERR = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # sessiz
                pass

            def handle_one_request(self):
                try:
                    super().handle_one_request()
                except _CONN_ERR:
                    self.close_connection = True

            def _fetch(self, real, timeout, stream=False, referer=None):
                return creq.get(
                    real,
                    impersonate=session.impersonate,
                    headers={"User-Agent": session.user_agent,
                             "Referer": referer or proxy.referer},
                    cookies=session.cookies,
                    timeout=timeout,
                    stream=stream,
                )

            def _safe_write(self, body):
                try:
                    self.wfile.write(body)
                except _CONN_ERR:
                    pass

            def do_GET(self):
                parsed = urlparse(self.path)

                # hls.js kütüphanesi (yerel kopya; yoksa CDN'e yönlendir)
                if parsed.path == "/hls.js":
                    js = _hls_js()
                    if js:
                        self.send_response(200)
                        self.send_header("Content-Type",
                                         "application/javascript; charset=utf-8")
                        self.send_header("Content-Length", str(len(js)))
                        self.send_header("Cache-Control", "max-age=86400")
                        self.end_headers()
                        self._safe_write(js)
                    else:
                        self.send_response(302)
                        self.send_header("Location", _HLS_CDN)
                        self.end_headers()
                    return

                # Tarayıcı oynatıcı sayfası (hls.js)
                if parsed.path == "/player.html":
                    q = parse_qs(parsed.query)
                    src = _unb64(q["u"][0]) if "u" in q else ""
                    title = _unb64(q["t"][0]) if "t" in q else "video"
                    body = proxy._player_page(src, title).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self._safe_write(body)
                    return

                # Enjekte edilen altyazı media playlist'i
                if parsed.path == "/subs.m3u8" and proxy.subtitle_url:
                    body = proxy._subs_playlist().encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self._safe_write(body)
                    return

                # Sentetik (virtual) playlist — çok-kaynaklı birleştirilmiş master/subs
                if parsed.path.startswith("/v."):
                    q = parse_qs(parsed.query)
                    text, kind = proxy._virtual.get(int(q.get("i", ["-1"])[0]), ("", "m3u8"))
                    body = text.encode()
                    self.send_response(200 if text else 404)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl"
                                     if kind == "m3u8" else "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self._safe_write(body)
                    return

                q = parse_qs(parsed.query)
                if "u" not in q:
                    self.send_error(400)
                    return
                real = _unb64(q["u"][0])
                referer = _unb64(q["r"][0]) if "r" in q else None

                # Segment (.ts) = büyük ikili gövde. Tümünü belleğe alıp yazmak yerine
                # parça parça (stream) aktar: oynatıcı beklemeden oynatmaya başlar ve
                # yavaş/büyük segmentte 30sn'lik sabit timeout'a takılmaz.
                if parsed.path.endswith(".ts"):
                    self._stream_segment(real, referer)
                else:
                    self._proxy_playlist(real, parsed, referer)

            def _stream_segment(self, real, referer=None):
                # CDN segmentleri ara sıra tamamen takılıyor (curl 28: <1 byte/sec).
                # Tek takılan segment tüm ffmpeg indirmesini düşürmesin: segmenti
                # tampona alıp, hata olursa taze bağlantıyla birkaç kez yeniden dene.
                import time
                data = ctype = None
                for attempt in range(3):
                    r = None
                    try:
                        r = self._fetch(real, timeout=60, stream=True, referer=referer)
                        buf = bytearray()
                        for chunk in r.iter_content(chunk_size=65536):
                            if chunk:
                                buf += chunk
                        data = bytes(buf)
                        ctype = r.headers.get("content-type") or "video/mp2t"
                        break
                    except _CONN_ERR:
                        return                       # istemci gitti → bırak
                    except Exception:
                        if attempt == 2:
                            try:
                                self.send_error(502, "segment fetch failed")
                            except _CONN_ERR:
                                pass
                            return
                        time.sleep(0.5 * (attempt + 1))  # kısa geri çekilme, tekrar dene
                    finally:
                        if r is not None:
                            try:
                                r.close()
                            except Exception:
                                pass

                try:
                    self.send_response(200)
                    self.send_header("Content-Type", ctype or "video/mp2t")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self._safe_write(data)
                except _CONN_ERR:
                    pass

            def _proxy_playlist(self, real, parsed, referer=None):
                r = None
                for attempt in range(3):
                    try:
                        r = self._fetch(real, timeout=60, referer=referer)
                        break
                    except Exception as e:
                        if attempt == 2:
                            try:
                                self.send_error(502, str(e))
                            except _CONN_ERR:
                                pass
                            return
                        import time
                        time.sleep(0.5 * (attempt + 1))

                body = r.content
                ct = r.headers.get("content-type", "") or "application/octet-stream"
                low = real.lower()
                if ".m3u8" in low or body[:7] == b"#EXTM3U":
                    body = proxy._rewrite(r.text, real, referer).encode()
                    ct = "application/vnd.apple.mpegurl"
                elif parsed.path.endswith(".vtt") or ".vtt" in low:
                    ct = "text/vtt"

                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self._safe_write(body)

        return Handler
