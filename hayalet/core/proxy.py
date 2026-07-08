"""Yerel 'impersonating' HLS proxy — izleme için ses+görüntü+altyazı sorununu çözer.

CDN, TLS taklidi olmayan istemcileri (tarayıcı/ffmpeg) 403 ile engelliyor;
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


# İzleme sayfası: video + tek bir dişli ikonu (sağ üst — sağ alt, native video
# denetimlerinin/tam ekran düğmesinin tam üstüne denk geldiği için taşındı),
# YouTube tarzı katmanlı ayarlar menüsü (Kalite / Ses / Altyazı, altyazının
# içinde Boyut+Renk alt menüsü).
# __SRC__ / __TITLE__ çalışma anında _player_page() içinde değiştirilir.
_PLAYER_TEMPLATE = """<!doctype html><html lang='tr'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>İzle</title><style>
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:#000;overflow:hidden;font-family:system-ui,'Segoe UI',sans-serif}
#wrap{position:fixed;inset:0}
video{width:100%;height:100%;background:#000}
#bar{position:fixed;top:0;left:0;right:0;padding:10px 14px;color:#fff;
background:linear-gradient(#000c,#0000);opacity:0;transition:opacity .25s;
z-index:5;pointer-events:none}
#wrap.active #bar{opacity:1}
#bar .t{font-weight:600;max-width:70vw;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;display:inline-block}
/* Denetimler fareyle etkileşim olmadığında (idle) tamamen gizlenir; #wrap'e
   JS ile eklenen .active sınıfı görünürlüğü yönetir (tam ekranda da çalışır —
   eski :hover mantığı tam ekranda imleç ekranın üzerinde sayıldığından hiç
   gizlenmiyordu). İmleç de idle'da gizlenir. */
#wrap:not(.active){cursor:none}
.ctlbtn{position:fixed;top:8px;width:36px;height:36px;border-radius:50%;
background:rgba(20,20,20,.75);color:#fff;border:1px solid #5558;
display:flex;align-items:center;justify-content:center;cursor:pointer;
font-size:16px;opacity:0;transition:opacity .2s,background .2s;z-index:6;
user-select:none}
.ctlbtn:hover{background:rgba(45,45,45,.9)}
#wrap.active .ctlbtn,#gear.open{opacity:1}
#gear{right:12px;font-size:17px}
#fsBtn{right:56px}
#gear.hidden,#fsBtn.hidden{display:none}
/* Native tam ekran düğmesi videoyu TEK BAŞINA tam ekran yapıyor (gear/menü/
   altyazı katmanımız kayboluyor). Onu gizleyip tüm tam ekranı kendi #wrap
   düğmemiz/F tuşumuz üzerinden yönlendiriyoruz (WebKit/Blink). */
video::-webkit-media-controls-fullscreen-button{display:none}
#menu{position:fixed;right:12px;top:52px;width:260px;
max-height:min(60vh,420px);overflow-y:auto;background:rgba(24,24,24,.94);
backdrop-filter:blur(8px);border:1px solid #444;border-radius:10px;color:#fff;
font-size:13px;box-shadow:0 8px 28px #000a;z-index:7}
#menu.hidden{display:none}
#menu .hd{display:flex;align-items:center;gap:8px;padding:11px 12px;
border-bottom:1px solid #3a3a3a;font-weight:600}
#menu .hd .back{cursor:pointer;opacity:.75;padding:0 4px;font-size:16px}
#menu .hd .back:hover{opacity:1}
#menu .row{display:flex;align-items:center;justify-content:space-between;
padding:9px 14px;cursor:pointer;gap:10px}
#menu .row:hover{background:rgba(255,255,255,.1)}
#menu .row .l{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#menu .row .r{display:flex;align-items:center;gap:6px;opacity:.65;
font-size:12px;flex-shrink:0}
#menu .row .check{color:#4dabf7;opacity:1;font-size:13px}
#menu .row .chev{opacity:.55;font-size:14px}
#menu .sep{height:1px;background:#3a3a3a;margin:4px 0}
#menu .sec{padding:8px 14px 2px;font-size:11px;opacity:.5;
text-transform:uppercase;letter-spacing:.05em}
#capOverlay{position:absolute;left:0;right:0;bottom:7%;display:none;
justify-content:center;pointer-events:none;z-index:4;padding:0 4%}
#capOverlay span{display:inline-block;max-width:92%;text-align:center;
white-space:pre-line;line-height:1.35;border-radius:.2em}
video::cue{color:transparent;background:transparent;text-shadow:none}
</style></head><body><div id='wrap'>
<video id='v' controls autoplay playsinline></video>
<div id='bar'><span class='t' id='ttl'></span></div>
<div id='fsBtn' class='ctlbtn' title='Tam ekran (F)'>⛶</div>
<div id='gear' class='ctlbtn' title='Ayarlar'>⚙</div>
<div id='menu' class='hidden'></div>
<div id='capOverlay'><span id='capText'></span></div>
</div>
<script src='/hls.js'></script>
<script>
var src=__SRC__,title=__TITLE__;
var v=document.getElementById('v');
var wrap=document.getElementById('wrap');
var gear=document.getElementById('gear');
var menu=document.getElementById('menu');
var $=function(i){return document.getElementById(i);};
$('ttl').textContent=title;document.title=title;

