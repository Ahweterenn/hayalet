// Sesli / görüntülü görüşme (WebRTC). Sunucu yalnız sinyali taşır; ses ve
// görüntü taraflar arasında şifreli (DTLS-SRTP) akar.
//
// Ölçülerek düzeltilmiş davranışlar (iki tarayıcı + sahte kamera ile):
//  * ICE adayları karşı tarafın açıklaması kurulmadan gelirse kuyrukta
//    bekler (eskiden atılıyordu; cevabı 0,8 sn gecikince 4/4 kurulamadı).
//  * İki taraf aynı anda ararsa socket kimliği küçük olan arayan kalır, öteki
//    kendiliğinden kabul eder (eskiden ikisi de reddediyordu, 4/4).
//  * Gelen arama varken aramak = kabul etmek.
//  * 'disconnected' hemen kapatmaz: 8 sn bekler, sonra ICE yeniden başlatılır
//    (yalnız arayan, en fazla 2 kez); görüşme sürerken gelen teklif mevcut
//    bağlantıyı yeniden müzakere eder, yenisini açmaz.
//  * Gönderim sınırları bağlantıdan SONRA uygulanır (öncesinde Chrome'da
//    sessizce başarısız oluyordu); ses öncelikli, görüntü hıza göre kademelenir.
//  * Ölü openrelay TURN listeden çıkarıldı (aktarma adayı vermiyordu).

import { $, h, icon, toast, store, avatar } from './ui.js';

const RTC_CONFIG = {
    iceServers: [
        { urls: ['stun:stun.l.google.com:19302', 'stun:stun1.l.google.com:19302'] },
        { urls: 'stun:stun.cloudflare.com:3478' },
    ],
    iceCandidatePoolSize: 10,
};
const MEDIA = {
    audio: { echoCancellation: true, noiseSuppression: true },
    video: { width: { max: 640 }, height: { max: 480 }, frameRate: { max: 20 }, facingMode: 'user' },
};
const RECONNECT_GRACE_MS = 8000;
const MAX_ICE_RESTARTS = 2;
const RING_TIMEOUT_MS = 30000;
const VIDEO_TIERS = [
    { minKbps: 600, maxBitrate: 400000, scale: 1, fps: 20 },
    { minKbps: 250, maxBitrate: 200000, scale: 1.5, fps: 15 },
    { minKbps: 0, maxBitrate: 90000, scale: 3, fps: 10 },
];
const BITRATE_CHECK_MS = 4000;

export class Call {
    constructor({ socket, roomId, me, dock, onState }) {
        this.socket = socket;
        this.roomId = roomId;
        this.me = me;                       // () => {name}
        this.dock = dock;
        this.onState = onState || (() => { });
        this.pc = null;
        this.local = null;
        this.remoteStream = null;
        this.isVideo = false;
        this.isCaller = false;
        this.outgoing = false;              // bizim isteğimiz cevap bekliyor
        this.incoming = null;               // {isVideo, callerName}
        this.pending = [];                  // erken gelen ICE adayları
        this.restarts = 0;
        this.reconnectTimer = null;
        this.bitrateTimer = null;
        this.ringTimer = null;
        this.timeout = null;
        this.muted = false;
        this.camOff = false;
        this.audioCtx = null; this.gain = null; this.src = null;
        this.volume = Number(store.get('oda.callVolume', '1')) || 1;
        this._bindSocket();
        this._bindDock();
    }

    get active() { return !!(this.pc || this.local); }
    state() {
        return this.active ? 'active' : this.outgoing ? 'calling' : this.incoming ? 'ringing' : 'idle';
    }

    // --- dış yüz ----------------------------------------------------------
    request(video = true) {
        if (this.outgoing) return this.cancel();
        if (this.incoming) return this.accept();       // karşı taraf zaten arıyor
        if (this.active) return toast('Zaten görüşmedesin.');
        this.isVideo = video;
        this.outgoing = true;
        this.socket.emit('webrtc-call-request', {
            roomId: this.roomId, isVideo: video, callerName: this.me().name,
            callerColor: this.me().color, callerId: this.socket.id,
        });
        this.timeout = setTimeout(() => {
            if (!this.outgoing) return;
            this.socket.emit('webrtc-call-reject', { roomId: this.roomId });
            this.end(false, 'Cevap gelmedi.');
        }, RING_TIMEOUT_MS);
        this.onState();
    }

