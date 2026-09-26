// Oynatıcı: HLS/MP4 (hls.js + proxy), YouTube, senkron, altyazı katmanı,
// kendi kontrol çubuğu.
//
// Senkron kuralları eski sayfadan birebir taşındı; her biri ölçülmüş bir
// hatanın düzeltmesi:
//  * isSyncing: uzaktan gelen komutu uygularken tarayıcının ateşlediği
//    play/pause/seeked olayları sunucuya GERİ gönderilmez (sonsuz döngü).
//    Olaylar asenkron geldiği için bayrak gecikmeli iner.
//  * syncPlay/syncPause 0,75 sn'den küçük farkta sarmaz: HLS'te gereksiz sarma
//    kısa bir "waiting" doğurup odayı yanlışlıkla beklemeye sokuyordu.
//  * Kayma düzeltmesi (lider olmayanlar): >2 sn atla, 0,15-2 sn arası hızı
//    1,12 / 0,88 yap, altında 1,0.
//  * Oda beklemedeyken gelen oynat/durdur/sar kuyruğa alınır, bekleme bitince
//    uygulanır.
//  * Yerel bekleme 1,5 sn sürerse duyurulur (anlık takılmalar odayı durdurmasın).
//  * Altyazı tarayıcının ::cue'su ile değil kendi katmanımızla çizilir: bazı
//    Chrome sürümleri ::cue stilini sonradan güncellemiyor.

import { $, h, icon, toast, store } from './ui.js';

const isYouTube = u => /youtube\.com|youtu\.be/i.test(u || '');
const isMedia = u => /\.(mp4|webm|m3u8|ogg)($|\?)/i.test(u || '') || /m3u8/i.test(u || '');
const ytId = u => (String(u).match(/(?:v=|\/embed\/|\/shorts\/|youtu\.be\/)([A-Za-z0-9_-]{11})/) || [])[1] || null;
const isForced = v => /forced|zorunlu/i.test(String(v || ''));

const SUB_STYLE_KEY = 'oda.subStyle';
const DEFAULT_SUB = { size: 100, color: '#ffffff', bg: 'none' };
const SUB_BG = { none: 'transparent', soft: 'rgba(0,0,0,.45)', solid: 'rgba(0,0,0,.8)' };
// Uygulamadaki seçenekler (Settings.subSize / subColor / subBg).
const SUB_SIZES = [['Küçük', 75], ['Normal', 100], ['Büyük', 127]];
const SUB_COLORS = [['Beyaz', '#ffffff'], ['Sarı', '#ffe94a']];
const SUB_BGS = [['Yok', 'none'], ['Yarı saydam', 'soft'], ['Koyu', 'solid']];
// "Sonraki bölüm" düğmesi bölümün son 5 dakikasında (PlayerActivity.NEXT_WINDOW_MS).
const NEXT_WINDOW = 5 * 60;
// Bitmesine bu kadar kala (jenerik) bir kez sonraki bölüm kartı.
const UP_NEXT = 25;
const pad2 = n => String(n).padStart(2, '0');
const clock = sec => {
    sec = Math.max(0, Math.floor(isFinite(sec) ? sec : 0));
    const hh = Math.floor(sec / 3600), mm = Math.floor(sec % 3600 / 60), ss = sec % 60;
    return hh ? `${hh}:${pad2(mm)}:${pad2(ss)}` : `${pad2(mm)}:${pad2(ss)}`;
};

export class Player {
    constructor(root, { roomId, emit, onChange }) {
        this.root = root;
        this.roomId = roomId;
        this.emit = emit;
        const changed = onChange || (() => { });
        // Menü açıksa parça listesi değişince (kalite/ses/altyazı) tazelensin.
        this.onChange = () => { this._refreshMenu(); changed(); };
        this.onNext = null;
        this.onPrefetch = null;
        this.onEnded = null;
        this.fill = false;
        this.brightness = 1;
        this.video = $('#video', root);
        this.ytWrap = $('#yt', root);
        this.frame = $('#frame', root);
        this.subLayer = $('#subs', root);
        this.mode = 'none';
        this.hls = null;
        this.yt = null;
        this.ytStarted = false;
        this.isSyncing = false;
        this.isLeader = false;
        this.canControl = true;
        this.roomBuffering = false;
        this.resumeAfterBuffering = false;
        this.pendingRemote = null;
        this.localBufferingReported = false;
        this.bufferTimer = null;
        this.heartbeatTimer = null;
        this.loadToken = 0;
        this.activeTextTrack = null;
        this.manualTracks = [];      // {id, label, el}
        this.selectedSub = null;     // 'hls:N' | 'manual:id' | null
        this.subStyle = { ...DEFAULT_SUB, ...safeJson(store.get(SUB_STYLE_KEY)) };
        this._bindVideo();
        this._bindControls();
        this.applySubStyle(this.subStyle);
    }

    // --- durum ----------------------------------------------------------
    time() {
        if (this.mode === 'youtube' && this.yt && this.yt.getCurrentTime) return this.yt.getCurrentTime() || 0;
        if (this.mode === 'html5') return this.video.currentTime || 0;
        return 0;
    }
    duration() {
        if (this.mode === 'youtube' && this.yt && this.yt.getDuration) return this.yt.getDuration() || 0;
        if (this.mode === 'html5') return this.video.duration || 0;
        return 0;
    }
    playing() {
        if (this.mode === 'youtube' && this.yt && this.yt.getPlayerState) return this.yt.getPlayerState() === 1;
        if (this.mode === 'html5') return !this.video.paused && !this.video.ended;
        return false;
    }
    hasMedia() { return this.mode === 'html5' || this.mode === 'youtube'; }

    setLeader(v) {
        this.isLeader = !!v;
        clearInterval(this.heartbeatTimer);
        this.heartbeatTimer = null;
        if (!this.isLeader) return;
        // Yalnız lider odanın saatini yayınlar; herkes yayınlasaydı en geride
        // kalan izleyici diğerlerini sürekli geri sarardı.
        this.heartbeatTimer = setInterval(() => {
            if (!this.hasMedia() || this.roomBuffering || this.localBufferingReported) return;
            this.emit('sync-heartbeat', { currentTime: this.time(), isPlaying: this.playing() });
        }, 1000);
    }

