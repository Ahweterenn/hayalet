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
        v.addEventListener('ended', () => { if (this.onEnded) this.onEnded(); });
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

    // --- kontroller (uygulamanın oynatıcısıyla aynı: PlayerActivity) ---------
    _bindControls() {
        const r = this.root;
        this.ui = {
            play: $('[data-act=play]', r), time: $('.c-time', r), seek: $('.c-seek', r),
            buffered: $('.c-buffered', r), mute: $('[data-act=mute]', r), vol: $('.c-vol', r),
            next: $('[data-act=next]', r), menu: $('#p-menu', r), indicator: $('.p-indicator', r),
        };
        const denied = () => toast('Kumanda şu an yalnız ev sahibinde.', 'warn');
        this.ui.play.addEventListener('click', () => this.toggle());
        $('[data-act=back]', r).addEventListener('click', () => this.nudge(-10));
        $('[data-act=fwd]', r).addEventListener('click', () => this.nudge(10));
        this.ui.seek.addEventListener('input', () => { this._scrubbing = true; this._updateTime(this.ui.seek.value * this.duration() / 1000); });
        this.ui.seek.addEventListener('change', () => {
            this._scrubbing = false;
            if (!this.canControl) { denied(); return this._updateTime(); }
            this.seekTo(this.ui.seek.value * this.duration() / 1000);
        });
        this.ui.mute.addEventListener('click', () => { this.video.muted = !this.video.muted; if (!this.video.muted && this.video.volume === 0) this.video.volume = 0.6; });
        this.ui.vol.addEventListener('input', () => { this.video.volume = this.ui.vol.value / 100; this.video.muted = this.video.volume === 0; });
        this.ui.next.addEventListener('click', () => { if (this.onNext) this.onNext(); });
        $('[data-act=settings]', r).addEventListener('click', () => this.toggleMenu());

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
        for (const ev of ['pointermove', 'focusin']) r.addEventListener(ev, e => { if (e.pointerType !== 'touch') this.showControls(true); });
        this._updateVolume();
        this.setCanControl(true);
    }

    // Dokunmatikte uygulamadaki hareketler: tek dokunuş kontrolleri açar/kapar,
    // çift dokunuş ya da iki parmak "sığdır / doldur" arasında geçer, sol
    // yarıda dikey kaydırma parlaklık, sağ yarıda ses. Farede tek tık
    // oynat/durdur, çift tık tam ekran (masaüstü alışkanlığı).
    _bindGestures() {
        const r = this.root, screen = $('.screen', r);
        const skip = e => e.target.closest('.controls button, .controls input, .p-menu, .p-next, .prompt, .empty, .yt, .frame');
        const pts = new Map();
        let lastTap = 0, tapTimer = null, swipe = null, pinch0 = 0, gestured = false;
        const reset = () => { swipe = null; pinch0 = 0; if (gestured) this._indicateEnd(); gestured = false; };

        screen.addEventListener('pointerdown', e => {
            if (skip(e)) return;
            pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
            if (pts.size === 2) {
                const [a, b] = [...pts.values()];
                pinch0 = Math.hypot(a.x - b.x, a.y - b.y);
                swipe = null;
            } else if (pts.size === 1 && e.pointerType !== 'mouse') {
                const rect = screen.getBoundingClientRect();
                swipe = { x: e.clientX, y: e.clientY, h: rect.height, left: e.clientX - rect.left < rect.width / 2, mode: null, start: 0 };
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
            if (!swipe.mode) {
                const dy = e.clientY - swipe.y, dx = e.clientX - swipe.x;
                if (Math.abs(dy) < 16 || Math.abs(dy) < Math.abs(dx) * 1.5) return;
                swipe.mode = swipe.left ? 'bright' : 'vol';
                swipe.y = e.clientY;
                swipe.start = swipe.mode === 'bright' ? this.brightness : (this.video.muted ? 0 : this.video.volume);
                gestured = true;
            }
            // Ekranın tam yüksekliği ≈ %100 değişim.
            const delta = (swipe.y - e.clientY) / Math.max(1, swipe.h) * 1.2;
            if (swipe.mode === 'bright') {
                const v = Math.max(0.15, Math.min(1, swipe.start + delta));
                this.setBrightness(v);
                this._indicate('brightness', Math.round(v * 100) + '%');
            } else {
                const v = Math.max(0, Math.min(1, swipe.start + delta));
                this.video.volume = v; this.video.muted = v === 0;
                this._indicate(v === 0 ? 'mute' : 'volume', Math.round(v * 100) + '%');
            }
        });
        screen.addEventListener('pointercancel', e => { pts.delete(e.pointerId); if (!pts.size) reset(); });
        screen.addEventListener('pointerup', e => {
            pts.delete(e.pointerId);
            if (pts.size) return;
            const was = gestured;
            reset();
            if (was || skip(e)) return;
            if (!this.ui.menu.hidden) return this.closeMenu();
            const now = Date.now();
            if (now - lastTap < 300) {
                clearTimeout(tapTimer); lastTap = 0;
                if (e.pointerType === 'mouse') return this.onDoubleTap && this.onDoubleTap();
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
    }

    /** Tarayıcı ekran parlaklığına erişemiyor; görüntü karartılır (%15–100). */
    setBrightness(v) {
        this.brightness = v;
        this.video.style.filter = v < 0.995 ? `brightness(${v.toFixed(2)})` : '';
    }

    _indicate(name, text) {
        const el = this.ui.indicator;
        clearTimeout(this._indT);
        el.replaceChildren(icon(name), h('span', {}, text));
        el.hidden = false;
    }
    _indicateEnd() {
        clearTimeout(this._indT);
        this._indT = setTimeout(() => { this.ui.indicator.hidden = true; }, 700);
    }

    showControls(on) {
        clearTimeout(this._hideT);
        this.root.classList.toggle('show-controls', on);
        // Menü açıkken kontroller kendiliğinden kapanmaz.
        if (on && this.playing() && this.ui.menu.hidden) this._hideT = setTimeout(() => this.root.classList.remove('show-controls'), 3000);
        if (!on && !this.ui.menu.hidden) this.closeMenu();
    }

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

    nudge(s) {
        if (this.mode !== 'html5') return;
        if (!this.canControl) return toast('Kumanda şu an yalnız ev sahibinde.', 'warn');
        this.seekTo(this.video.currentTime + s);
        this.root.dataset.nudge = s > 0 ? 'fwd' : 'back';
        clearTimeout(this._nudgeT);
        this._nudgeT = setTimeout(() => delete this.root.dataset.nudge, 500);
        this.showControls(true);
    }

    /** "Sonraki bölüm" düğmesi: fn verilirse son 5 dakikada görünür. */
    setNext(fn) {
        this.onNext = fn || null;
        this._updateTime();
    }

    _updateControls() {
        const playing = this.playing();
        this.root.classList.toggle('playing', playing);
        this.ui.play.replaceChildren(icon(playing ? 'pause' : 'play'));
        this.ui.play.setAttribute('aria-label', playing ? 'Duraklat' : 'Oynat');
        if (!playing) this.showControls(true); else this.showControls(this.root.classList.contains('show-controls'));
        this._updateTime();
    }

    _updateTime(preview) {
        const d = this.duration();
        const t = typeof preview === 'number' ? preview : this.time();
        // Uygulamadaki biçim: "00:32 · 53:52".
        this.ui.time.textContent = d ? `${clock(t)}  ·  ${clock(d)}` : clock(t);
        if (!this._scrubbing && d) this.ui.seek.value = String(Math.round(t / d * 1000));
        this.ui.seek.style.setProperty('--p', d ? (t / d * 100) + '%' : '0%');
        if (this.mode === 'html5' && d && this.video.buffered.length) {
            let end = 0;
            for (let i = 0; i < this.video.buffered.length; i++) if (this.video.buffered.start(i) <= t + 1) end = Math.max(end, this.video.buffered.end(i));
            this.ui.buffered.style.width = (end / d * 100) + '%';
        }
        this.ui.next.hidden = !(this.onNext && this.mode === 'html5' && d && d - t <= NEXT_WINDOW);
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