    cancel() {
        this.socket.emit('webrtc-call-reject', { roomId: this.roomId });
        this.end(false, 'Arama iptal edildi.');
    }

    async accept() {
        const isVideo = this.incoming ? this.incoming.isVideo : this.isVideo;
        this._stopRing();
        this.incoming = null;
        try {
            await this._openMedia(isVideo);
            this.socket.emit('webrtc-call-accept', { roomId: this.roomId });
            toast('Bağlanıyor…');
        } catch (_) {
            toast('Mikrofon/kamera izni verilmedi.', 'err');
            this.socket.emit('webrtc-call-reject', { roomId: this.roomId });
            this.end(false, '');
        }
        this.onState();
    }

    reject() {
        this._stopRing();
        this.incoming = null;
        this.socket.emit('webrtc-call-reject', { roomId: this.roomId });
        this.onState();
    }

    end(emit = true, message = 'Görüşme bitti.') {
        clearTimeout(this.timeout); this.timeout = null;
        this._stopRing();
        this.outgoing = false; this.incoming = null; this.isCaller = false;
        this.pending = []; this.restarts = 0;
        clearTimeout(this.reconnectTimer); this.reconnectTimer = null;
        clearInterval(this.bitrateTimer); this.bitrateTimer = null;
        if (this.pc) { this.pc.close(); this.pc = null; }
        if (this.local) { this.local.getTracks().forEach(t => t.stop()); this.local = null; }
        this.remoteStream = null;
        if (this.src) { try { this.src.disconnect(); } catch (_) { } this.src = null; }
        $('.call-remote', this.dock).srcObject = null;
        $('.call-local', this.dock).srcObject = null;
        this.dock.hidden = true;
        this.muted = false; this.camOff = false;
        if (emit) this.socket.emit('webrtc-hangup', { roomId: this.roomId });
        if (message) toast(message);
        this.onState();
    }

    toggleMute() {
        if (!this.local) return;
        this.muted = !this.muted;
        this.local.getAudioTracks().forEach(t => { t.enabled = !this.muted; });
        this._renderDock();
    }

    toggleCam() {
        if (!this.local) return;
        this.camOff = !this.camOff;
        this.local.getVideoTracks().forEach(t => { t.enabled = !this.camOff; });
        this._renderDock();
    }

    setVolume(v) {
        this.volume = Math.max(0, Math.min(2, Number(v) || 0));
        store.set('oda.callVolume', String(this.volume));
        this._applyVolume();
    }

    // --- medya ------------------------------------------------------------
    async _openMedia(isVideo) {
        this.local = await navigator.mediaDevices.getUserMedia(
            isVideo ? MEDIA : { audio: MEDIA.audio, video: false });
        this.isVideo = isVideo;
        const lv = $('.call-local', this.dock);
        lv.srcObject = isVideo ? this.local : null;
        this.dock.hidden = false;
        this.dock.classList.toggle('audio-only', !isVideo);
        this._renderDock();
    }

    _newPeer() {
        const pc = new RTCPeerConnection(RTC_CONFIG);
        this.local.getTracks().forEach(t => pc.addTrack(t, this.local));
        pc.onicecandidate = e => { if (e.candidate) this.socket.emit('webrtc-ice-candidate', { roomId: this.roomId, candidate: e.candidate }); };
        pc.ontrack = e => {
            this.remoteStream = e.streams[0];
            const rv = $('.call-remote', this.dock);
            rv.srcObject = this.remoteStream;
            this.dock.classList.toggle('no-remote-video', !this.remoteStream.getVideoTracks().length);
            if (this.src) { try { this.src.disconnect(); } catch (_) { } this.src = null; }
            this._applyVolume();
        };
        pc.onconnectionstatechange = () => this._connState(pc);
        return pc;
    }

    async _startAsCaller() {
        clearTimeout(this.timeout); this.timeout = null;
        this.outgoing = false;
        this.isCaller = true;
        // Bu arama için karşıdan henüz aday gelemez; kalanlar eski görüşmeden.
        this.pending = [];
        try { await this._openMedia(this.isVideo); }
        catch (_) { toast('Mikrofon/kamera izni verilmedi.', 'err'); return this.end(true, ''); }
        const pc = this.pc = this._newPeer();
        pc.onnegotiationneeded = async () => {
            try {
                await pc.setLocalDescription(await pc.createOffer());
                this.socket.emit('webrtc-offer', { roomId: this.roomId, offer: pc.localDescription });
            } catch (err) { console.warn('[görüşme] teklif oluşturulamadı', err); }
        };
        this.onState();
    }