    setCanControl(v) {
        this.canControl = !!v;
        this.root.classList.toggle('locked', !this.canControl);
    }

    // --- yükleme ----------------------------------------------------------
    clear() {
        this.loadToken++;
        this._teardown();
        this.mode = 'none';
        this.root.classList.remove('has-media', 'is-yt', 'is-frame', 'playing');
        this._updateControls();
        this.onChange();
    }

    load(url, subtitles = [], { startTime = 0, autoplay = false, onReady = null } = {}) {
        if (!url) return this.clear();
        const token = ++this.loadToken;
        this._teardown();
        this._upOffered = false;
        this._prefetched = false;
        this.hideUpNext();
        this.localBufferingReported = false;
        this.roomBuffering = false;
        this.resumeAfterBuffering = false;
        this.pendingRemote = null;
        this.root.classList.remove('is-yt', 'is-frame');
        const ready = () => {
            if (token !== this.loadToken) return;
            this.root.classList.add('has-media');
            if (typeof onReady === 'function') onReady();
            this._updateControls();
            this.onChange();
        };

        if (isYouTube(url)) {
            this.mode = 'youtube';
            this.root.classList.add('is-yt');
            this._loadYouTube(url, startTime, autoplay, ready);
        } else if (isMedia(url)) {
            this.mode = 'html5';
            this._loadMedia(url, subtitles, ready);
        } else {
            // Oynatıcı olmayan sayfa: gösterilir ama senkronlanamaz.
            this.mode = 'iframe';
            this.root.classList.add('is-frame', 'has-media');
            this.frame.src = url;
            toast('Bu bağlantı bir oynatıcı değil; herkes kendi başına izler.', 'warn');
            this.onChange();
        }
    }

    _teardown() {
        clearTimeout(this.bufferTimer);
        try { this.video.pause(); } catch (_) { }
        if (this.hls) { try { this.hls.destroy(); } catch (_) { } this.hls = null; }
        this.video.removeAttribute('src');
        try { this.video.load(); } catch (_) { }
        this._clearTracks();
        if (this.yt) { try { this.yt.destroy(); } catch (_) { } this.yt = null; }
        this.ytWrap.innerHTML = '';
        this.frame.removeAttribute('src');
        this._hidePrompt();
    }