var fsBtn=$('fsBtn');

// Video (native denetimleri) klavye fokusunu alırsa tarayıcı kendi dahili
// kısayollarını (boşluk/ok tuşları vb.) devreye sokuyor ve bu olaylar bizim
// document seviyesindeki dinleyicimize HİÇ ulaşmıyor (native tamamen yutuyor) —
// böylece "10sn ileri sar" gibi tutarlı kısayollarımız, kullanıcı denetim
// çubuğuna dokunur dokunmaz beklenmedik/tutarsız bir native davranışa dönüşüyordu.
// Video hiçbir zaman fokusu tutmasın diye anında blur ediyoruz; fare ile
// oynat/durdur/ses/kaydırma çubuğu etkileşimleri bundan etkilenmez, sadece
// klavye fokusunun native'e geçmesi engellenir.
v.addEventListener('focus',function(){v.blur();});

function toggleFullscreen(){
if(document.fullscreenElement)document.exitFullscreen();
else if(wrap.requestFullscreen)wrap.requestFullscreen();
}
fsBtn.onclick=function(e){e.stopPropagation();toggleFullscreen();};

// Denetimleri (bar/gear/fs düğmesi + imleç) fare hareketinde göster, hareketsiz
// kalınca gizle. Menü açıkken asla gizlenmez. Eski :hover mantığı tam ekranda
// hiç gizlenmiyordu; bu, tam ekranda da doğru çalışır.
var hideTimer;
function showControls(){
wrap.classList.add('active');
clearTimeout(hideTimer);
hideTimer=setTimeout(function(){
if(menu.classList.contains('hidden'))wrap.classList.remove('active');
},2800);
}
wrap.addEventListener('mousemove',showControls);
wrap.addEventListener('mousedown',showControls);
wrap.addEventListener('touchstart',showControls,{passive:true});
showControls();

document.addEventListener('keydown',function(e){
if(/^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName))return;
if(!menu.classList.contains('hidden')){
if(e.key==='Escape')closeMenu();
return;
}
var k=e.key.toLowerCase();
if(k==='f'||k===' '||k==='k'||k==='arrowright'||k==='arrowleft'||
k==='arrowup'||k==='arrowdown'||k==='m')showControls();
switch(k){
case 'f':
e.preventDefault();
toggleFullscreen();
break;
case ' ':
case 'k':
e.preventDefault();
v.paused?v.play():v.pause();
break;
case 'arrowright':
e.preventDefault();
v.currentTime+=10;
break;
case 'arrowleft':
e.preventDefault();
v.currentTime-=10;
break;
case 'arrowup':
e.preventDefault();
v.volume=Math.min(1,v.volume+.1);
break;
case 'arrowdown':
e.preventDefault();
v.volume=Math.max(0,v.volume-.1);
break;
case 'm':
e.preventDefault();
v.muted=!v.muted;
break;
}
});