    async _onOffer(offer) {
        if (this.pc && this.local) {
            // Görüşme sürerken gelen teklif = ICE yeniden başlatma.
            try {
                await this.pc.setRemoteDescription(new RTCSessionDescription(offer));
                await this._flush();
                await this.pc.setLocalDescription(await this.pc.createAnswer());
                this.socket.emit('webrtc-answer', { roomId: this.roomId, answer: this.pc.localDescription });
            } catch (err) { console.warn('[görüşme] yeniden müzakere başarısız', err); }
            return;
        }
        if (!this.local) {
            try { await this._openMedia(!!(offer.sdp && offer.sdp.includes('m=video'))); }
            catch (_) { return toast('Görüşme isteği var ama mikrofon izni yok.', 'err'); }
        }
        // Aranan taraf. Kuyruk SIFIRLANMAZ: arayanın adayları önceden gelmiş olabilir.
        this.isCaller = false;
        const pc = this.pc = this._newPeer();
        try {
            await pc.setRemoteDescription(new RTCSessionDescription(offer));
            await this._flush();
            await pc.setLocalDescription(await pc.createAnswer());
            this.socket.emit('webrtc-answer', { roomId: this.roomId, answer: pc.localDescription });
        } catch (err) {
            console.warn('[görüşme] teklif cevaplanamadı', err);
            if (pc === this.pc) this.end(true, 'Görüşme kurulamadı.');
        }
        this.onState();
    }

    _candidate(c) {
        if (!this.pc || !this.pc.remoteDescription) { this.pending.push(c); return; }
        this.pc.addIceCandidate(new RTCIceCandidate(c)).catch(err => console.warn('[görüşme] aday eklenemedi', err));
    }

    async _flush() {
        const list = this.pending; this.pending = [];
        for (const c of list) {
            try { await this.pc.addIceCandidate(new RTCIceCandidate(c)); }
            catch (err) { console.warn('[görüşme] bekleyen aday eklenemedi', err); }
        }
    }

    // --- bağlantı sağlığı ----------------------------------------------------
    _connState(pc) {
        if (pc !== this.pc) return;
        const st = pc.connectionState;
        this.dock.dataset.conn = st;
        if (st === 'connected') {
            clearTimeout(this.reconnectTimer); this.reconnectTimer = null;
            if (this.restarts > 0) toast('Bağlantı düzeldi.', 'ok');
            this.restarts = 0;
            this._watchBitrate(pc);
        } else if (st === 'disconnected') {
            toast('Görüşme bağlantısı zayıf, toparlanıyor…', 'warn');
            if (!this.reconnectTimer) this._recheck(pc, RECONNECT_GRACE_MS);
        } else if (st === 'failed') {
            clearTimeout(this.reconnectTimer); this.reconnectTimer = null;
            this._restart(pc);
        }
    }

    _recheck(pc, ms) {
        clearTimeout(this.reconnectTimer);
        this.reconnectTimer = setTimeout(() => {
            this.reconnectTimer = null;
            if (pc === this.pc && pc.connectionState !== 'connected') this._restart(pc);
        }, ms);
    }

    _restart(pc) {
        if (this.restarts >= MAX_ICE_RESTARTS) return this.end(true, 'Görüşme bağlantısı kurulamadı. Ağ doğrudan bağlantıya izin vermiyor olabilir.');
        this.restarts++;
        if (this.isCaller) {
            if (pc.restartIce) pc.restartIce();
            else pc.createOffer({ iceRestart: true }).then(o => pc.setLocalDescription(o))
                .then(() => this.socket.emit('webrtc-offer', { roomId: this.roomId, offer: pc.localDescription }))
                .catch(err => console.warn('[görüşme] ICE yeniden başlatılamadı', err));
        }
        this._recheck(pc, RECONNECT_GRACE_MS * 2);
    }