    proxyUrl(raw) {
        if (!/^https?:\/\//i.test(raw) || raw.includes('/api/proxy?url=')) return raw;
        const u = new URL('/api/proxy', location.origin);
        u.searchParams.set('url', raw);
        u.searchParams.set('roomId', this.roomId);
        try { u.searchParams.set('ref', new URL(raw).origin); } catch (_) { }
        return u.toString();
    }

    _loadMedia(url, subtitles, ready) {
        const src = this.proxyUrl(url);
        if (/m3u8/i.test(url) && window.Hls && Hls.isSupported()) {
            const mobile = /Android|iPhone|iPad|Mobile/i.test(navigator.userAgent);
            const player = this;
            const Base = Hls.DefaultConfig.loader;
            // Playlist'teki her adres (segment, ses, altyazı) proxy'den geçer:
            // CDN yalnız hayalet'in TLS kimliğine cevap veriyor.
            class ProxyLoader extends Base {
                load(ctx, cfg, cb) {
                    try { if (ctx && ctx.url) ctx.url = player.proxyUrl(String(ctx.url)); } catch (_) { }
                    return super.load(ctx, cfg, cb);
                }
            }
            let netRetry = 0, mediaRetry = 0;
            const hls = this.hls = new Hls({
                enableWorker: true, loader: ProxyLoader, startLevel: -1,
                renderTextTracksNatively: true, subtitleDisplay: true,
                maxBufferLength: mobile ? 12 : 30, maxMaxBufferLength: mobile ? 30 : 120,
                manifestLoadingTimeOut: 20000, levelLoadingTimeOut: 20000, fragLoadingTimeOut: 25000,
            });
            hls.loadSource(src);
            hls.attachMedia(this.video);
            hls.on(Hls.Events.MANIFEST_PARSED, () => { ready(); this._addManualSubs(subtitles); });
            for (const ev of [Hls.Events.AUDIO_TRACKS_UPDATED, Hls.Events.SUBTITLE_TRACKS_UPDATED, Hls.Events.LEVEL_SWITCHED])
                hls.on(ev, () => this.onChange());
            hls.on(Hls.Events.SUBTITLE_TRACK_SWITCH, () => this._bindHlsTextTrack());
            hls.on(Hls.Events.ERROR, (_, d) => {
                if (!d || !d.fatal) return;
                if (d.type === Hls.ErrorTypes.NETWORK_ERROR && netRetry < 4) { netRetry++; try { hls.startLoad(); } catch (_) { } return; }
                if (d.type === Hls.ErrorTypes.MEDIA_ERROR && mediaRetry < 2) { mediaRetry++; try { hls.recoverMediaError(); } catch (_) { } return; }
                this._bufferEnd();
                const code = (d.response && d.response.code) || 0;
                toast(code >= 400 && code < 500
                    ? 'Videonun bağlantısının süresi dolmuş; içeriği yeniden açın.'
                    : 'Video yüklenemedi.', 'err');
            });
        } else {
            this.video.src = src;
            this.video.addEventListener('loadedmetadata', () => { ready(); this._addManualSubs(subtitles); }, { once: true });
        }
    }

    _loadYouTube(url, startTime, autoplay, ready) {
        const id = ytId(url);
        if (!id) { toast('YouTube videosu bulunamadı.', 'err'); return; }
        const create = () => {
            this.ytWrap.innerHTML = '';
            const holder = h('div');
            this.ytWrap.append(holder);
            this.ytStarted = false;
            this.yt = new YT.Player(holder, {
                width: '100%', height: '100%', videoId: id,
                playerVars: { autoplay: autoplay ? 1 : 0, start: Math.floor(startTime), controls: 1, rel: 0, modestbranding: 1, fs: 0, playsinline: 1 },
                events: {
                    onReady: e => {
                        if (autoplay) { e.target.seekTo(Math.floor(startTime), true); e.target.playVideo(); }
                        setTimeout(ready, 300);
                    },
                    onStateChange: e => this._ytState(e),
                },
            });
        };
        if (window.YT && window.YT.Player) return create();
        window.onYouTubeIframeAPIReady = create;
        if (!document.querySelector('script[data-yt]')) {
            document.head.append(h('script', { src: 'https://www.youtube.com/iframe_api', 'data-yt': '1' }));
        }
    }

    _ytState(e) {
        const t = e.target.getCurrentTime();
        if (e.data === 1) {           // PLAYING
            this.ytStarted = true;
            this._hidePrompt();
            this._bufferEnd(t);
            if (!this.isSyncing) this.emit('play', { currentTime: t });
        } else if (e.data === 2) {    // PAUSED
            this._bufferEnd(t);
            if (!this.isSyncing) this.emit('pause', { currentTime: t });
        } else if (e.data === 3) {    // BUFFERING
            if (!this.isSyncing && this.ytStarted) this._bufferSchedule(t);
        } else if (e.data === 0) {    // ENDED
            this._bufferEnd(t);
        }
        this._updateControls();
    }

    // --- uzaktan gelen komutlar ----------------------------------------------
    _guard(ms) {
        this.isSyncing = true;
        clearTimeout(this._syncT);
        this._syncT = setTimeout(() => { this.isSyncing = false; }, ms);
    }

    syncPlay(t) {
        this._guard(500);
        this._hidePrompt();
        const time = typeof t === 'number' ? t : this.time();
        if (this.mode === 'youtube' && this.yt && this.yt.playVideo) {
            try { this.yt.seekTo(time, true); this.yt.playVideo(); } catch (_) { }
        } else if (this.mode === 'html5') {
            if (Math.abs(this.video.currentTime - time) > 0.75) this.video.currentTime = time;
            const p = this.video.play();
            if (p && p.catch) p.catch(err => { if (err.name === 'NotAllowedError') this._showPrompt(); });
        }
    }

    syncPause(t) {
        this._guard(300);
        const time = typeof t === 'number' ? t : this.time();
        if (this.mode === 'youtube' && this.yt && this.yt.pauseVideo) {
            try { this.yt.seekTo(time, true); this.yt.pauseVideo(); } catch (_) { }
        } else if (this.mode === 'html5') {
            if (Math.abs(this.video.currentTime - time) > 0.75) this.video.currentTime = time;
            this.video.pause();
        }
    }

    syncSeek(t) {
        this._guard(300);
        const time = typeof t === 'number' ? t : this.time();
        if (this.mode === 'youtube' && this.yt && this.yt.seekTo) { try { this.yt.seekTo(time, true); } catch (_) { } }
        else if (this.mode === 'html5') this.video.currentTime = time;
    }

    remote(type, currentTime) {
        if (this.roomBuffering) { this.pendingRemote = { type, currentTime }; return; }
        if (type === 'play') this.syncPlay(currentTime);
        else if (type === 'pause') this.syncPause(currentTime);
        else this.syncSeek(currentTime);
    }

    heartbeat({ currentTime, isPlaying }) {
        if (this.isLeader || this.mode === 'iframe' || this.roomBuffering || this.isSyncing) return;
        const target = typeof currentTime === 'number' ? currentTime : this.time();
        const mine = this.time();
        const drift = Math.abs(target - mine);
        if (isPlaying) {
            if (!this.playing()) return this.syncPlay(target);
            if (drift > 2.0) { this.syncSeek(target); if (this.mode === 'html5') this.video.playbackRate = 1; }
            else if (drift > 0.15) {
                if (this.mode === 'html5') this.video.playbackRate = mine < target ? 1.12 : 0.88;
                else if (drift > 0.85) this.syncSeek(target);
            } else if (this.mode === 'html5' && this.video.playbackRate !== 1) this.video.playbackRate = 1;
        } else {
            if (this.playing()) { this.syncPause(target); if (this.mode === 'html5') this.video.playbackRate = 1; return; }
            if (drift > 0.5) this.syncSeek(target);
        }
    }

    /** Oda genelinde bekleme (biri takıldı). */
    setRoomBuffering(on, currentTime) {
        if (on) {
            if (this.roomBuffering) return;
            this.roomBuffering = true;
            this.resumeAfterBuffering = this.playing();
            this.syncPause(typeof currentTime === 'number' ? currentTime : this.time());
            return;
        }
        const was = this.roomBuffering;
        const resume = was && this.resumeAfterBuffering;
        this.roomBuffering = false;
        this.resumeAfterBuffering = false;
        if (this.pendingRemote) {
            const a = this.pendingRemote; this.pendingRemote = null;
            this.remote(a.type, typeof a.currentTime === 'number' ? a.currentTime : this.time());
        } else if (resume) {
            this.syncPlay(typeof currentTime === 'number' ? currentTime : this.time());
        }
    }

    // --- yerel olaylar -----------------------------------------------------
    _bufferSchedule(t) {
        if (this.mode === 'iframe' || this.isSyncing || this.roomBuffering || this.localBufferingReported) return;
        if (!this.playing()) return;
        clearTimeout(this.bufferTimer);
        this.bufferTimer = setTimeout(() => {
            if (this.isSyncing || this.roomBuffering || this.localBufferingReported || !this.playing()) return;
            this.localBufferingReported = true;
            this.emit('buffering-start', { currentTime: typeof t === 'number' ? t : this.time() });
        }, 1500);
    }

    _bufferEnd(t) {
        clearTimeout(this.bufferTimer);
        if (!this.localBufferingReported) return;
        this.localBufferingReported = false;
        this.emit('buffering-end', { currentTime: typeof t === 'number' ? t : this.time() });
    }

    _bindVideo() {
        const v = this.video;
        v.addEventListener('play', () => { this._updateControls(); if (!this.isSyncing && this.mode === 'html5') this.emit('play', { currentTime: v.currentTime }); });
        v.addEventListener('pause', () => { this._updateControls(); if (!this.isSyncing && this.mode === 'html5') this.emit('pause', { currentTime: v.currentTime }); });
        let lastSeek = 0;
        v.addEventListener('seeked', () => {
            if (this.isSyncing || this.mode !== 'html5') return;
            const now = Date.now(); if (now - lastSeek < 300) return; lastSeek = now;
            this.emit('seek', { currentTime: v.currentTime });
        });
        v.addEventListener('waiting', () => { this.root.classList.add('waiting'); if (this.mode === 'html5') this._bufferSchedule(v.currentTime); });
        v.addEventListener('stalled', () => { if (this.mode === 'html5') this._bufferSchedule(v.currentTime); });
        for (const ev of ['playing', 'canplay']) v.addEventListener(ev, () => { this.root.classList.remove('waiting'); if (this.mode === 'html5') this._bufferEnd(v.currentTime); });
        // Bitince: ev sahibinde 5 sn geri sayımlı kart (uygulamadaki gibi).
        v.addEventListener('ended', () => { if (this.onNext) this.showUpNext(5); else if (this.onEnded) this.onEnded(); });
        v.addEventListener('timeupdate', () => this._updateTime());
        v.addEventListener('progress', () => this._updateTime());
        v.addEventListener('durationchange', () => this._updateTime());
        v.addEventListener('volumechange', () => this._updateVolume());
        v.addEventListener('error', () => {
            if (this.mode !== 'html5' || !v.getAttribute('src') && !this.hls) return;
            this._bufferEnd(v.currentTime || 0);
            toast('Video açılamadı.', 'err');
        });
        if (v.textTracks) v.textTracks.addEventListener('addtrack', () => this.onChange());
    }

    // --- altyazı -------------------------------------------------------------
    _clearTracks() {
        if (this.video.textTracks) for (const t of this.video.textTracks) { try { t.mode = 'disabled'; } catch (_) { } }
        this.video.querySelectorAll('track').forEach(t => t.remove());
        this.manualTracks = [];
        this.selectedSub = null;
        this._setTextTrack(null);
    }

    _addManualSubs(list) {
        const items = (list || []).map(s => typeof s === 'string' ? { url: s, label: null } : s)
            .filter(s => s && s.url && !isForced(`${s.label} ${s.url}`));
        for (const s of items) {
            const id = Math.random().toString(36).slice(2, 9);
            const label = /tr|türk|turk/i.test(`${s.label || ''} ${s.url}`) ? 'Türkçe' : (s.label || 'Altyazı');
            const el = h('track', { kind: 'subtitles', label, srclang: label === 'Türkçe' ? 'tr' : 'und', src: this.proxyUrl(s.url) });
            el.addEventListener('load', () => this.onChange());
            this.video.append(el);
            this.manualTracks.push({ id, label, el });
        }
        // Türkçe altyazı varsa varsayılan olarak açılır (hayalet'in amacı bu).
        const tr = this.manualTracks.find(t => t.label === 'Türkçe');
        const pref = store.get('oda.subOff') === '1' ? null : (tr ? 'manual:' + tr.id : null);
        if (pref) setTimeout(() => this.setSub(pref), 200);
        this.onChange();
    }

    _setTextTrack(track) {
        if (this.activeTextTrack && this.activeTextTrack !== track) this.activeTextTrack.oncuechange = null;
        this.activeTextTrack = track || null;
        const render = () => {
            const cues = this.activeTextTrack && this.activeTextTrack.activeCues;
            this.subLayer.replaceChildren();
            if (!cues || !cues.length) return;
            // Bazı kaynakların VTT'sinde aynı satır aynı zamanlı iki kez var
            // (Slow Horses 1x01'de 4 yerde); ekranda üst üste iki kez çıkıyordu.
            const seen = new Set();
            for (let i = 0; i < cues.length; i++) {
                const text = String(cues[i].text || '').replace(/<[^>]*>/g, '')
                    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&');
                for (const line of text.split('\n').filter(Boolean)) {
                    if (seen.has(line)) continue;
                    seen.add(line);
                    this.subLayer.append(h('span', { class: 'sub-line' }, line));
                }
            }
        };
        if (this.activeTextTrack) this.activeTextTrack.oncuechange = render;
        render();
    }

    _bindHlsTextTrack() {
        setTimeout(() => {
            for (const t of this.video.textTracks) {
                if (t.mode === 'showing') { t.mode = 'hidden'; this._setTextTrack(t); return; }
            }
        }, 60);
    }

    /** id: 'hls:N' | 'manual:ID' | null (kapalı) */
    setSub(id) {
        this.selectedSub = id || null;
        store.set('oda.subOff', id ? '0' : '1');
        if (this.hls) this.hls.subtitleTrack = -1;
        for (const t of this.manualTracks) { try { t.el.track.mode = 'disabled'; } catch (_) { } }
        if (!id) { this._setTextTrack(null); this.onChange(); return; }
        const [kind, key] = id.split(':');
        if (kind === 'hls' && this.hls) {
            this.hls.subtitleTrack = Number(key);
            this._bindHlsTextTrack();
        } else if (kind === 'manual') {
            const m = this.manualTracks.find(t => t.id === key);
            if (m) {
                // 'hidden': cue'lar ayrıştırılır ama tarayıcı çizmez; biz çizeriz.
                const tryShow = n => {
                    try { m.el.track.mode = 'hidden'; this._setTextTrack(m.el.track); }
                    catch (_) { if (n < 3) setTimeout(() => tryShow(n + 1), 300); }
                };
                tryShow(0);
            }
        }
        this.onChange();
    }

    applySubStyle(s) {
        this.subStyle = { ...this.subStyle, ...s };
        store.set(SUB_STYLE_KEY, JSON.stringify(this.subStyle));
        this.subLayer.style.setProperty('--sub-scale', String(this.subStyle.size / 100));
        this.subLayer.style.setProperty('--sub-color', this.subStyle.color);
        this.subLayer.style.setProperty('--sub-bg', SUB_BG[this.subStyle.bg] || 'transparent');
    }

    // --- parça seçimi (kalite, ses, altyazı) -----------------------------------
    tracks() {
        const out = { levels: [], audio: [], subs: [] };
        if (this.hls) {
            const lv = this.hls.levels || [];
            if (lv.length > 1) {
                out.levels.push({ id: -1, label: 'Otomatik', active: this.hls.autoLevelEnabled });
                lv.map((l, i) => ({ id: i, h: l.height || 0, label: l.height ? l.height + 'p' : Math.round((l.bitrate || 0) / 1000) + ' kbps' }))
                    .sort((a, b) => b.h - a.h)
                    .forEach(l => out.levels.push({ ...l, active: !this.hls.autoLevelEnabled && this.hls.currentLevel === l.id }));
            }
            const au = this.hls.audioTracks || [];
            if (au.length > 1) au.forEach((a, i) => out.audio.push({ id: i, label: a.name || a.lang || `Ses ${i + 1}`, active: this.hls.audioTrack === i }));
            (this.hls.subtitleTracks || []).forEach((s, i) => {
                if (isForced(`${s.name} ${s.lang}`)) return;
                out.subs.push({ id: 'hls:' + i, label: s.name || s.lang || `Altyazı ${i + 1}`, active: this.selectedSub === 'hls:' + i });
            });
        }
        this.manualTracks.forEach(t => out.subs.push({ id: 'manual:' + t.id, label: t.label, active: this.selectedSub === 'manual:' + t.id }));
        return out;
    }
    setLevel(id) { if (this.hls) { this.hls.currentLevel = id; this.onChange(); } }
    setAudio(id) { if (this.hls) { this.hls.audioTrack = id; this.onChange(); } }

    // --- kontroller (uygulamanın oynatıcısıyla aynı: PlayerControls.java) -----
    _bindControls() {
        const r = this.root;
        this.ui = {
            play: $('[data-act=play]', r), time: $('.c-time', r), end: $('.c-end', r), seek: $('.c-seek', r),
            buffered: $('.c-buffered', r), mute: $('[data-act=mute]', r), vol: $('.c-vol', r),
            next: $('[data-act=next]', r), fit: $('[data-act=fit]', r), menu: $('#p-menu', r),
            indicator: $('.p-indicator', r), scrub: $('.p-scrub', r), unlock: $('.p-unlock', r),
            bubbleL: $('.p-bubble-l', r), bubbleR: $('.p-bubble-r', r), upnext: $('.p-upnext', r),
        };
        this.locked = false;
        this.showRemaining = true;
        const denied = () => toast('Kumanda şu an yalnız ev sahibinde.', 'warn');
        this.ui.play.addEventListener('click', () => this.toggle());
        $('[data-act=back]', r).addEventListener('click', () => this.nudge(-10));
        $('[data-act=fwd]', r).addEventListener('click', () => this.nudge(10));
        this.ui.seek.addEventListener('input', () => { this._scrubbing = true; this._keep(); this._updateTime(this.ui.seek.value * this.duration() / 1000); });
        this.ui.seek.addEventListener('change', () => {
            this._scrubbing = false;
            if (!this.canControl) { denied(); return this._updateTime(); }
            this.seekTo(this.ui.seek.value * this.duration() / 1000);
            this.showControls(true);
        });
        // Kalan süre / toplam süre arasında geçiş (uygulamadaki gibi).
        this.ui.end.addEventListener('click', () => { this.showRemaining = !this.showRemaining; this._updateTime(); });
        this.ui.mute.addEventListener('click', () => { this.video.muted = !this.video.muted; if (!this.video.muted && this.video.volume === 0) this.video.volume = 0.6; });
        this.ui.vol.addEventListener('input', () => { this.video.volume = this.ui.vol.value / 100; this.video.muted = this.video.volume === 0; });
        this.ui.next.addEventListener('click', () => { this.hideUpNext(); if (this.onNext) this.onNext(); });
        this.ui.fit.addEventListener('click', () => { this.setFill(!this.fill); this.showControls(true); });
        $('[data-act=tracks]', r).addEventListener('click', () => this.openMenu('main'));
        $('[data-act=settings]', r).addEventListener('click', () => this.toggleMenu());
        $('[data-act=lock]', r).addEventListener('click', () => this.setLocked(true));
        this.ui.unlock.addEventListener('click', () => this.setLocked(false));

        const pip = $('[data-act=pip]', r);
        if (document.pictureInPictureEnabled) {
            pip.hidden = false;
            pip.addEventListener('click', () => {
                if (document.pictureInPictureElement) return document.exitPictureInPicture().catch(() => { });
                if (this.mode !== 'html5') return toast('Mini ekran yalnız videoda açılır.');
                this.video.requestPictureInPicture().catch(() => toast('Mini ekran açılamadı.'));
            });
        }
        this._bindGestures();
        for (const ev of ['pointermove', 'focusin']) r.addEventListener(ev, e => { if (e.pointerType !== 'touch' && !this.locked) this.showControls(true); });
        this._updateVolume();
        this.setCanControl(true);
    }

    // Dokunmatikte uygulamadaki hareketler: tek dokunuş kontrolleri açar/kapar;
    // kenarlarda çift dokunuş geri/ileri sarar (art arda dokunuşlar birikir),
    // ortada çift dokunuş ya da iki parmak sığdır/doldur; yana kaydırma zamanda
    // sarar; dikey kaydırma sol yarıda parlaklık, sağ yarıda ses. Farede tek tık
    // oynat/durdur, çift tık tam ekran (masaüstü alışkanlığı).
    _bindGestures() {
        const r = this.root, screen = $('.screen', r);
        const skip = e => e.target.closest('.controls button, .controls input, .p-menu, .p-upnext, .p-unlock, .prompt, .empty, .yt, .frame');
        const pts = new Map();
        let lastTap = 0, tapTimer = null, swipe = null, pinch0 = 0, gestured = false;
        let lastSeek = 0, lastSide = 0;
        const sideOf = e => {
            const rect = screen.getBoundingClientRect();
            const x = (e.clientX - rect.left) / rect.width;
            return x < 0.35 ? -1 : x > 0.65 ? 1 : 0;
        };
        const reset = () => { swipe = null; pinch0 = 0; if (gestured) this._indicateEnd(); gestured = false; };

        screen.addEventListener('pointerdown', e => {
            if (skip(e) || this.locked) return;
            pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
            if (pts.size === 2) {
                const [a, b] = [...pts.values()];
                pinch0 = Math.hypot(a.x - b.x, a.y - b.y);
                swipe = null;
            } else if (pts.size === 1 && e.pointerType !== 'mouse') {
                const rect = screen.getBoundingClientRect();
                swipe = { x: e.clientX, y: e.clientY, w: rect.width, h: rect.height, left: e.clientX - rect.left < rect.width / 2, mode: null, start: 0 };
            }
        });
        screen.addEventListener('pointermove', e => {
            if (!pts.has(e.pointerId)) return;
            pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
            if (pts.size === 2) {
                if (!pinch0) return;
                const [a, b] = [...pts.values()];
                const d = Math.hypot(a.x - b.x, a.y - b.y);
                // Açınca doldur, kapatınca sığdır; ara durum yok (uygulamadaki gibi).
                if (Math.abs(d - pinch0) > 40) { this.setFill(d > pinch0); pinch0 = 0; gestured = true; }
                return;
            }
            if (!swipe || this.mode !== 'html5') return;
            const dy = e.clientY - swipe.y, dx = e.clientX - swipe.x;
            if (!swipe.mode) {
                if (Math.abs(dy) > 16 && Math.abs(dy) > Math.abs(dx) * 1.5) {
                    swipe.mode = swipe.left ? 'bright' : 'vol';
                    swipe.y = e.clientY;
                    swipe.start = swipe.mode === 'bright' ? this.brightness : (this.video.muted ? 0 : this.video.volume);
                } else if (Math.abs(dx) > 24 && Math.abs(dx) > Math.abs(dy) * 1.5 && this.duration()) {
                    // Yana kaydırma: zamanda sarma (ekran genişliği = 2 dakika).
                    swipe.mode = 'seek';
                    swipe.x = e.clientX;
                    swipe.start = this.time();
                } else return;
                gestured = true;
            }
            if (swipe.mode === 'seek') {
                const delta = (e.clientX - swipe.x) / Math.max(1, swipe.w) * 120;
                swipe.target = Math.max(0, Math.min(this.duration(), swipe.start + delta));
                this._scrubHud(swipe.target, swipe.target - swipe.start);
                return;
            }
            // Ekranın tam yüksekliği ≈ %100 değişim.
            const delta = (swipe.y - e.clientY) / Math.max(1, swipe.h) * 1.2;
            if (swipe.mode === 'bright') {
                const v = Math.max(0.15, Math.min(1, swipe.start + delta));
                this.setBrightness(v);
                this._indicate('brightness', v, Math.round(v * 100) + '%');
            } else {
                const v = Math.max(0, Math.min(1, swipe.start + delta));
                this.video.volume = v; this.video.muted = v === 0;
                this._indicate(v === 0 ? 'mute' : 'volume', v, Math.round(v * 100) + '%');
            }
        });
        const finishSeek = commit => {
            if (swipe && swipe.mode === 'seek') {
                this.ui.scrub.hidden = true;
                if (commit && typeof swipe.target === 'number') this.seekTo(swipe.target);
            }
        };
        screen.addEventListener('pointercancel', e => { pts.delete(e.pointerId); if (!pts.size) { finishSeek(false); reset(); } });
        screen.addEventListener('pointerup', e => {
            if (this.locked) {
                if (!skip(e)) this._flashUnlock();
                return;
            }
            pts.delete(e.pointerId);
            if (pts.size) return;
            finishSeek(true);
            const was = gestured;
            reset();
            if (was || skip(e)) return;
            if (!this.ui.menu.hidden) return this.closeMenu();
            const now = Date.now();
            const side = sideOf(e);
            // Çift dokunuşla sarma sürerken aynı taraftaki her dokunuş biraz daha sarar.
            if (e.pointerType !== 'mouse' && side && side === lastSide && now - lastSeek < 700) {
                clearTimeout(tapTimer); lastTap = 0; lastSeek = now;
                return this.nudge(side * 10, false);
            }
            if (now - lastTap < 300) {
                clearTimeout(tapTimer); lastTap = 0;
                if (e.pointerType === 'mouse') return this.onDoubleTap && this.onDoubleTap();
                if (side) { lastSeek = now; lastSide = side; return this.nudge(side * 10, false); }
                return this.setFill(!this.fill);
            }
            lastTap = now;
            tapTimer = setTimeout(() => {
                if (e.pointerType === 'mouse') this.toggle();
                else this.showControls(!r.classList.contains('show-controls'));
            }, e.pointerType === 'mouse' ? 220 : 260);
        });
    }

    setFill(on) {
        this.fill = !!on;
        this.root.classList.toggle('fill', this.fill);
        if (this.ui) $('span', this.ui.fit).textContent = this.fill ? 'Sığdır' : 'Doldur';
    }

    /** Tarayıcı ekran parlaklığına erişemiyor; görüntü karartılır (%15–100). */
    setBrightness(v) {
        this.brightness = v;
        this.video.style.filter = v < 0.995 ? `brightness(${v.toFixed(2)})` : '';
    }

    /** Parlaklık / ses göstergesi: simge + dolum çubuğu + yüzde. */
    _indicate(name, fraction, text) {
        const el = this.ui.indicator;
        clearTimeout(this._indT);
        el.replaceChildren(icon(name), h('i', { class: 'p-ind-bar' }, h('b', { style: { width: Math.round(fraction * 100) + '%' } })), h('span', {}, text));
        el.hidden = false;
    }
    _indicateEnd() {
        clearTimeout(this._indT);
        this._indT = setTimeout(() => { this.ui.indicator.hidden = true; }, 600);
    }

    _scrubHud(target, delta) {
        this.ui.scrub.textContent = `${clock(target)}   ${delta >= 0 ? '+' : '−'}${clock(Math.abs(delta))}`;
        this.ui.scrub.hidden = false;
    }

    // --- kilit -----------------------------------------------------------------
    setLocked(on) {
        this.locked = !!on;
        this.root.classList.toggle('locked-screen', this.locked);
        if (this.locked) { this.showControls(false); this._flashUnlock(); }
        else { this.ui.unlock.hidden = true; this.showControls(true); }
    }
    _flashUnlock() {
        this.ui.unlock.hidden = false;
        clearTimeout(this._unlockT);
        this._unlockT = setTimeout(() => { if (this.locked) this.ui.unlock.hidden = true; }, 2500);
    }

    showControls(on) {
        clearTimeout(this._hideT);
        if (on && this.locked) return;
        this.root.classList.toggle('show-controls', on);
        // Menü açıkken ya da sürüklerken kontroller kendiliğinden kapanmaz.
        if (on && this.playing() && this.ui.menu.hidden && !this._scrubbing) this._hideT = setTimeout(() => this.root.classList.remove('show-controls'), 3500);
        if (!on && !this.ui.menu.hidden) this.closeMenu();
    }
    _keep() { clearTimeout(this._hideT); }

    toggle() {
        if (!this.hasMedia() || this.mode === 'youtube') return;
        if (!this.canControl) return toast('Kumanda şu an yalnız ev sahibinde.', 'warn');
        if (this.video.paused) { const p = this.video.play(); if (p && p.catch) p.catch(() => this._showPrompt()); }
        else this.video.pause();
    }

    toggleSub() {
        const subs = this.tracks().subs;
        if (!subs.length) return toast('Bu videoda altyazı yok.');
        this.setSub(this.selectedSub ? null : subs[0].id);
        toast(this.selectedSub ? 'Altyazı açık' : 'Altyazı kapalı');
    }

    seekTo(t) {
        if (this.mode !== 'html5') return;
        if (!this.canControl) return toast('Kumanda şu an yalnız ev sahibinde.', 'warn');
        this.video.currentTime = Math.max(0, Math.min(t, this.duration() || t));
    }

    /** ±s saniye sar; balon art arda sarmaları toplar ("« 30 sn"). */
    nudge(s, reveal = true) {
        if (this.mode !== 'html5') return;
        if (!this.canControl) return toast('Kumanda şu an yalnız ev sahibinde.', 'warn');
        this.seekTo(this.video.currentTime + s);
        const fwd = s > 0;
        if (this._bubbleFwd !== fwd) this._bubbleSum = 0;
        this._bubbleFwd = fwd;
        this._bubbleSum = (this._bubbleSum || 0) + Math.abs(s);
        const [on, off] = fwd ? [this.ui.bubbleR, this.ui.bubbleL] : [this.ui.bubbleL, this.ui.bubbleR];
        off.hidden = true;
        on.textContent = fwd ? `${this._bubbleSum} sn  »` : `«  ${this._bubbleSum} sn`;
        on.hidden = false;
        clearTimeout(this._bubbleT);
        this._bubbleT = setTimeout(() => { this._bubbleSum = 0; on.hidden = true; }, 750);
        if (reveal) this.showControls(true);
    }

    /** "Sonraki bölüm": fn verilirse hap son 5 dakikada, kart jenerikte ve
     *  bölüm bitince (5 sn geri sayım) çıkar. Yalnız ev sahibinde verilir. */
    setNext(fn) {
        this.onNext = fn || null;
        if (!fn) this.hideUpNext();
        this._updateTime();
    }

    showUpNext(seconds) {
        if (!this.onNext) return;
        clearInterval(this._upT);
        const title = $('#p-title') ? $('#p-title').textContent : '';
        const play = h('button', { class: 'btn light', type: 'button', onclick: () => { this.hideUpNext(); if (this.onNext) this.onNext(); } },
            icon('play'), h('span', {}, 'Oynat'));
        const cancel = h('button', { class: 'btn glass', type: 'button', onclick: () => this.hideUpNext() }, 'İptal');
        this.ui.upnext.replaceChildren(h('div', { class: 'p-up-k' }, 'SONRAKİ BÖLÜM'), h('div', { class: 'p-up-t' }, title),
            h('div', { class: 'p-up-row' }, play, cancel));
        this.ui.upnext.hidden = false;
        if (seconds > 0) {
            let n = seconds;
            const label = $('span', play);
            label.textContent = `Oynat (${n})`;
            this._upT = setInterval(() => {
                if (this.ui.upnext.hidden) return clearInterval(this._upT);
                if (--n <= 0) { this.hideUpNext(); if (this.onNext) this.onNext(); return; }
                label.textContent = `Oynat (${n})`;
            }, 1000);
        }
    }

    hideUpNext() {
        clearInterval(this._upT);
        if (this.ui) this.ui.upnext.hidden = true;
    }

    _updateControls() {
        const playing = this.playing();
        this.root.classList.toggle('playing', playing);
        this.ui.play.replaceChildren(icon(playing ? 'pause' : 'play'));
        this.ui.play.setAttribute('aria-label', playing ? 'Duraklat' : 'Oynat');
        if (!playing && !this.locked) this.showControls(true); else this.showControls(this.root.classList.contains('show-controls'));
        this._updateTime();
    }

    _updateTime(preview) {
        const d = this.duration();
        const t = typeof preview === 'number' ? preview : this.time();
        this.ui.time.textContent = clock(t);
        this.ui.end.textContent = d ? (this.showRemaining ? '-' + clock(Math.max(0, d - t)) : clock(d)) : '';
        if (!this._scrubbing && d) this.ui.seek.value = String(Math.round(t / d * 1000));
        this.ui.seek.style.setProperty('--p', d ? (t / d * 100) + '%' : '0%');
        if (this.mode === 'html5' && d && this.video.buffered.length) {
            let end = 0;
            for (let i = 0; i < this.video.buffered.length; i++) if (this.video.buffered.start(i) <= t + 1) end = Math.max(end, this.video.buffered.end(i));
            this.ui.buffered.style.width = (end / d * 100) + '%';
        }
        const left = d ? d - t : Infinity;
        this.ui.next.hidden = !(this.onNext && this.mode === 'html5' && left <= NEXT_WINDOW);
        // Son dakikalar: sunucu sonraki bölümü şimdiden çözsün (geçişte bekleme olmasın).
        if (this.onNext && this.onPrefetch && this.mode === 'html5' && left <= NEXT_WINDOW
            && typeof preview !== 'number' && !this._prefetched) {
            this._prefetched = true;
            this.onPrefetch();
        }
        // Jenerik: bir kez kart (uygulamadaki gibi).
        if (this.onNext && this.mode === 'html5' && left <= UP_NEXT && !this._upOffered && this.playing()) {
            this._upOffered = true;
            this.showUpNext(0);
        }
    }

    _updateVolume() {
        const v = this.video.muted ? 0 : this.video.volume;
        this.ui.vol.value = String(Math.round(v * 100));
        this.ui.mute.replaceChildren(icon(v === 0 ? 'mute' : 'volume'));
    }

    // --- çark menüsü (uygulamadaki ayarlar kartı) ------------------------------
    toggleMenu() { if (this.ui.menu.hidden) this.openMenu(); else this.closeMenu(); }

    openMenu(page = 'main') {
        this.ui.menu.hidden = false;
        this._renderMenu(page);
        this.showControls(true);
    }

    closeMenu() {
        this.ui.menu.hidden = true;
        if (this.playing() && this.root.classList.contains('show-controls')) this.showControls(true);
    }

    _refreshMenu() { if (this.ui && !this.ui.menu.hidden) this._renderMenu(this._menuPage); }

    _renderMenu(page) {
        this._menuPage = page;
        const t = this.tracks(), st = this.subStyle;
        const row = (label, value, { checked = false, chevron = false, onclick = null } = {}) =>
            h('button', { class: 'p-row', type: 'button', onclick },
                h('span', { class: 'p-row-t' }, label),
                value ? h('span', { class: 'p-row-v' }, value) : null,
                checked ? h('span', { class: 'p-row-c' }, '✓') : null,
                chevron ? h('span', { class: 'p-row-v p-chev' }, '›') : null);
        const pick = (list, fallback, fn) => list.length
            ? list.map(it => row(it.label, null, { checked: it.active, onclick: () => fn(it) }))
            : [row(fallback, null, { checked: true })];
        const nameOf = (list, dflt) => (list.find(x => x.active) || {}).label || dflt;
        const lv = this.hls && this.hls.levels && this.hls.levels[this.hls.currentLevel];
        const size = SUB_SIZES.reduce((a, b) => Math.abs(b[1] - st.size) < Math.abs(a[1] - st.size) ? b : a);
        const color = SUB_COLORS.find(c => c[1] === st.color) || SUB_COLORS[0];
        const bg = SUB_BGS.find(b => b[1] === st.bg) || SUB_BGS[0];
        const style = (patch, back) => () => { this.applySubStyle(patch); this._renderMenu(back); };

        const pages = {
            main: ['Ayarlar', null, () => [
                row('Kalite', lv && lv.height ? lv.height + 'p' : 'Otomatik', { chevron: true, onclick: () => this._renderMenu('quality') }),
                row('Ses', nameOf(t.audio, 'Varsayılan'), { chevron: true, onclick: () => this._renderMenu('audio') }),
                row('Altyazı', nameOf(t.subs, 'Kapalı'), { chevron: true, onclick: () => this._renderMenu('subs') }),
                // Hız yok: odada herkes aynı hızda izler, kayma düzeltmesi hızı 1'e çeker.
            ]],
            quality: ['Kalite', 'main', () => pick(t.levels, 'Otomatik', it => { this.setLevel(it.id); this._renderMenu('main'); })],
            audio: ['Ses', 'main', () => pick(t.audio, 'Varsayılan', it => { this.setAudio(it.id); this._renderMenu('main'); })],
            subs: ['Altyazı', 'main', () => [
                row('Kapalı', null, { checked: !t.subs.some(s => s.active), onclick: () => { this.setSub(null); this._renderMenu('main'); } }),
                ...t.subs.map(s => row(s.label, null, { checked: s.active, onclick: () => { this.setSub(s.id); this._renderMenu('main'); } })),
                h('div', { class: 'p-div' }),
                row('Altyazı Ayarları', size[0], { chevron: true, onclick: () => this._renderMenu('subStyle') }),
            ]],
            subStyle: ['Altyazı Ayarları', 'subs', () => [
                row('Boyut', size[0], { chevron: true, onclick: () => this._renderMenu('subSize') }),
                row('Renk', color[0], { chevron: true, onclick: () => this._renderMenu('subColor') }),
                row('Arka plan', bg[0], { chevron: true, onclick: () => this._renderMenu('subBg') }),
            ]],
            subSize: ['Altyazı Boyutu', 'subStyle', () => SUB_SIZES.map(([l, v]) => row(l, null, { checked: v === size[1], onclick: style({ size: v }, 'subStyle') }))],
            subColor: ['Altyazı Rengi', 'subStyle', () => SUB_COLORS.map(([l, v]) => row(l, null, { checked: v === color[1], onclick: style({ color: v }, 'subStyle') }))],
            subBg: ['Arka Plan', 'subStyle', () => SUB_BGS.map(([l, v]) => row(l, null, { checked: v === bg[1], onclick: style({ bg: v }, 'subStyle') }))],
        };
        const [title, back, build] = pages[page] || pages.main;
        const m = this.ui.menu;
        $('.p-menu-title', m).textContent = title;
        const b = $('.p-menu-back', m);
        b.hidden = !back;
        b.onclick = () => this._renderMenu(back);
        $('.p-menu-body', m).replaceChildren(...build());
    }

    // --- otomatik oynatma engeli ------------------------------------------------
    _showPrompt() {
        const p = $('.prompt', this.root);
        p.hidden = false;
        // Kontroller bu katmanın üstüne binmesin (ortadaki düğmeler çakışıyordu).
        this.root.classList.add('prompting');
        p.onclick = () => {
            p.hidden = true;
            this.root.classList.remove('prompting');
            if (this.mode === 'html5') this.video.play().catch(() => { });
            else if (this.yt) { try { this.yt.unMute(); } catch (_) { } this.yt.playVideo(); }
        };
    }
    _hidePrompt() { const p = $('.prompt', this.root); if (p) p.hidden = true; this.root.classList.remove('prompting'); }
}

function safeJson(s) { try { return JSON.parse(s) || {}; } catch (_) { return {}; } }