// Altyazı, native cue render yerine KENDİ katmanımızda (#capOverlay) çizilir.
// Neden: native/hls.js cue render'ının stili (boyut/renk/arka plan) tarayıcıya
// göre değişip hiç yansımayabiliyor. Bunun yerine seçili altyazı track'inin
// modunu 'hidden'a çekiyoruz — böylece TARAYICI hiçbir şey ÇİZMİYOR ama cue'lar
// (activeCues) okunabilir kalıyor; metni alıp tamamen kontrolümüzdeki bir
// <span>'e basıyoruz. 'hidden' yapmak ('showing' yerine), altyazının ekranda
// ÇİFT (native + bizimki) görünmesini de kökten engelliyor.
var SUBSIZE={s:'.85em',m:'1.25em',l:'1.75em',xl:'2.25em'};
var SUBSIZE_L={s:'Küçük',m:'Orta',l:'Büyük',xl:'Çok büyük'};
var SUBCOLOR={w:'#fff',y:'#ffeb3b',g:'#00e676',c:'#18ffff'};
var SUBCOLOR_L={w:'Beyaz',y:'Sarı',g:'Yeşil',c:'Camgöbeği'};
var SUBBG={n:'transparent',h:'rgba(0,0,0,.6)',s:'rgba(0,0,0,.9)'};
var SUBBG_L={n:'Yok',h:'Yarı saydam',s:'Koyu'};
var subSize=localStorage.getItem('subsize')||'m';
var subColor=localStorage.getItem('subcolor')||'w';
var subBg=localStorage.getItem('subbg')||'n';
var capOverlay=$('capOverlay');
var capText=$('capText');
function applyCue(){
capText.style.fontSize=(SUBSIZE[subSize]||SUBSIZE.m);
capText.style.color=(SUBCOLOR[subColor]||SUBCOLOR.w);
capText.style.background=(SUBBG[subBg]||SUBBG.n);
capText.style.padding=(subBg==='n')?'0':'.15em .5em';
capText.style.textShadow='0 0 3px #000,0 0 3px #000';
}
applyCue();
function escCueText(t){
var e=t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
return e.replace(/&lt;(\\/?)(i|b|u)&gt;/g,'<$1$2>');
}
function hideNativeCues(){
// hls.js seçili track'i 'showing' yapar (native render açık). 'hidden'a çekip
// native render'ı kapatıyoruz; cue'lar okunmaya devam eder.
if(!h)return;
var tts=v.textTracks;
for(var i=0;i<tts.length;i++){
var tt=tts[i];
if((tt.kind==='subtitles'||tt.kind==='captions')&&tt.mode==='showing')tt.mode='hidden';
}
}
function pollCaptions(){
if(!h){capOverlay.style.display='none';return;}
hideNativeCues();
var text='',tts=v.textTracks;
for(var i=0;i<tts.length;i++){
var tt=tts[i];
if((tt.kind!=='subtitles'&&tt.kind!=='captions')||tt.mode==='disabled')continue;
if(tt.activeCues&&tt.activeCues.length){
var parts=[];
for(var j=0;j<tt.activeCues.length;j++)parts.push(tt.activeCues[j].text||'');
text=parts.join('\\n');
}
}
if(h.subtitleDisplay&&text){
capText.innerHTML=escCueText(text).replace(/\\n/g,'<br>');
capOverlay.style.display='flex';
}else{
capOverlay.style.display='none';
}
}
setInterval(pollCaptions,120);

var stack=['root'];
var h=null;

function openMenu(){stack=['root'];render();menu.classList.remove('hidden');gear.classList.add('open');showControls();}
function closeMenu(){menu.classList.add('hidden');gear.classList.remove('open');showControls();}
function pushView(n){stack.push(n);render();}
function popView(){stack.pop();if(!stack.length)stack=['root'];render();}

gear.onclick=function(e){
e.stopPropagation();
if(menu.classList.contains('hidden'))openMenu();else closeMenu();
};
// Menü içindeki satır tıklamaları render()'da kendi DOM node'unu siliyor
// (menu.innerHTML=''); olay yine de document'a kabarcıklanmaya devam eder ve
// aşağıdaki "dışına tıklayınca kapat" mantığı, artık DOM'da olmayan eski
// node'u "menü dışında" sanıp menüyü anında kapatırdı. Bunu menü konteynerinin
// kendisinde durdurarak önlüyoruz (menu asla kendi kendini silmiyor).
menu.addEventListener('click',function(e){e.stopPropagation();});
document.addEventListener('click',function(e){
if(!menu.classList.contains('hidden')&&!menu.contains(e.target)&&e.target!==gear)closeMenu();
});