    _limits(pc, tier) {
        pc.getSenders().forEach(s => {
            if (!s.track) return;
            const p = s.getParameters();
            if (!p.encodings || !p.encodings.length) return;
            const e = p.encodings[0];
            if (s.track.kind === 'audio') { e.priority = 'high'; e.networkPriority = 'high'; }
            else { e.maxBitrate = tier.maxBitrate; e.maxFramerate = tier.fps; e.scaleResolutionDownBy = tier.scale; e.priority = 'low'; }
            s.setParameters(p).catch(err => console.warn('[görüşme] gönderim sınırı uygulanamadı', err));
        });
    }

    _watchBitrate(pc) {
        clearInterval(this.bitrateTimer);
        let tier = 0;
        this._limits(pc, VIDEO_TIERS[0]);
        this.bitrateTimer = setInterval(async () => {
            if (pc !== this.pc || pc.connectionState !== 'connected') return;
            let kbps = null;
            try {
                (await pc.getStats()).forEach(r => {
                    if (r.type === 'candidate-pair' && r.nominated && r.state === 'succeeded' && r.availableOutgoingBitrate)
                        kbps = r.availableOutgoingBitrate / 1000;
                });
            } catch (_) { return; }
            if (kbps == null) return;
            const next = VIDEO_TIERS.findIndex(t => kbps >= t.minKbps);
            const up = next < tier && kbps >= VIDEO_TIERS[next].minKbps * 1.5;
            if (next > tier || up) { tier = next; this._limits(pc, VIDEO_TIERS[tier]); }
        }, BITRATE_CHECK_MS);
    }

    // --- ses seviyesi (%200'e kadar) --------------------------------------------
    _applyVolume() {
        const rv = $('.call-remote', this.dock);
        if (this.volume <= 1 && !this.audioCtx) { rv.muted = false; rv.volume = this.volume; return; }
        try {
            if (!this.audioCtx) {
                this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                this.gain = this.audioCtx.createGain();
                this.gain.connect(this.audioCtx.destination);
            }
            if (this.remoteStream && !this.src && this.remoteStream.getAudioTracks().length) {
                this.src = this.audioCtx.createMediaStreamSource(this.remoteStream);
                this.src.connect(this.gain);
            }
            if (this.src) { rv.muted = true; this.gain.gain.value = this.volume; }
        } catch (_) { rv.volume = Math.min(this.volume, 1); }
    }

    // --- sinyal olayları -------------------------------------------------------
    _bindSocket() {
        const s = this.socket;
        s.on('webrtc-call-request', ({ isVideo, callerName, callerColor, callerId }) => {
            // İkimiz aynı anda aradık: aynı kuralı iki taraf da uygular.
            if (this.outgoing && !this.active && callerId && s.id) {
                if (s.id < callerId) return;
                clearTimeout(this.timeout); this.timeout = null;
                this.outgoing = false;
                this.incoming = { isVideo: !!isVideo, callerName };
                return this.accept();
            }
            if (this.active || this.outgoing || this.incoming) {
                return s.emit('webrtc-call-reject', { roomId: this.roomId });
            }
            this.incoming = { isVideo: !!isVideo, callerName: String(callerName || 'Birisi'),
                callerColor: /^#[0-9a-f]{6}$/i.test(callerColor || '') ? callerColor : '' };
            this._startRing();
            this.onState();
            setTimeout(() => { if (this.incoming && !this.active) this.reject(); }, RING_TIMEOUT_MS);
        });
        s.on('webrtc-call-accept', () => { if (this.outgoing) this._startAsCaller(); });
        s.on('webrtc-call-reject', () => {
            const wasOutgoing = this.outgoing;
            this._stopRing();
            this.incoming = null;
            if (wasOutgoing && !this.active) this.end(false, 'Karşı taraf şu an müsait değil.');
            else this.onState();
        });
        s.on('webrtc-offer', ({ offer }) => this._onOffer(offer));
        s.on('webrtc-answer', async ({ answer }) => {
            if (!this.pc) return;
            try { await this.pc.setRemoteDescription(new RTCSessionDescription(answer)); await this._flush(); }
            catch (err) { console.warn('[görüşme] cevap uygulanamadı', err); }
        });
        s.on('webrtc-ice-candidate', ({ candidate }) => { if (candidate) this._candidate(candidate); });
        s.on('webrtc-hangup', () => { if (this.active || this.outgoing || this.incoming) this.end(false, 'Karşı taraf görüşmeyi bitirdi.'); });
    }

    // --- zil -----------------------------------------------------------------
    _startRing() {
        this._stopRing();
        const ring = () => {
            try {
                const C = window.AudioContext || window.webkitAudioContext; if (!C) return;
                const ctx = new C(), g = ctx.createGain(), now = ctx.currentTime;
                g.connect(ctx.destination);
                g.gain.setValueAtTime(0, now); g.gain.linearRampToValueAtTime(0.09, now + 0.05);
                g.gain.setValueAtTime(0.09, now + 0.72); g.gain.linearRampToValueAtTime(0, now + 0.8);
                for (const f of [440, 480]) { const o = ctx.createOscillator(); o.frequency.value = f; o.connect(g); o.start(now); o.stop(now + 0.82); }
                setTimeout(() => ctx.close().catch(() => { }), 1000);
            } catch (_) { }
        };
        ring();
        this.ringTimer = setInterval(ring, 1600);
    }
    _stopRing() { clearInterval(this.ringTimer); this.ringTimer = null; }

    // --- yüzen görüşme kutusu --------------------------------------------------
    _bindDock() {
        const d = this.dock;
        d.addEventListener('click', e => {
            const b = e.target.closest('[data-call]'); if (!b) return;
            const a = b.dataset.call;
            if (a === 'mute') this.toggleMute();
            else if (a === 'cam') this.toggleCam();
            else if (a === 'end') this.end(true);
            else if (a === 'size') d.classList.toggle('big');
        });
        $('.call-vol', d).value = String(Math.round(this.volume * 100));
        $('.call-vol', d).addEventListener('input', e => this.setVolume(e.target.value / 100));
        // Sürükleme: kutu videonun üstünde istenen köşeye taşınabilsin.
        let drag = null;
        d.addEventListener('pointerdown', e => {
            if (e.target.closest('button, input')) return;
            const r = d.getBoundingClientRect(), p = d.offsetParent.getBoundingClientRect();
            drag = { dx: e.clientX - r.left, dy: e.clientY - r.top, p };
            d.setPointerCapture(e.pointerId); d.classList.add('dragging');
        });
        d.addEventListener('pointermove', e => {
            if (!drag) return;
            const x = Math.max(0, Math.min(e.clientX - drag.p.left - drag.dx, drag.p.width - d.offsetWidth));
            const y = Math.max(0, Math.min(e.clientY - drag.p.top - drag.dy, drag.p.height - d.offsetHeight));
            Object.assign(d.style, { left: x + 'px', top: y + 'px', right: 'auto', bottom: 'auto' });
        });
        const stop = () => { drag = null; d.classList.remove('dragging'); };
        d.addEventListener('pointerup', stop); d.addEventListener('pointercancel', stop);
    }

    _renderDock() {
        const d = this.dock;
        const set = (sel, ic, on, label) => { const b = $(sel, d); b.replaceChildren(icon(ic)); b.classList.toggle('off', on); b.setAttribute('aria-label', label); };
        set('[data-call=mute]', this.muted ? 'mic-off' : 'mic', this.muted, this.muted ? 'Mikrofonu aç' : 'Sessize al');
        set('[data-call=cam]', this.camOff ? 'cam-off' : 'cam', this.camOff, this.camOff ? 'Kamerayı aç' : 'Kamerayı kapat');
        $('[data-call=cam]', d).hidden = !this.isVideo;
        $('.call-local', d).hidden = !this.isVideo || this.camOff;
    }

    /** Gelen arama kartı (oda.js sheet içinde gösterir). */
    incomingCard() {
        const c = this.incoming; if (!c) return null;
        return h('div', { class: 'incoming' },
            avatar(c.callerName, c.callerColor, 'xl'),
            h('div', { class: 'incoming-title' }, c.callerName),
            h('div', { class: 'muted' }, c.isVideo ? 'seni görüntülü arıyor' : 'seni sesli arıyor'),
            h('div', { class: 'row gap' },
                h('button', { class: 'btn danger round', 'aria-label': 'Reddet', onclick: () => this.reject() }, icon('hangup')),
                h('button', { class: 'btn ok round', 'aria-label': 'Aç', onclick: () => this.accept() }, icon(c.isVideo ? 'cam' : 'phone'))));
    }
}