function mkEl(tag,cls){
var e=document.createElement(tag);
if(cls)e.className=cls;
return e;
}
function addHeader(title,back){
var hd=mkEl('div','hd');
if(back){
var b=mkEl('span','back');
b.textContent='‹';
b.onclick=popView;
hd.appendChild(b);
}
var t=document.createElement('span');
t.textContent=title;
hd.appendChild(t);
menu.appendChild(hd);
}
function addRow(label,value,checked,onClick,chevron){
var r=mkEl('div','row');
var l=mkEl('div','l');
l.textContent=label;
r.appendChild(l);
var right=mkEl('div','r');
if(value)right.appendChild(document.createTextNode(value));
if(checked){
var c=mkEl('span','check');
c.textContent='✓';
right.appendChild(c);
}
if(chevron){
var cv=mkEl('span','chev');
cv.textContent='›';
right.appendChild(cv);
}
r.appendChild(right);
if(onClick)r.onclick=onClick;
menu.appendChild(r);
}
function addSep(){menu.appendChild(mkEl('div','sep'));}
function addSec(label){
var s=mkEl('div','sec');
s.textContent=label;
menu.appendChild(s);
}

function qualityLabel(){
if(!h)return'';
if(h.autoLevelEnabled)return'Otomatik';
var L=h.levels[h.currentLevel];
return L?(L.height?L.height+'p':(Math.round((L.bitrate||0)/1000)+'k')):'';
}
function audioLabel(){
if(!h||!h.audioTracks||!h.audioTracks.length)return'';
var T=h.audioTracks[h.audioTrack];
return T?(T.name||T.lang||''):'';
}
function subLabel(){
if(!h||!h.subtitleDisplay)return'Kapalı';
var T=(h.subtitleTracks||[])[h.subtitleTrack];
return T?(T.name||T.lang||'Açık'):'Kapalı';
}

function render(){
menu.innerHTML='';
var view=stack[stack.length-1];
if(view==='root'){
addHeader('Ayarlar',false);
if(h){
if(h.levels&&h.levels.length>1){
addRow('Kalite',qualityLabel(),false,function(){pushView('quality');},true);
}
if(h.audioTracks&&h.audioTracks.length>1){
addRow('Ses',audioLabel(),false,function(){pushView('audio');},true);
}
addRow('Altyazı',subLabel(),false,function(){pushView('subtitle');},true);
}else{
addRow('Yükleniyor…',null,false,null,false);
}
}else if(view==='quality'){
addHeader('Kalite',true);
addRow('Otomatik',null,!!h.autoLevelEnabled,function(){h.currentLevel=-1;closeMenu();},false);
(h.levels||[]).forEach(function(L,i){
var lbl=L.height?(L.height+'p'):(Math.round((L.bitrate||0)/1000)+'k');
addRow(lbl,null,(!h.autoLevelEnabled&&h.currentLevel===i),function(){h.currentLevel=i;closeMenu();},false);
});
}else if(view==='audio'){
addHeader('Ses',true);
(h.audioTracks||[]).forEach(function(T,i){
addRow(T.name||T.lang||('Ses '+(i+1)),null,h.audioTrack===i,function(){h.audioTrack=i;closeMenu();},false);
});
}else if(view==='subtitle'){
addHeader('Altyazı',true);
addRow('Kapalı',null,!h.subtitleDisplay,function(){h.subtitleDisplay=false;closeMenu();},false);
(h.subtitleTracks||[]).forEach(function(T,i){
addRow(T.name||T.lang||('Altyazı '+(i+1)),null,h.subtitleDisplay&&h.subtitleTrack===i,function(){h.subtitleDisplay=true;h.subtitleTrack=i;closeMenu();},false);
});
addSep();
addRow('Altyazı Ayarları',null,false,function(){pushView('substyle');},true);
}else if(view==='substyle'){
addHeader('Altyazı Ayarları',true);
addSec('Boyut');
Object.keys(SUBSIZE_L).forEach(function(k){
addRow(SUBSIZE_L[k],null,subSize===k,function(){subSize=k;localStorage.setItem('subsize',k);applyCue();render();},false);
});
addSec('Renk');
Object.keys(SUBCOLOR_L).forEach(function(k){
addRow(SUBCOLOR_L[k],null,subColor===k,function(){subColor=k;localStorage.setItem('subcolor',k);applyCue();render();},false);
});
addSec('Arka Plan');
Object.keys(SUBBG_L).forEach(function(k){
addRow(SUBBG_L[k],null,subBg===k,function(){subBg=k;localStorage.setItem('subbg',k);applyCue();render();},false);
});
}
}

if(window.Hls&&Hls.isSupported()){
// renderTextTracksNatively:true → cue'lar native TextTrack'lere yazılır (bize
// activeCues verir, hls.js kendi <div> altyazı katmanını OLUŞTURMAZ). Ardından
// track modunu 'hidden'a çekip (pollCaptions) native render'ı da kapatıyoruz;
// altyazıyı yalnız kendi katmanımız çiziyor → asla çift görünmüyor.
h=new Hls({subtitleDisplay:true,renderTextTracksNatively:true});
h.loadSource(src);
h.attachMedia(v);
h.on(Hls.Events.MANIFEST_PARSED,function(){
v.play().catch(function(){});
if(!menu.classList.contains('hidden'))render();
});
h.on(Hls.Events.AUDIO_TRACKS_UPDATED,function(){if(!menu.classList.contains('hidden'))render();});
h.on(Hls.Events.SUBTITLE_TRACKS_UPDATED,function(){if(!menu.classList.contains('hidden'))render();});
if(Hls.Events.SUBTITLE_TRACK_SWITCH)h.on(Hls.Events.SUBTITLE_TRACK_SWITCH,hideNativeCues);
h.on(Hls.Events.LEVEL_SWITCHED,function(){if(!menu.classList.contains('hidden'))render();});
}else if(v.canPlayType('application/vnd.apple.mpegurl')){
v.src=src;
v.addEventListener('loadedmetadata',function(){v.play().catch(function(){});});
gear.classList.add('hidden');
}else{
document.body.innerHTML='<p style=\\'color:#fff;padding:1em\\'>Tarayıcı HLS oynatamıyor.</p>';
}
</script></body></html>
"""


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
        # Segment/playlist istekleri arasında TCP/TLS bağlantısını paylaşır (her
        # segment için yeniden el sıkışma yapmaz) — indirme/izleme hızını artırır.
        # curl-cffi Session'ı eşzamanlı thread'lerden kullanmak güvenli (test edildi).
        client_kwargs = {"impersonate": session.impersonate}
        if session.proxy:
            # Video/segment trafiği de aynı proxy'den (ör. Tor) geçmezse, sadece
            # arama/bölüm-sayfası isteklerini gizlemenin bir anlamı kalmaz.
            client_kwargs["proxies"] = {"http": session.proxy, "https": session.proxy}
        self._client = creq.Session(**client_kwargs)
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
        try:
            self._client.close()
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
        """Tarayıcıda HLS oynatan sayfa (hls.js) + sağ alttaki tek ayarlar menüsü.

        Tarayıcılar .m3u8'i çoğunlukla native oynatamaz; hls.js segmentleri yerel
        proxy'den çeker (proxy CDN'e impersonate ile gider). Altyazı master'a
        SUBTITLES kanalı olarak enjekte edilir. Kalite/ses/altyazı seçimi ve
        altyazı boyut-renk ayarı, dişli ikonun açtığı katmanlı (YouTube tarzı)
        tek bir menüde toplanır — bkz. _PLAYER_TEMPLATE.
        """
        return (
            _PLAYER_TEMPLATE
            .replace("__SRC__", json.dumps(src))
            .replace("__TITLE__", json.dumps(title))
        )

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
                return proxy._client.get(
                    real,
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
