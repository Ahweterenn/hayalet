/* =============================================
   PERDE — Room Client (room.js)
   YouTube + HLS + MP4 desteği
   ============================================= */

const params = new URLSearchParams(window.location.search);
// Oda kimligi artik sabit degil: telefon surumunde her oturum rastgele bir
// kimlik uretiyor (davet linkini bilmeyen giremesin). Eski davranis, parametre
// yoksa 'PERDE' olarak korunuyor.
const roomId = params.get('room') || 'PERDE';
let username = decodeURIComponent(params.get('username') || '');
const guestSuffix = Math.random().toString(36).slice(2, 6).toUpperCase();
const defaultGuestName = 'Misafir-' + guestSuffix;

if (!username || username.toLowerCase() === 'misafir' || username === 'null') {
    let input = prompt('🎭 Sahneye katılmak için adını gir:', '');
    input = input ? input.trim() : '';
    username = input || defaultGuestName;
    const newUrl = new URL(window.location);
    newUrl.searchParams.set('username', username);
    window.history.replaceState({}, '', newUrl);
}

// ── Proxy base ────────────────────────────────────────────────────────────────
function normalizeProxyBase(rawBase) {
    const fallback = '/api/proxy';
    if (!rawBase || typeof rawBase !== 'string') return fallback;
    const clean = rawBase.trim().replace(/\/+$/, '');
    if (!clean) return fallback;
    if (/^https?:\/\//i.test(clean)) {
        return /\/(api\/proxy|proxy)$/i.test(clean) ? clean : `${clean}/proxy`;
    }
    if (clean.startsWith('/')) {
        return /\/(api\/proxy|proxy)$/i.test(clean) ? clean : `${clean}/proxy`;
    }
    return fallback;
}

// Proxy tabanı her zaman local endpoint olmalı.
// Cloudflare/ngrok URL'leri restart sonrası değiştiği için istemcide cachelenirse CORS kırılır.
const proxyBaseRaw = '/api/proxy';
const PROXY_BASE = normalizeProxyBase(proxyBaseRaw);

if (params.get('proxyBase')) {
    const cleanUrl = new URL(window.location.href);
    cleanUrl.searchParams.delete('proxyBase');
    window.history.replaceState({}, '', cleanUrl);
}

function isAlreadyProxiedUrl(rawUrl) {
    const candidate = String(rawUrl || '');
    return candidate.includes('/api/proxy?url=') || candidate.includes('/proxy?url=');
}

function unwrapProxiedUrl(rawUrl) {
    const candidate = String(rawUrl || '');
    if (!isAlreadyProxiedUrl(candidate)) return candidate;
    try {
        const parsed = new URL(candidate, window.location.origin);
        const inner = parsed.searchParams.get('url');
        return inner ? inner : candidate;
    } catch (_) {
        return candidate;
    }
}

function buildProxyUrl(targetUrl, opts = {}) {
    const raw = String(unwrapProxiedUrl(targetUrl) || '');
    if (raw.startsWith('data:')) return raw;
    if (!/^https?:\/\//i.test(raw)) return raw;
    const baseUrl = new URL(PROXY_BASE, window.location.origin);

    baseUrl.search = '';
    baseUrl.searchParams.set('url', raw);
    baseUrl.searchParams.set('roomId', roomId);

    const refValue = opts.ref || (() => {
        try { return new URL(raw).origin; } catch (_) { return ''; }
    })();
    if (refValue) baseUrl.searchParams.set('ref', refValue);

    return `${baseUrl.pathname}?${baseUrl.searchParams.toString()}`;
}

// ── DOM ───────────────────────────────────────────────────────────────────────
const videoWrapperEl = document.getElementById('video-wrapper');
const videoEl = document.getElementById('video-player');
const fullscreenWrapperBtn = document.getElementById('fullscreen-wrapper-btn');

// ── Özel altyazı katmanı ──────────────────────────────────────────────────────
// Not: Bazı Chrome sürümlerinde video::cue kuralları ilk sayfa render'ından sonra
// (JS ile) değiştirildiğinde artık yeniden hesaplanmıyor; boyut/renk/arkaplan
// ayarları görünmez oluyor. Bu yüzden native ::cue yerine kendi katmanımızı
// kullanıyoruz; stil her zaman normal bir DOM elemanına uygulanır.
const subtitleOverlayEl = document.getElementById('subtitle-overlay');
let activeSubtitleTextTrack = null;
let subStyleSize = '4.2vh';
let subStyleColor = '#ffffff';
let subStyleBg = 'rgba(0,0,0,0)';

function decodeVttEntities(str) {
    return str
        .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
        .replace(/&nbsp;/g, ' ').replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'").replace(/&amp;/g, '&');
}

function vttCueTextToLines(rawText) {
    // WebVTT cue metni <c>, <i>, <b>, <v Ad> gibi etiketler içerebilir; sadece düz metni al
    const withoutTags = String(rawText || '').replace(/<[^>]*>/g, '');
    return decodeVttEntities(withoutTags).split('\n').filter((l) => l.length > 0);
}

function renderSubtitleOverlay(activeCues) {
    if (!subtitleOverlayEl) return;
    if (!activeCues || activeCues.length === 0) {
        subtitleOverlayEl.innerHTML = '';
        return;
    }
    const lines = [];
    for (let i = 0; i < activeCues.length; i++) {
        lines.push(...vttCueTextToLines(activeCues[i].text));
    }
    subtitleOverlayEl.innerHTML = lines
        .map((l) => `<span class="subtitle-line" style="background-color:${subStyleBg}">${escHtml(l)}</span>`)
        .join('');
}

function updateSubtitleOverlayFromTrack(track) {
    if (!track || activeSubtitleTextTrack !== track) return;
    renderSubtitleOverlay(track.activeCues);
}

function setActiveSubtitleTextTrack(track) {
    if (activeSubtitleTextTrack && activeSubtitleTextTrack !== track) {
        activeSubtitleTextTrack.oncuechange = null;
    }
    activeSubtitleTextTrack = track || null;
    if (activeSubtitleTextTrack) {
        activeSubtitleTextTrack.oncuechange = () => updateSubtitleOverlayFromTrack(activeSubtitleTextTrack);
        renderSubtitleOverlay(activeSubtitleTextTrack.activeCues);
    } else {
        renderSubtitleOverlay(null);
    }
}

function applySubtitleOverlayStyle(size, color, bg) {
    subStyleSize = size;
    subStyleColor = color;
    subStyleBg = bg;
    if (!subtitleOverlayEl) return;
    subtitleOverlayEl.style.fontSize = size;
    subtitleOverlayEl.style.color = color;
    // Zaten ekranda duran satırların arka planını da anında güncelle
    subtitleOverlayEl.querySelectorAll('.subtitle-line').forEach((el) => { el.style.backgroundColor = bg; });
}

function toggleWatchFullscreen() {
    if (document.fullscreenElement || document.webkitFullscreenElement) {
        if (document.exitFullscreen) {
            document.exitFullscreen().catch(()=>{});
        } else if (document.webkitExitFullscreen) {
            document.webkitExitFullscreen();
        }
        // Tam ekrandan çıkarken ekran kilidini kaldır (cihaz normal haline dönsün)
        if (screen.orientation && screen.orientation.unlock) {
            try { screen.orientation.unlock(); } catch (e) {}
        }
    } else {
        const req = videoWrapperEl.requestFullscreen || videoWrapperEl.webkitRequestFullscreen;
        if (req) {
            req.call(videoWrapperEl).then(() => {
                // Tam ekrana geçildiğinde, destekleyen cihazlarda telefonu yatay moda (landscape) zorla kilitle
                if (screen.orientation && screen.orientation.lock) {
                    screen.orientation.lock('landscape').catch(() => {
                        // Bazı tarayıcılar (mesela masaüstü veya ayarları bloklu olanlar) izin vermez, sessizce yoksay
                    });
                }
            }).catch((err) => {
                console.error('Fullscreen hatasi:', err);
                showToast('Tam ekran desteklenmiyor veya engellendi.');
            });
        } else {
            showToast('Tarayıcınız tam ekranı desteklemiyor.');
        }
    }
}

if (fullscreenWrapperBtn) {
    fullscreenWrapperBtn.addEventListener('click', toggleWatchFullscreen);
}
// Video ekranında çift tıklayınca tam ekrana al-çıkart (Native kontroller iptal edildi)
videoWrapperEl.addEventListener('dblclick', toggleWatchFullscreen);
const ytPlayerWrap = document.getElementById('yt-player-wrap');
const iframePlayer = document.getElementById('iframe-player');
const videoOverlay = document.getElementById('video-overlay');
const videoUrlInput = document.getElementById('video-url-input');
const loadVideoBtn = document.getElementById('load-video-btn');
const chatMessages = document.getElementById('chat-messages');
const chatInput = document.getElementById('chat-input');
const sendBtn = document.getElementById('send-btn');
const chatPanel = document.querySelector('.chat-panel');
const onlineCount = document.getElementById('online-count');
const chatUsersList = document.getElementById('chat-users-list');
const syncToast = document.getElementById('sync-toast');
const syncStatusEl = document.getElementById('sync-status');
const secondaryStatus = document.getElementById('secondary-status');
const externalAssist = document.getElementById('external-assist');
const externalAssistText = document.getElementById('external-assist-text');
const openExternalBtn = document.getElementById('open-external-btn');
const copyExternalBtn = document.getElementById('copy-external-btn');
const tunnelLinkDisplay = document.getElementById('tunnel-link-display');
const tunnelLinkCopyBtn = document.getElementById('tunnel-link-copy');
const playerSettingsBtn = document.getElementById('player-settings-btn');
const playerSettingsMenu = document.getElementById('player-settings-menu');
const closeSettingsMenu = document.getElementById('close-settings-menu');

function buildInviteShareMessage(url) {
    return url;
}

if (tunnelLinkCopyBtn) {
    tunnelLinkCopyBtn.addEventListener('click', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        const url = tunnelLinkCopyBtn.getAttribute('data-url') || tunnelLinkDisplay?.getAttribute('data-url');
        if (!url) return;
        navigator.clipboard.writeText(buildInviteShareMessage(url));
        showToast('Davet mesaji kopyalandi!');
    });
} else if (tunnelLinkDisplay) {
    tunnelLinkDisplay.addEventListener('click', () => {
        const url = tunnelLinkDisplay.getAttribute('data-url');
        if (!url) return;
        navigator.clipboard.writeText(buildInviteShareMessage(url));
        showToast('Davet mesaji kopyalandi!');
    });
}

if (playerSettingsBtn) {
    playerSettingsBtn.addEventListener('click', () => {
        const isHidden = playerSettingsMenu.classList.toggle('hidden');
        playerSettingsBtn.classList.toggle('active', !isHidden);
        playerSettingsBtn.setAttribute('aria-expanded', String(!isHidden));
    });
}

if (closeSettingsMenu) {
    closeSettingsMenu.addEventListener('click', () => {
        playerSettingsMenu.classList.add('hidden');
        playerSettingsBtn?.classList.remove('active');
        playerSettingsBtn?.setAttribute('aria-expanded', 'false');
    });
}

document.addEventListener('click', (ev) => {
    if (!playerSettingsMenu || !playerSettingsBtn) return;
    if (playerSettingsMenu.classList.contains('hidden')) return;
    const clickedInsideMenu = playerSettingsMenu.contains(ev.target);
    const clickedOnBtn = playerSettingsBtn.contains(ev.target);
    if (!clickedInsideMenu && !clickedOnBtn) {
        playerSettingsMenu.classList.add('hidden');
        playerSettingsBtn.classList.remove('active');
        playerSettingsBtn.setAttribute('aria-expanded', 'false');
    }
});

document.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Escape') return;
    if (!playerSettingsMenu || !playerSettingsBtn) return;
    playerSettingsMenu.classList.add('hidden');
    playerSettingsBtn.classList.remove('active');
    playerSettingsBtn.setAttribute('aria-expanded', 'false');
});

// function isCompactLandscapeViewport() is removed as tablet acts like desktop browser
function isCinemaFullscreenActive() {
    const fsEl = document.fullscreenElement || document.webkitFullscreenElement;
    if (!fsEl) return false;
    return fsEl === videoEl || fsEl === iframePlayer || fsEl === videoWrapperEl || ytPlayerWrap?.contains(fsEl);
}

function hasActiveCallSession() {
    return !!localStream || !!remoteVideo?.srcObject;
}

async function syncCallVisibilityForFullscreen() {
    let fullscreenOn = isCinemaFullscreenActive();

    // Sinema modunu devre dışı bıraktık. Tarayıcı tam ekran mantığı (desktop) her cihazda işlesin.
    // Tablette Chrome üzerinden açtıkları için tarayıcıda overlay (OBS stili) zaten çalışıyor.

    const uiCompactModeOn = fullscreenOn; // Direkt native fullscreen'e bağladık
    document.body.classList.toggle('room-fullscreen', uiCompactModeOn);

    // Eğer masaüstünde/geniş ekranda tam ekrana alırsak, voice-panel katman hatasına (Z-Index)
    // düşmesin diye DOM içinde video wrapper ile body arasında ufak bir taşıma yapılabilir.
    const voicePanel = document.querySelector('.voice-panel');
    const chatPanelEl = document.querySelector('.chat-panel');
    const actualFsEl = document.fullscreenElement || document.webkitFullscreenElement;
    const isActuallyFullscreen = actualFsEl != null;
    
    // Voice Panel Taşıma
    if (isActuallyFullscreen && hasActiveCallSession() && voicePanel) {
        if (!actualFsEl.contains(voicePanel)) {
            // Reparenting video pause yapar ve bazen stream buffer'ı kopar, oynatmayı geri başlatmak için durumlarını alalım
            const isLocalPlaying = localVideo && !localVideo.paused && !localVideo.ended;
            const isRemotePlaying = remoteVideo && !remoteVideo.paused && !remoteVideo.ended;
            const lStream = localVideo.srcObject;
            const rStream = remoteVideo.srcObject;

            // Fullscreen elementinin içine al ki native player'ın arkasında kalmasın
            try {
                actualFsEl.appendChild(voicePanel);
            } catch (err) {
                console.warn('[ROOM] voice-panel fullscreen elementine taşınamadı (iframe vs).', err);
            }

            restoreVoicePanelPosition(voicePanel);

            // Taşıma bittikten sonra Buffer yenilemesi yap (safari/bazı chromelar için şart)
            if (lStream) localVideo.srcObject = lStream;
            if (rStream) remoteVideo.srcObject = rStream;

            // Videoyu canlandır
            if (isLocalPlaying) localVideo.play().catch(console.error);
            if (isRemotePlaying) remoteVideo.play().catch(console.error);
        } else if (voicePanel.style.left) {
            // Ekran boyutu değiştiyse (ör. pencere yeniden boyutlandı) sürüklenmiş konum ekran dışına taşmasın
            const { left, top } = clampVoicePanelToViewport(parseFloat(voicePanel.style.left) || 0, parseFloat(voicePanel.style.top) || 0);
            voicePanel.style.left = left + 'px';
            voicePanel.style.top = top + 'px';
        }
    } else if (!isActuallyFullscreen && voicePanel && voicePanel.parentElement !== document.querySelector('.w2g-right')) {
        // Fullscreen'den çıkınca eski yerine geri koy
        const w2gRight = document.querySelector('.w2g-right');
        if(w2gRight) {
            const isLocalPlaying = localVideo && !localVideo.paused && !localVideo.ended;
            const isRemotePlaying = remoteVideo && !remoteVideo.paused && !remoteVideo.ended;
            const lStream = localVideo.srcObject;
            const rStream = remoteVideo.srcObject;

            w2gRight.prepend(voicePanel);
            // Sürükleyerek taşınan konumu (fixed pozisyon stilleri) normal düzene dönerken temizle,
            // yoksa tam ekrandan çıkınca panel eski kart yerleşiminde bozuk görünür.
            voicePanel.style.left = '';
            voicePanel.style.top = '';
            voicePanel.style.right = '';
            voicePanel.style.bottom = '';
            voicePanel.style.width = '';
            voicePanel.classList.remove('dragging', 'controls-visible', 'resizing');

            if (lStream) localVideo.srcObject = lStream;
            if (rStream) remoteVideo.srcObject = rStream;

            if (isLocalPlaying) localVideo.play().catch(console.error);
            if (isRemotePlaying) remoteVideo.play().catch(console.error);
        }
    }

    // Chat Panel Taşıma (OBS Stili Overlay İçin)
    if (isActuallyFullscreen && chatPanelEl) {
        if (!actualFsEl.contains(chatPanelEl)) {
            try {
                actualFsEl.appendChild(chatPanelEl);
            } catch (err) {
                console.warn('[ROOM] chat-panel fullscreen elementine taşınamadı.', err);
            }
        }
    } else if (!isActuallyFullscreen && chatPanelEl && chatPanelEl.parentElement !== document.querySelector('.w2g-right')) {
        const w2gRight = document.querySelector('.w2g-right');
        // w2gRight içinde voice-panel varsa onun sonrasına, yoksa en sonuna koy
        if(w2gRight) w2gRight.appendChild(chatPanelEl);
    }

    // Fullscreen reparenting işlemi tamamen tarayıcıya (OBS tarzı) bırakılıyor.
    // Mobil/tablet de olsa overlay'lerimiz DOM içinde normal bir şekilde çalışır.
}

document.addEventListener('fullscreenchange', syncCallVisibilityForFullscreen);
document.addEventListener('webkitfullscreenchange', syncCallVisibilityForFullscreen);
window.addEventListener('orientationchange', () => {
    setTimeout(syncCallVisibilityForFullscreen, 120);
});
window.addEventListener('resize', syncCallVisibilityForFullscreen);

// ── Durum ─────────────────────────────────────────────────────────────────────
let mode = 'none';
let isSyncing = false;
let hls = null;
let ytPlayer = null;
let ytReady = false;
let ytPendingAction = null;
let externalUrl = '';
let localBufferingReported = false;
let bufferingStartTimer = null;
let roomBufferingActive = false;
let resumeAfterRoomBuffering = false;
let pendingRemoteAction = null;
let isRoomLeader = false;
let syncHeartbeatTimer = null;
let lastLeaderName = '';
let ytStartedOnce = false;
let activeVideoLoadToken = 0;
let activeSubtitleSessionId = 0;
const manualSubtitleRegistry = new Map();
let selectedManualSubtitleId = null;

function triggerPrivateCoupleEffects(message, senderUsername) {
    const coupleEffectsApi = (typeof window !== 'undefined' && window.PerdeCoupleEffects && typeof window.PerdeCoupleEffects === 'object')
        ? window.PerdeCoupleEffects
        : null;

    if (!coupleEffectsApi) return;
    if (typeof coupleEffectsApi.getEffectProfileForMessage !== 'function') return;
    if (typeof coupleEffectsApi.runEffectProfile !== 'function') return;

    const profile = coupleEffectsApi.getEffectProfileForMessage(message, senderUsername);
    if (!profile) return;
    coupleEffectsApi.runEffectProfile(profile);
}

const ENABLE_ROOM_DEBUG = false; // Detaylı loglama ayarı

function roomDebug(eventName, details = {}, level = 'log') {
    if (!ENABLE_ROOM_DEBUG && level !== 'error' && level !== 'warn') return;

    const snapshot = {
        ts: new Date().toISOString(),
        roomId,
        mode,
        isSyncing,
        roomBufferingActive,
        resumeAfterRoomBuffering,
        ytReady,
        ytStartedOnce,
        hasHls: !!hls,
        currentTime: Number(getCurrentPlaybackTime() || 0).toFixed(3),
        paused: mode === 'html5' ? !!videoEl.paused : null,
        readyState: mode === 'html5' ? videoEl.readyState : null,
        networkState: mode === 'html5' ? videoEl.networkState : null,
        bufferedRanges: mode === 'html5' && videoEl.buffered ? videoEl.buffered.length : 0,
        selectedManualSubtitleId,
        externalUrl,
        ...details
    };

    const method = console[level] || console.log;
    method.call(console, `[ROOM-DEBUG] ${eventName}`, snapshot);
    try {
        window.__PERDE_ROOM_DEBUG__ = window.__PERDE_ROOM_DEBUG__ || [];
        window.__PERDE_ROOM_DEBUG__.push(snapshot);
        if (window.__PERDE_ROOM_DEBUG__.length > 500) window.__PERDE_ROOM_DEBUG__.shift();
    } catch (_) { }
}

function getManualTrackMetaList() {
    const tags = Array.from(videoEl.querySelectorAll('track[data-manual-id]'));
    return tags.map((tag) => {
        const id = tag.getAttribute('data-manual-id') || '';
        const fallbackMeta = manualSubtitleRegistry.get(id) || {};
        return {
            id,
            label: tag.label || fallbackMeta.label || 'Altyazı',
            tag,
            textTrack: tag.track || null
        };
    });
}

function setManualSubtitleMode(activeManualId) {
    selectedManualSubtitleId = activeManualId || null;
    roomDebug('manual subtitle mode set', { activeManualId: selectedManualSubtitleId }, 'info');

    // Önce HLS.js altyazısını kapat
    if (hls) {
        hls.subtitleTrack = -1;
    }

    // Tüm manual text track'leri kapat
    getManualTrackMetaList().forEach((meta) => {
        if (meta.textTrack) {
            try { meta.textTrack.mode = 'disabled'; } catch (_) {}
        }
    });

    if (!activeManualId) {
        setActiveSubtitleTextTrack(null);
        return;
    }

    console.log('[DEBUG] setManualSubtitleMode tetiklendi. Hedef ID:', activeManualId);

    // Seçili track'i aç — 3 kez dene (HLS.js gecikmeli reset yapabilir)
    // 'hidden' modu: cue'lar ayrıştırılır ve oncuechange tetiklenir, ama native
    // görüntüleme (::cue) yapılmaz — kendi altyazı katmanımızı biz besleriz.
    const tryShow = (attempt) => {
        let success = false;
        getManualTrackMetaList().forEach((meta) => {
            if (meta.id === activeManualId && meta.textTrack) {
                meta.textTrack.mode = 'hidden';
                setActiveSubtitleTextTrack(meta.textTrack);
                success = true;
                console.log(`[DEBUG] Track 'hidden' yapıldı (deneme ${attempt}):`, meta.label);

                // Track içi cue'lar kontrol ediliyor mu?
                if (meta.textTrack.cues) {
                    console.log(`[DEBUG] Track cue sayısı: ${meta.textTrack.cues.length}`);
                }
            }
        });
        if (attempt < 3 && !success) setTimeout(() => tryShow(attempt + 1), 300);
    };
    tryShow(0);
}

// HLS.js altyazısını seçtikten sonra kendi text track'ini bulup 'hidden' yapar
// ve özel altyazı katmanına bağlar (native ::cue yerine).
function bindActiveHlsSubtitleTrack() {
    setTimeout(() => {
        let found = null;
        for (let i = 0; i < videoEl.textTracks.length; i++) {
            const t = videoEl.textTracks[i];
            if (t.mode === 'showing') { found = t; break; }
        }
        if (found) {
            found.mode = 'hidden';
            setActiveSubtitleTextTrack(found);
        }
    }, 60);
}

function clearManualSubtitleState() {
    roomDebug('manual subtitle state cleared');
    activeSubtitleSessionId += 1;
    if (videoEl.textTracks && videoEl.textTracks.length) {
        for (let i = 0; i < videoEl.textTracks.length; i++) {
            try { videoEl.textTracks[i].mode = 'disabled'; } catch (_) { }
        }
    }
    videoEl.querySelectorAll('track').forEach((trackEl) => trackEl.remove());
    manualSubtitleRegistry.clear();
    selectedManualSubtitleId = null;
    setActiveSubtitleTextTrack(null);
}

function isForcedSubtitleLabelOrUrl(value) {
    const lower = String(value || '').toLowerCase();
    return lower.includes('forced') || lower.includes('zorunlu');
}

function scoreTurkishSubtitleCandidate(url, label) {
    const combined = `${label || ''} ${url || ''}`.toLowerCase();
    if (combined.startsWith('data:text/vtt')) return 300;
    if (isForcedSubtitleLabelOrUrl(combined)) return 40;

    const noQuery = combined.split('?')[0];
    const fileName = noQuery.split('/').pop() || '';
    if (/^(tr|tur)\.(vtt|srt|ass)$/i.test(fileName)) return 220;
    if (noQuery.endsWith('/tr.vtt') || noQuery.includes('/tr/')) return 200;
    if (combined.includes('turkish') || combined.includes('turkce') || combined.includes('türkçe')) return 180;
    return 150;
}

function sortSubtitleCandidates(items) {
    return [...items]
        .filter((item) => !isForcedSubtitleLabelOrUrl(`${item?.label || ''} ${item?.url || ''}`))
        .sort((a, b) => scoreTurkishSubtitleCandidate(b.url, b.label) - scoreTurkishSubtitleCandidate(a.url, a.label));
}

// ── YouTube ───────────────────────────────────────────────────────────────────
function loadYouTubeAPI() {
    if (window.YT && window.YT.Player) return;
    const tag = document.createElement('script');
    tag.src = 'https://www.youtube.com/iframe_api';
    document.getElementsByTagName('script')[0].parentNode.insertBefore(tag, document.getElementsByTagName('script')[0]);
}

window.onYouTubeIframeAPIReady = function () {
    ytReady = true;
    if (ytPendingAction) {
        const { url, time, playing, onReady } = ytPendingAction;
        ytPendingAction = null;
        createYTPlayer(url, time, playing, onReady);
    }
};

function extractYTVideoId(url) {
    const m = url.match(/(?:v=|\/embed\/|\/shorts\/|youtu\.be\/)([A-Za-z0-9_-]{11})/);
    return m ? m[1] : null;
}

function isYouTubeUrl(url) {
    return /youtube\.com|youtu\.be/.test(url || '');
}

function createYTPlayer(url, startTime = 0, autoplay = false, onReadyCb = null) {
    const videoId = extractYTVideoId(url);
    if (!videoId) { addSystemMessage('❌ YouTube video ID bulunamadı'); return; }

    roomDebug('youtube player create requested', { url, startTime, autoplay, videoId }, 'info');

    if (ytPlayer) { try { ytPlayer.destroy(); } catch (e) { } ytPlayer = null; }
    ytPlayerWrap.innerHTML = '<div id="yt-iframe-inner"></div>';
    ytStartedOnce = false;

    ytPlayer = new YT.Player('yt-iframe-inner', {
        width: '100%', height: '100%', videoId,
        playerVars: { autoplay: autoplay ? 1 : 0, start: Math.floor(startTime), controls: 1, rel: 0, modestbranding: 1, fs: 0 },
        events: {
            onReady: (e) => {
                roomDebug('youtube onReady', { startTime, autoplay }, 'info');
                if (autoplay) { e.target.seekTo(Math.floor(startTime), true); e.target.playVideo(); }
                setTimeout(() => {
                    if (typeof onReadyCb === 'function') onReadyCb();
                    if (autoplay) verifyYouTubePlaybackAndPrompt(0);
                }, 300);
            },
            onStateChange: (e) => {
                const t = e.target.getCurrentTime();
                roomDebug('youtube state change', { state: e.data, currentTime: t }, e.data === YT.PlayerState.ERROR ? 'error' : 'log');
                switch (e.data) {
                    case YT.PlayerState.PLAYING:
                        ytStartedOnce = true;
                        removeAutoplayOverlay();
                        emitLocalBufferingEnd(t);
                        if (!isSyncing) socket.emit('play', { roomId, currentTime: t });
                        break;
                    case YT.PlayerState.PAUSED:
                        emitLocalBufferingEnd(t);
                        if (!isSyncing) socket.emit('pause', { roomId, currentTime: t });
                        break;
                    case YT.PlayerState.BUFFERING:
                        if (!isSyncing && ytStartedOnce) scheduleLocalBufferingStart(t);
                        break;
                    case YT.PlayerState.ENDED:
                        emitLocalBufferingEnd(t);
                        break;
                }
            }
        }
    });
}

// ── Socket ────────────────────────────────────────────────────────────────────
// EV SAHİBİ ANAHTARINI SOCKET'E DE VER. Sunucu ev sahibini el sıkışmanın
// sorgu dizesinde `hostToken=` arayarak tanıyor (events.py), ama burada
// yalnız sayfanın adresinde duruyordu: socket'e hiç gitmiyordu, dolayısıyla
// `is_host` HİÇ doğru olmuyor ve liderlik "ilk giren" kuralına düşüyordu.
// Sonuç: odayı açan telefon lider olmuyor, sonradan giren bir misafir lider
// oluyor; ev sahibi ne oynatmayı senkronlayabiliyor ne de /seek gibi
// komutları kullanabiliyordu (telefonda ölçüldü).
const hostToken = params.get('hostToken') || '';
const socket = io({
    query: hostToken ? { hostToken } : {},
    transports: ['websocket', 'polling'],
    timeout: 20000,
    reconnection: true,
    reconnectionAttempts: Infinity,
    reconnectionDelay: 900,
    reconnectionDelayMax: 5000
});

function joinCurrentRoom() {
    // Anahtar hem el sıkışmada hem burada: yeniden bağlanmada sorgu dizesi
    // korunmazsa ev sahibi liderliğini kaybetmesin.
    socket.emit('join-room', { roomId, username, hostToken });
}

// ── Altyazı normalize yardımcısı ─────────────────────────────────────────────
// Eklentiden {url, label} objesi veya düz string gelebilir — ikisini de destekle
function normalizeSubtitle(item) {
    if (!item) return null;
    if (typeof item === 'string') return { url: item, label: null };
    if (typeof item === 'object' && item.url) return { url: item.url, label: item.label || null };
    return null;
}

// ── Socket olayları ───────────────────────────────────────────────────────────
function clearAndStopVideo(showOverlayMessage = true) {
    if (showOverlayMessage) {
        videoOverlay.innerHTML = `
            <div class="overlay-content">
                <div class="overlay-icon">🎬</div>
                <p>Video URL'si girmek için aşağıdaki kutuyu kullan</p>
                <div style="margin-top:15px;font-size:13px;color:#94a3b8;">
                    Veya <strong>Perde Admin Eklentisi</strong> ile otomatik yansıt
                </div>
            </div>`;
        videoOverlay.classList.remove('hidden');
    }
    
    // Geri planda çalmayı kesin olarak durdur
    mode = null;
    
    if (videoEl) {
        try { videoEl.pause(); } catch(e){}
        videoEl.removeAttribute('src');
        videoEl.load(); 
        videoEl.style.display = 'none';
    }
    
    if (iframePlayer) {
        iframePlayer.src = 'about:blank';
        iframePlayer.style.display = 'none';
        setTimeout(() => { iframePlayer.removeAttribute('src'); }, 100);
    }
    
    if (ytPlayerWrap) {
        ytPlayerWrap.style.display = 'none';
    }
    if (ytPlayer) {
        try { 
            if (typeof ytPlayer.stopVideo === 'function') ytPlayer.stopVideo(); 
            if (typeof ytPlayer.destroy === 'function') ytPlayer.destroy();
        } catch (e) {}
        ytPlayer = null;
    }
    
    if (typeof hls !== 'undefined' && hls) {
        try { hls.destroy(); } catch(e){}
        hls = null;
    }
    
    if (typeof hideHlsControls === 'function') hideHlsControls();
    if (secondaryStatus) {
        secondaryStatus.innerHTML = '<span class="status-dot warning"></span> Herhangi Bir Bağlantı Yok';
    }
}

socket.on('room-state', ({ videoUrl, subtitles, currentTime, isPlaying, isBuffering, users }) => {
    roomDebug('socket room-state', {
        videoUrl,
        subtitleCount: Array.isArray(subtitles) ? subtitles.length : 0,
        currentTime,
        isPlaying,
        isBuffering,
        userCount: Array.isArray(users) ? users.length : 0
    }, 'info');
    updateUserList(users);
    if (videoUrl) {
        const orderedSubtitles = sortSubtitleCandidates((subtitles || []).map(normalizeSubtitle).filter(Boolean));
        loadVideo(videoUrl, {
            startTime: currentTime || 0,
            autoplay: isPlaying && !isBuffering,
            onReady: () => {
                if (orderedSubtitles.length > 0) {
                    roomDebug('room-state subtitles ordered', {
                        subtitles: orderedSubtitles.map((s) => ({ url: s.url, label: s.label }))
                    }, 'info');
                    orderedSubtitles.forEach(item => {
                        if (item) addSubtitleTrack(item.url, item.label);
                    });
                }
                videoOverlay.classList.add('hidden');
                if (isBuffering) {
                    roomBufferingActive = true;
                    resumeAfterRoomBuffering = isPlaying;
                    syncPause(currentTime);
                    showToast('⏳ Ağ sorunu nedeniyle oda beklemede');
                } else if (isPlaying) {
                    syncPlay(currentTime);
                } else {
                    syncSeek(currentTime);
                    removeAutoplayOverlay();
                }
            }
        });
    } else {
        clearAndStopVideo(true);
    }
});

socket.on('role-updated', ({ isLeader, leaderUsername }) => {
    isRoomLeader = !!isLeader;
    updateHeartbeatLoop();
    if (leaderUsername && leaderUsername !== lastLeaderName) {
        lastLeaderName = leaderUsername;
        addSystemMessage(`👑 Senkron lideri: ${leaderUsername}`);
    }
});

socket.on('kicked', ({ by }) => {
    alert(`Oda yöneticisi (${by}) tarafından odadan atıldınız.`);
    window.location.href = '/'; 
});

socket.on('user-joined', ({ username: u, users }) => { addSystemMessage(`👋 ${u} odaya katıldı`); updateUserList(users); });
socket.on('user-left', ({ username: u, users }) => { addSystemMessage(`👋 ${u} odadan ayrıldı`); updateUserList(users); });
socket.on('room-users', ({ users }) => { updateUserList(Array.isArray(users) ? users : []); });

socket.on('video-changed', ({ videoUrl, subtitles }) => {
    roomDebug('socket video-changed', {
        videoUrl,
        subtitleCount: Array.isArray(subtitles) ? subtitles.length : 0
    }, 'info');
    removeAutoplayOverlay();
    
    if (!videoUrl) {
        clearAndStopVideo(true);
        showToast('🗑️ Video temizlendi');
        addSystemMessage('🗑️ Video kapatıldı');
        return;
    }

    const orderedSubtitles = sortSubtitleCandidates((subtitles || []).map(normalizeSubtitle).filter(Boolean));
    loadVideo(videoUrl, {
        onReady: () => {
            if (orderedSubtitles.length > 0) {
                roomDebug('video-changed subtitles ordered', {
                    subtitles: orderedSubtitles.map((s) => ({ url: s.url, label: s.label }))
                }, 'info');
                orderedSubtitles.forEach(item => {
                    if (item) addSubtitleTrack(item.url, item.label);
                });
            }
        }
    });
    showToast('🎬 Video yüklendi');
    addSystemMessage('📺 Yeni video ve altyazılar yüklendi');
});

socket.on('play', ({ currentTime }) => {
    if (roomBufferingActive) {
        pendingRemoteAction = { type: 'play', currentTime };
        roomDebug('socket play queued (buffering)', { currentTime }, 'warn');
        return;
    }
    roomDebug('socket play', { currentTime }, 'info');
    syncPlay(currentTime);
    showToast('▶ Oynatıldı');
});

socket.on('pause', ({ currentTime }) => {
    if (roomBufferingActive) {
        pendingRemoteAction = { type: 'pause', currentTime };
        roomDebug('socket pause queued (buffering)', { currentTime }, 'warn');
        return;
    }
    roomDebug('socket pause', { currentTime }, 'info');
    syncPause(currentTime);
    showToast('⏸ Duraklatıldı');
});

socket.on('seek', ({ currentTime }) => {
    if (roomBufferingActive) {
        pendingRemoteAction = { type: 'seek', currentTime };
        roomDebug('socket seek queued (buffering)', { currentTime }, 'warn');
        return;
    }
    roomDebug('socket seek', { currentTime }, 'info');
    syncSeek(currentTime);
    showToast('⏩ Konum değiştirildi');
});

socket.on('room-buffering', ({ isBuffering, username: u, currentTime }) => {
    roomDebug('socket room-buffering', { isBuffering, actor: u, currentTime }, isBuffering ? 'warn' : 'info');
    if (isBuffering) {
        if (!roomBufferingActive) {
            roomBufferingActive = true;
            resumeAfterRoomBuffering = isCurrentlyPlaying();
            syncPause(typeof currentTime === 'number' ? currentTime : getCurrentPlaybackTime());
            showToast('⏳ Oda beklemede (bağlantı sorunu)');
            addSystemMessage(`⏸ ${u} tarafında bağlantı sorunu var, oda senkron için durduruldu.`);
        }
    } else {
        const wasBuffering = roomBufferingActive;
        const shouldResume = wasBuffering && resumeAfterRoomBuffering;
        roomBufferingActive = false;
        resumeAfterRoomBuffering = false;
        if (pendingRemoteAction) {
            const action = pendingRemoteAction;
            pendingRemoteAction = null;
            if (action.type === 'play') syncPlay(typeof action.currentTime === 'number' ? action.currentTime : getCurrentPlaybackTime());
            else if (action.type === 'pause') syncPause(typeof action.currentTime === 'number' ? action.currentTime : getCurrentPlaybackTime());
            else if (action.type === 'seek') syncSeek(typeof action.currentTime === 'number' ? action.currentTime : getCurrentPlaybackTime());
        } else if (shouldResume) {
            syncPlay(typeof currentTime === 'number' ? currentTime : getCurrentPlaybackTime());
        }
        if (wasBuffering) {
            showToast('✅ Oda tekrar senkron');
            addSystemMessage(`▶ ${u} bağlantısı düzeldi, senkron devam ediyor.`);
        }
    }
});

socket.on('sync-heartbeat', ({ currentTime, isPlaying }) => {
    roomDebug('socket sync-heartbeat', { currentTime, isPlaying }, 'log');
    if (isRoomLeader || mode === 'iframe' || roomBufferingActive || isSyncing) return;

    const targetTime = typeof currentTime === 'number' ? currentTime : getCurrentPlaybackTime();
    const myTime = getCurrentPlaybackTime();
    const drift = Math.abs(targetTime - myTime);
    const isBehind = myTime < targetTime;

    if (isPlaying) {
        if (!isCurrentlyPlaying()) { syncPlay(targetTime); return; }
        if (drift > 2.0) {
            syncSeek(targetTime);
            if (mode === 'html5') videoEl.playbackRate = 1.0;
        } else if (drift > 0.15) {
            if (mode === 'html5') {
                videoEl.playbackRate = isBehind ? 1.12 : 0.88;
            } else if (drift > 0.85) {
                syncSeek(targetTime);
            }
        } else {
            if (mode === 'html5' && videoEl.playbackRate !== 1.0) videoEl.playbackRate = 1.0;
        }
    } else {
        if (isCurrentlyPlaying()) {
            syncPause(targetTime);
            if (mode === 'html5') videoEl.playbackRate = 1.0;
            return;
        }
        if (drift > 0.5) syncSeek(targetTime);
    }
});

socket.on('chat-message', ({ username: u, message, time }) => {
    addChatMessage(u, message, time, u === username);
});

socket.on('tunnel-info', ({ url, inviteUrl }) => {
    const shareUrl = inviteUrl || (url ? url + '/invite.html' : null);
    if (shareUrl && tunnelLinkDisplay) {
        tunnelLinkDisplay.style.display = 'inline-flex';
        tunnelLinkDisplay.setAttribute('data-url', shareUrl);
        tunnelLinkCopyBtn?.setAttribute('data-url', shareUrl);
    } else if (tunnelLinkDisplay) {
        tunnelLinkDisplay.style.display = 'none';
    }

    // Proxy tabanı local sabit. Tunnel URL'si sadece davet amaçlı gösterilir.
});

socket.on('connect', () => { setSyncStatus(true); joinCurrentRoom(); });
socket.on('disconnect', () => { setSyncStatus(false); addSystemMessage('⚠️ Bağlantı kesildi...'); });
socket.on('connect_error', (err) => { setSyncStatus(false); console.error('[SOCKET connect_error]', err?.message || err); });
socket.on('reconnect', () => { setSyncStatus(true); addSystemMessage('✅ Yeniden bağlanıldı'); });

socket.on('clear-chat', () => {
    chatMessages.innerHTML = '';
});

socket.on('system-message', ({ message }) => {
    addSystemMessage(message);
});

// ── Sync helpers ──────────────────────────────────────────────────────────────
function syncPlay(t) {
    isSyncing = true;
    roomDebug('syncPlay called', { targetTime: t }, 'info');
    removeAutoplayOverlay();
    const time = typeof t === 'number' ? t : getCurrentPlaybackTime();
    if (mode === 'youtube' && ytPlayer && typeof ytPlayer.playVideo === 'function') {
        try { ytPlayer.seekTo(time, true); ytPlayer.playVideo(); } catch (e) { }
    } else if (mode === 'html5') {
        // Zaten o civarda isek zorla seek YAPMA: HLS/MSE akışlarında gereksiz bir seek
        // tarayıcıyı kısaca "waiting" durumuna sokup yanlış buffering algılamasını tetikliyor,
        // bu da tüm odayı gereksiz yere durdurup duraklatmadan sonra 2-3 kez üst üste
        // "otomatik geri duraklatma" yaşanmasına sebep oluyordu (gerçek internet sorunu yokken).
        if (Math.abs(videoEl.currentTime - time) > 0.75) videoEl.currentTime = time;
        const p = videoEl.play();
        if (p !== undefined) p.catch(err => { if (err.name === 'NotAllowedError') showAutoplayOverlay(); });
    }
    setTimeout(() => { isSyncing = false; }, 500);
}

function verifyYouTubePlaybackAndPrompt(attempt) {
    setTimeout(() => {
        if (!ytPlayer || mode !== 'youtube') return;
        const state = typeof ytPlayer.getPlayerState === 'function' ? ytPlayer.getPlayerState() : null;
        if (state === YT.PlayerState.PLAYING) { removeAutoplayOverlay(); return; }
        if (attempt < 4 && (state === YT.PlayerState.BUFFERING || state === YT.PlayerState.UNSTARTED || state === YT.PlayerState.CUED)) {
            verifyYouTubePlaybackAndPrompt(attempt + 1); return;
        }
        showAutoplayOverlay(true);
    }, 900);
}

function removeAutoplayOverlay() {
    const o = document.getElementById('autoplay-overlay');
    if (o) o.remove();
}

function showAutoplayOverlay(forcePlay = true) {
    let overlay = document.getElementById('autoplay-overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'autoplay-overlay';
        overlay.innerHTML = '<div style="background:rgba(15,23,45,0.94);padding:20px;border-radius:12px;text-align:center;border:1px solid var(--accent);cursor:pointer;box-shadow:0 4px 20px rgba(0,0,0,0.5);"><h3 style="margin:0;">▶ Oynatmayı Başlat</h3><p style="color:var(--text-muted);font-size:14px;margin-top:8px;">Tarayıcı otomatik oynatmayı engelledi.<br>Tıklayınca video ve ses açılır.</p></div>';
        overlay.style.cssText = 'position:absolute;inset:0;background:rgba(0,0,0,0.55);display:flex;align-items:center;justify-content:center;z-index:9999;';
        overlay.addEventListener('click', () => {
            const force = overlay.getAttribute('data-force-play') === '1';
            overlay.remove();
            if (force) {
                if (mode === 'html5') videoEl.play().catch(() => { });
                else if (mode === 'youtube' && ytPlayer) {
                    try { if (typeof ytPlayer.unMute === 'function') ytPlayer.unMute(); } catch (e) { }
                    ytPlayer.playVideo();
                }
            }
        });
        document.getElementById('video-wrapper').appendChild(overlay);
    }
    overlay.setAttribute('data-force-play', forcePlay ? '1' : '0');
}

function syncPause(t) {
    isSyncing = true;
    roomDebug('syncPause called', { targetTime: t }, 'info');
    const time = typeof t === 'number' ? t : getCurrentPlaybackTime();
    if (mode === 'youtube' && ytPlayer && typeof ytPlayer.pauseVideo === 'function') {
        try { ytPlayer.seekTo(time, true); ytPlayer.pauseVideo(); } catch (e) { }
    } else if (mode === 'html5') {
        if (Math.abs(videoEl.currentTime - time) > 0.75) videoEl.currentTime = time;
        videoEl.pause();
    }
    // videoEl.pause() tetiklediği 'pause' olayı asenkron ateşlenir (bir sonraki task'ta);
    // isSyncing'i hemen false yaparsak o olay dinleyicisi bunu "gerçek kullanıcı duraklatması"
    // sanıp sunucuya tekrar 'pause' gönderiyordu — syncPlay/syncSeek'teki gibi gecikmeli sıfırlıyoruz.
    setTimeout(() => { isSyncing = false; }, 300);
}

function syncSeek(t) {
    isSyncing = true;
    roomDebug('syncSeek called', { targetTime: t }, 'info');
    const time = typeof t === 'number' ? t : getCurrentPlaybackTime();
    if (mode === 'youtube' && ytPlayer && typeof ytPlayer.seekTo === 'function') {
        try { ytPlayer.seekTo(time, true); } catch (e) { }
    } else if (mode === 'html5') {
        videoEl.currentTime = time;
    }
    setTimeout(() => { isSyncing = false; }, 300);
}

function getCurrentPlaybackTime() {
    if (mode === 'youtube' && ytPlayer && typeof ytPlayer.getCurrentTime === 'function') return ytPlayer.getCurrentTime();
    if (mode === 'html5') return videoEl.currentTime;
    return 0;
}

function isCurrentlyPlaying() {
    if (mode === 'youtube' && ytPlayer && typeof ytPlayer.getPlayerState === 'function') return ytPlayer.getPlayerState() === YT.PlayerState.PLAYING;
    if (mode === 'html5') return !videoEl.paused && !videoEl.ended;
    return false;
}

function scheduleLocalBufferingStart(currentTime) {
    if (mode === 'iframe' || isSyncing || roomBufferingActive || localBufferingReported) return;
    if (!isCurrentlyPlaying()) return;
    clearTimeout(bufferingStartTimer);
    bufferingStartTimer = setTimeout(() => emitLocalBufferingStart(currentTime), 1500);
}

function emitLocalBufferingStart(currentTime) {
    if (mode === 'iframe' || isSyncing || roomBufferingActive || localBufferingReported) return;
    if (!isCurrentlyPlaying()) return;
    localBufferingReported = true;
    socket.emit('buffering-start', {
        roomId, username,
        currentTime: typeof currentTime === 'number' ? currentTime : getCurrentPlaybackTime()
    });
}

function emitLocalBufferingEnd(currentTime) {
    clearTimeout(bufferingStartTimer);
    if (!localBufferingReported) return;
    localBufferingReported = false;
    socket.emit('buffering-end', {
        roomId, username,
        currentTime: typeof currentTime === 'number' ? currentTime : getCurrentPlaybackTime()
    });
}

function updateHeartbeatLoop() {
    if (syncHeartbeatTimer) { clearInterval(syncHeartbeatTimer); syncHeartbeatTimer = null; }
    if (!isRoomLeader) return;
    syncHeartbeatTimer = setInterval(() => {
        if (mode === 'iframe' || roomBufferingActive || localBufferingReported) return;
        socket.emit('sync-heartbeat', { roomId, currentTime: getCurrentPlaybackTime(), isPlaying: isCurrentlyPlaying() });
    }, 1000);
}

window.addEventListener('beforeunload', () => {
    if (syncHeartbeatTimer) { clearInterval(syncHeartbeatTimer); syncHeartbeatTimer = null; }
});

// ── Video yükleme ─────────────────────────────────────────────────────────────
function loadVideo(url, opts) {
    if (!url) return;
    opts = opts || {};
    const loadToken = ++activeVideoLoadToken;
    const startTime = opts.startTime || 0;
    const autoplay = !!opts.autoplay;
    const onReady = opts.onReady || null;

    resetPlayerSettingsMenuState();
    clearTimeout(bufferingStartTimer);
    localBufferingReported = false;
    roomBufferingActive = false;
    resumeAfterRoomBuffering = false;
    pendingRemoteAction = null;
    ytStartedOnce = false;
    hideExternalAssist();
    externalUrl = '';
    videoOverlay.classList.add('hidden');

    roomDebug('loadVideo start', {
        url,
        startTime,
        autoplay,
        hasOnReady: typeof onReady === 'function',
        isYouTube: isYouTubeUrl(url),
        isM3U8: /m3u8/i.test(url),
        isMP4: /\.(mp4|webm|ogg)($|\?)/i.test(url)
    }, 'info');

    if (isYouTubeUrl(url)) {
        // ── YouTube ──
        mode = 'youtube';
        hideHlsControls();
        videoEl.pause(); videoEl.src = ''; videoEl.style.display = 'none';
        iframePlayer.style.display = 'none'; iframePlayer.src = '';
        if (hls) { hls.destroy(); hls = null; }
        ytPlayerWrap.style.display = 'block';
        secondaryStatus.innerHTML = '<span class="status-dot active"></span> YouTube';

        const wrappedOnReady = () => {
            if (loadToken !== activeVideoLoadToken) return;
            videoOverlay.classList.add('hidden');
            if (typeof onReady === 'function') onReady();
        };
        roomDebug('youtube branch entered', { url, startTime, autoplay }, 'info');
        if (ytReady) createYTPlayer(url, startTime, autoplay, wrappedOnReady);
        else { ytPendingAction = { url, time: startTime, playing: autoplay, onReady: wrappedOnReady }; loadYouTubeAPI(); }

    } else if (/\.(mp4|webm|m3u8|ogg)($|\?)/i.test(url) || /m3u8/i.test(url)) {
        // ── HTML5 / HLS ──
        mode = 'html5';
        ytPlayerWrap.style.display = 'none';
        iframePlayer.style.display = 'none'; iframePlayer.src = '';
        if (ytPlayer) { try { ytPlayer.destroy(); } catch (e) { } ytPlayer = null; }
        if (hls) { hls.destroy(); hls = null; }

        videoEl.style.display = 'block';
        videoEl.pause(); videoEl.src = '';
        clearManualSubtitleState();

        const isHLS = /\.m3u8|m3u8/i.test(url);
        const wrappedOnReady = () => {
            if (loadToken !== activeVideoLoadToken) return;
            videoOverlay.classList.add('hidden');
            if (typeof onReady === 'function') onReady();
            roomDebug('html5/hls ready callback', { url, autoplay, startTime }, 'info');
        };

        // Her zaman proxy'den geç — local izleme için de proxy çalışır (same-origin pass-through)
        const sourceUrl = buildProxyUrl(url);

        if (isHLS && Hls.isSupported()) {
            const isMobile = /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);
            const conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
            const saveData = !!(conn && conn.saveData);
            const isSlowNet = !!(conn && /2g|3g/.test(conn.effectiveType || ''));

            // Same-origin URL'lerde direkt deneme yapabiliriz, değilse hep proxy
            let canTryDirect = false;
            if (/^https?:\/\//i.test(url) && !isAlreadyProxiedUrl(url)) {
                try { canTryDirect = new URL(url, window.location.href).origin === window.location.origin; } catch (_) { }
            }

            // Tüm HLS isteklerini (segment + manifest) proxy'den geçiren loader
            const BaseLoader = Hls.DefaultConfig.loader;
            class ProxyAwareLoader extends BaseLoader {
                load(context, config, callbacks) {
                    try {
                        const rawUrl = context && context.url ? String(context.url) : '';
                        if (/^https?:\/\//i.test(rawUrl) && !isAlreadyProxiedUrl(rawUrl)) {
                            context.url = buildProxyUrl(rawUrl);
                        }
                    } catch (_) { }
                    return super.load(context, config, callbacks);
                }
            }

            const hlsConfig = () => ({
                enableWorker: true,
                loader: ProxyAwareLoader,
                startLevel: -1,
                renderTextTracksNatively: true,
                subtitleDisplay: true,
                maxBufferLength: (isMobile || isSlowNet) ? 8 : 24,
                maxMaxBufferLength: (isMobile || isSlowNet) ? 24 : 180,
                startFragPrefetch: true,
                manifestLoadingTimeOut: (isMobile || isSlowNet) ? 15000 : 40000,
                levelLoadingTimeOut: (isMobile || isSlowNet) ? 12000 : 35000,
                fragLoadingTimeOut: (isMobile || isSlowNet) ? 15000 : 40000,
                xhrSetup: (xhr) => {
                    xhr.withCredentials = false;
                    xhr.setRequestHeader('bypass-tunnel-reminder', 'true');
                    xhr.setRequestHeader('Bypass-Tunnel-Reminder', 'true');
                }
            });

            const initHls = (source, allowProxyFallback) => {
                if (hls) { hls.destroy(); hls = null; }
                let netRetry = 0, mediaRetry = 0;

                roomDebug('hls init', { source, allowProxyFallback, canTryDirect }, 'info');

                hls = new Hls(hlsConfig());
                hls.on(Hls.Events.MEDIA_ATTACHED, () => roomDebug('hls media attached', { source }, 'info'));
                hls.on(Hls.Events.MANIFEST_LOADING, (_, data) => roomDebug('hls manifest loading', { url: data?.url || source }, 'log'));
                hls.on(Hls.Events.MANIFEST_LOADED, (_, data) => roomDebug('hls manifest loaded', {
                    levels: data?.levels?.length || 0,
                    audioTracks: data?.audioTracks?.length || 0,
                    subtitles: data?.subtitles?.length || 0
                }, 'info'));
                hls.loadSource(source);
                hls.attachMedia(videoEl);

                hls.on(Hls.Events.MANIFEST_PARSED, () => {
                    roomDebug('hls manifest parsed', {
                        levels: hls.levels?.length || 0,
                        audioTracks: hls.audioTracks?.length || 0,
                        subtitleTracks: hls.subtitleTracks?.length || 0
                    }, 'info');
                    // Capping kaldırıldı, kullanıcı kendi kalitesini seçebilsin
                    wrappedOnReady();
                    buildHlsControls(hls);
                    setTimeout(() => buildHlsControls(hls), 300);
                });

                hls.on(Hls.Events.AUDIO_TRACKS_UPDATED, () => buildHlsControls(hls));
                hls.on(Hls.Events.SUBTITLE_TRACKS_UPDATED, () => { buildHlsControls(hls); setTimeout(() => buildHlsControls(hls), 100); });
                hls.on(Hls.Events.LEVEL_SWITCHED, (_, d) => { buildHlsControls(hls); });
                hls.on(Hls.Events.LEVEL_LOADED, (_, d) => roomDebug('hls level loaded', {
                    level: d?.level,
                    details: d?.details ? {
                        totalduration: d.details.totalduration,
                        fragments: d.details.fragments?.length || 0,
                        live: !!d.details.live
                    } : null
                }, 'log'));
                hls.on(Hls.Events.FRAG_LOADING, (_, d) => roomDebug('hls frag loading', {
                    sn: d?.frag?.sn,
                    level: d?.frag?.level,
                    url: d?.frag?.url
                }, 'log'));
                hls.on(Hls.Events.FRAG_LOADED, (_, d) => roomDebug('hls frag loaded', {
                    sn: d?.frag?.sn,
                    level: d?.frag?.level,
                    loadTime: d?.stats?.loading?.total || d?.stats?.tload || null
                }, 'log'));
                hls.on(Hls.Events.LEVEL_SWITCHED, (_, d) => roomDebug('hls level switched', { level: d?.level }, 'log'));

                hls.on(Hls.Events.ERROR, (_, d) => {
                    roomDebug('hls error', {
                        fatal: !!d?.fatal,
                        type: d?.type,
                        details: d?.details,
                        response: d?.response,
                        level: d?.level,
                        url: d?.url || null
                    }, d?.fatal ? 'error' : 'warn');
                    if (!d.fatal) return;
                    if (d.type === Hls.ErrorTypes.NETWORK_ERROR && netRetry < 4) {
                        netRetry++; 
                        try { hls.startLoad(); } catch (_) { }
                        return;
                    }
                    if (d.type === Hls.ErrorTypes.MEDIA_ERROR && mediaRetry < 2) {
                        mediaRetry++; 
                        try { hls.recoverMediaError(); } catch (_) { } 
                        return;
                    }
                    const levels = hls.levels || [];
                    if (levels.length > 0) {
                        hls.currentLevel = -1;
                        try { hls.startLoad(); } catch (_) { }
                        return;
                    }
                    if (allowProxyFallback && source !== sourceUrl) {
                        roomDebug('hls fallback to proxy', { source, sourceUrl }, 'warn');
                        addSystemMessage('⚡ Direkt m3u8 açılamadı, proxy moduna geçiliyor...');
                        initHls(sourceUrl, false); return;
                    }
                    emitLocalBufferingEnd(videoEl.currentTime);
                    // Üst kaynak 4xx/410 (özellikle dizipal/dplayer linkleri kısa ömürlü) ise
                    // kullanıcıya linkin süresinin dolduğunu ve yeniden yakalaması gerektiğini bildir.
                    const httpCode = d?.response?.code || d?.networkDetails?.status || 0;
                    if (httpCode === 410 || (httpCode >= 400 && httpCode < 500)) {
                        addSystemMessage('❌ Video linkinin süresi dolmuş. Eklentiden tekrar "YAKALA VE GÖNDER" yapın (link kısa ömürlüdür).');
                    } else {
                        addSystemMessage('❌ HLS akışı yüklenemedi (Kaynak/Proxy Hatası)');
                    }
                });
            };

            initHls(canTryDirect ? url : sourceUrl, canTryDirect);

        } else {
            hideHlsControls();
            videoEl.src = sourceUrl;
            roomDebug('html5 direct src assigned', { sourceUrl }, 'info');
            videoEl.addEventListener('loadedmetadata', wrappedOnReady, { once: true });
        }

        secondaryStatus.innerHTML = `<span class="status-dot active"></span> ${truncate(url, 40)}`;

    } else {
        // ── Scraper / iframe fallback ──
        mode = 'fetching';
        hideHlsControls();
        ytPlayerWrap.style.display = 'none';
        videoEl.style.display = 'none';
        iframePlayer.style.display = 'none'; iframePlayer.src = '';
        if (ytPlayer) { try { ytPlayer.destroy(); } catch (e) { } ytPlayer = null; }
        if (hls) { hls.destroy(); hls = null; }

        videoOverlay.innerHTML = `
            <div class="overlay-content">
                <div class="overlay-icon spinner">⏳</div>
                <p>Video linki çözümleniyor, lütfen bekleyin...<br/>
                <small style="color:#fbbf24;margin-top:6px;display:block;">Bu işlem 10-15 saniye sürebilir.</small></p>
            </div>`;
        videoOverlay.classList.remove('hidden');
        secondaryStatus.innerHTML = '<span class="status-dot warning"></span> Bağlantı Çözümleniyor...';
        addSystemMessage('⏳ Arka planda kaynak video aranıyor...');

        fetch('/api/extract-video', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url })
        })
            .then(r => r.json())
            .then(data => {
                roomDebug('scraper response', {
                    success: !!data.success,
                    extractedUrl: data.url || null,
                    error: data.error || null
                }, data.success ? 'info' : 'warn');
                if (data.success && data.url) {
                    if (loadToken !== activeVideoLoadToken) return;
                    addSystemMessage('✅ Alternatif video kaynağı bulundu!');
                    loadVideo(data.url, { autoplay, startTime, onReady });
                    socket.emit('set-video', { roomId, videoUrl: data.url });
                } else {
                    throw new Error(data.error || 'Video kaynağı bulunamadı.');
                }
            })
            .catch(err => {
                roomDebug('scraper failure', { message: err.message }, 'error');
                console.error('[Scraper Error]', err);
                videoOverlay.innerHTML = `
                <div class="overlay-content">
                    <div class="overlay-icon">🎬</div>
                    <p>Video URL'si girmek için aşağıdaki kutuyu kullan</p>
                    <div style="margin-top:15px;padding:10px;background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);border-radius:8px;font-size:13px;">
                        ⚠️ <strong>Sunucu Botu Başarısız:</strong> Bu site güvenlik nedeniyle taranamadı.<br>
                        Lütfen <strong>Perde Admin Eklentisini</strong> kullanarak videoyu odaya yansıt.
                    </div>
                </div>`;
                videoOverlay.classList.remove('hidden');
                addSystemMessage(`❌ Kaynak bulunamadı: ${err.message}`);

                mode = 'iframe';
                iframePlayer.style.display = 'block';
                iframePlayer.src = url;
                const wrappedOnReady = () => {
                    if (loadToken !== activeVideoLoadToken) return;
                    videoOverlay.classList.add('hidden');
                    if (typeof onReady === 'function') onReady();
                };
                iframePlayer.onload = wrappedOnReady;
                externalUrl = url;
                showExternalAssist(url);
                secondaryStatus.innerHTML = '<span class="status-dot active"></span> Web Sitesi IFrame';
            });
    }
}

// ── HLS kontrol paneli ────────────────────────────────────────────────────────
function buildHlsControls(hlsInstance) {
    const playerSettingsBtn = document.getElementById('player-settings-btn');
    const qualityGroup = document.getElementById('quality-group');
    const audioGroup = document.getElementById('audio-group');
    const subGroup = document.getElementById('subtitle-group');
    const qualityOpts = document.getElementById('quality-options');
    const audioOpts = document.getElementById('audio-options');
    const subOpts = document.getElementById('subtitle-options');

    if (!playerSettingsBtn) return;

    let hasAnyOption = false;

    // ── Kalite Seçimi ──
    const levels = hlsInstance.levels || [];
    if (levels.length > 1) {
        hasAnyOption = true;
        qualityOpts.innerHTML = '';

        // Auto
        const autoBtn = document.createElement('button');
        autoBtn.className = 'setting-option-btn';
        if (hlsInstance.autoLevelEnabled) autoBtn.classList.add('active');
        autoBtn.textContent = '🔄 Otomatik';
        autoBtn.onclick = () => { hlsInstance.currentLevel = -1; buildHlsControls(hlsInstance); };
        qualityOpts.appendChild(autoBtn);

        // Seviyeler
        levels.forEach((l, i) => {
            const btn = document.createElement('button');
            btn.className = 'setting-option-btn';
            if (!hlsInstance.autoLevelEnabled && hlsInstance.currentLevel === i) btn.classList.add('active');
            btn.textContent = `${l.height ? l.height + 'p' : 'Seviye ' + (i + 1)}${l.bitrate ? ' (' + Math.round(l.bitrate / 1000) + 'k)' : ''}`;
            btn.onclick = () => { hlsInstance.currentLevel = i; buildHlsControls(hlsInstance); };
            qualityOpts.appendChild(btn);
        });
        qualityGroup.style.display = 'flex';
    } else {
        qualityGroup.style.display = 'none';
    }

    // ── Ses Dili Seçimi ──
    const audioTracks = hlsInstance.audioTracks || [];
    if (audioTracks.length >= 1) {
        hasAnyOption = true;
        audioOpts.innerHTML = '';
        audioTracks.forEach((t, i) => {
            const btn = document.createElement('button');
            btn.className = 'setting-option-btn';
            if (hlsInstance.audioTrack === i) btn.classList.add('active');
            btn.textContent = formatTrackLabel(t, 'Ses', i);
            btn.onclick = () => { hlsInstance.audioTrack = i; buildHlsControls(hlsInstance); };
            audioOpts.appendChild(btn);
        });
        audioGroup.style.display = 'flex';
    } else {
        audioGroup.style.display = 'none';
    }

    // ── Altyazı Seçimi (HLS + Manuel) ──
    const manualSubs = getManualTrackMetaList();

    // Gerçekleşen subs array'i oluştur, [ { type: 'hls', index: 0, label: 'Korece' }, { type: 'manual', id: 'manual-1', label: 'Türkçe', textTrack: t } ]
    const combinedSubs = [];

    (hlsInstance.subtitleTracks || []).forEach((t, i) => {
        if (t.url || t.uri) {
            combinedSubs.push({ type: 'hls', index: i, label: formatTrackLabel(t, 'Altyazı', i) });
        }
    });

    manualSubs.forEach((manualSub) => {
        if (manualSub.id) {
            combinedSubs.push({ type: 'manual', id: manualSub.id, label: manualSub.label, track: manualSub.textTrack });
        }
    });

    if (combinedSubs.length > 0) {
        hasAnyOption = true;
        subOpts.innerHTML = '';

        let anyActive = false;

        // Her bir menü butonunu oluştur
        const buttons = [];
        const btnOff = document.createElement('button');
        btnOff.className = 'setting-option-btn';
        btnOff.textContent = '⛔ Kapalı';
        buttons.push({ btn: btnOff, isOff: true });
        subOpts.appendChild(btnOff);

        combinedSubs.forEach(sub => {
            const btn = document.createElement('button');
            btn.className = 'setting-option-btn';
            btn.textContent = sub.label;

            let isActive = false;
            if (sub.type === 'hls' && hlsInstance.subtitleTrack === sub.index) {
                isActive = true;
            } else if (sub.type === 'manual' && sub.track && (activeSubtitleTextTrack === sub.track || selectedManualSubtitleId === sub.id)) {
                isActive = true;
            }
            if (isActive) {
                btn.classList.add('active');
                anyActive = true;
            }

            btn.onclick = () => {
                // Hepsini kapa
                hlsInstance.subtitleTrack = -1;
                setManualSubtitleMode(null);

                // Seçileni aç
                if (sub.type === 'hls') {
                    hlsInstance.subtitleTrack = sub.index;
                    // HLS.js'e altyazı gösterimini zorla
                    if (typeof hlsInstance.subtitleDisplay !== 'undefined') {
                        hlsInstance.subtitleDisplay = true;
                    }
                    bindActiveHlsSubtitleTrack();
                } else if (sub.type === 'manual') {
                    setManualSubtitleMode(sub.id);
                }
                buildHlsControls(hlsInstance);
            };
            subOpts.appendChild(btn);
        });

        // Kapalı butonu on/off class
        if (!anyActive) {
            btnOff.classList.add('active');
        }
        btnOff.onclick = () => {
            hlsInstance.subtitleTrack = -1;
            setManualSubtitleMode(null);
            buildHlsControls(hlsInstance);
        };

        subGroup.style.display = 'flex';
    } else {
        subGroup.style.display = 'none';
    }

    if (hasAnyOption) {
        playerSettingsBtn.style.display = 'flex';
    } else {
        playerSettingsBtn.style.display = 'none';
        document.getElementById('player-settings-menu').classList.add('hidden');
    }
}

function formatTrackLabel(track, type, index) {
    const raw = (track.name || track.lang || `${type} ${index + 1}`);
    const lower = raw.toLowerCase().trim();
    const forcedSuffix = /forced|zorunlu/.test(lower) ? ' (Forced)' : '';
    if (/^tr([\W_]|$)/i.test(lower) || /turkce|türkçe|turkish/.test(lower)) return `🇹🇷 Türkçe${forcedSuffix}`;
    const map = {
        'tr': '🇹🇷 Türkçe', 'tur': '🇹🇷 Türkçe', 'turkish': '🇹🇷 Türkçe',
        'en': '🇺🇸 İngilizce', 'eng': '🇺🇸 İngilizce', 'english': '🇺🇸 İngilizce',
        'de': '🇩🇪 Almanca', 'ger': '🇩🇪 Almanca',
        'fr': '🇫🇷 Fransızca', 'fre': '🇫🇷 Fransızca',
        'es': '🇪🇸 İspanyolca', 'spa': '🇪🇸 İspanyolca',
        'ru': '🇷🇺 Rusça', 'rus': '🇷🇺 Rusça',
        'ar': '🇸🇦 Arapça', 'ara': '🇸🇦 Arapça',
        'ja': '🇯🇵 Japonca', 'jpn': '🇯🇵 Japonca',
        'ko': '🇰🇷 Korece', 'kor': '🇰🇷 Korece'
    };
    return map[lower] || raw;
}

function hideHlsControls() {
    const btn = document.getElementById('player-settings-btn');
    const menu = document.getElementById('player-settings-menu');
    if (btn) btn.style.display = 'none';
    if (btn) btn.classList.remove('active');
    if (menu) menu.classList.add('hidden');
}

function resetPlayerSettingsMenuState() {
    const qualityGroup = document.getElementById('quality-group');
    const audioGroup = document.getElementById('audio-group');
    const subtitleGroup = document.getElementById('subtitle-group');
    const qualityOpts = document.getElementById('quality-options');
    const audioOpts = document.getElementById('audio-options');
    const subtitleOpts = document.getElementById('subtitle-options');

    if (qualityOpts) qualityOpts.innerHTML = '';
    if (audioOpts) audioOpts.innerHTML = '';
    if (subtitleOpts) subtitleOpts.innerHTML = '';
    if (qualityGroup) qualityGroup.style.display = 'none';
    if (audioGroup) audioGroup.style.display = 'none';
    if (subtitleGroup) subtitleGroup.style.display = 'none';
    hideHlsControls();
}

// ── Altyazı ekleme ────────────────────────────────────────────────────────────
/**
 * url   : ham altyazı URL'si (string)
 * label : görüntülenecek isim (string | null)
 *
 * - Eklentiden gelen {url,label} objesi normalizeSubtitle() ile çözülür, buraya saf string gelir
 * - URL her zaman proxy'den geçirilir (CORS + IP-lock + Cookie aktarımı)
 * - Aynı URL ikinci kez eklenmez
 */
function addSubtitleTrack(url, label) {
    if (!videoEl || !url) return;
    const subtitleSessionId = activeSubtitleSessionId;

    if (isForcedSubtitleLabelOrUrl(`${label || ''} ${url}`)) {
        roomDebug('subtitle track skip forced', { url, label }, 'info');
        return;
    }

    // Label belirle
    if (!label) {
        try {
            const seg = new URL(url).pathname.split('/').pop() || '';
            label = seg.replace(/\.(vtt|srt|ass)$/i, '') || 'Altyazı';
        } catch (e) {
            label = `Altyazı ${videoEl.querySelectorAll('track').length + 1}`;
        }
    }

    // Türkçe ise standart label ver
    if (/^(tr|tur)([\W_]|$)/i.test(label.trim()) || /turkce|türkçe|turkish/i.test(`${label} ${url}`)) {
        label = isForcedSubtitleLabelOrUrl(`${label} ${url}`) ? '🇹🇷 Türkçe (Forced)' : '🇹🇷 Türkçe';
    }

    roomDebug('subtitle track add requested', {
        url,
        label,
        inferredForced: isForcedSubtitleLabelOrUrl(`${label} ${url}`)
    }, 'info');

    // Proxy URL'si oluştur (roomId ve ref dahil — headers alınabilsin)
    const proxiedUrl = buildProxyUrl(url);

    // Duplicate kontrolü
    const exists = Array.from(videoEl.querySelectorAll('track')).some(t =>
        t.src === proxiedUrl || t.src.includes(encodeURIComponent(url))
    );
    if (exists) return;

    const manualId = `manual-${Math.random().toString(36).substring(2, 9)}`;
    const track = document.createElement('track');
    track.kind = 'subtitles';
    track.label = label;
    track.srclang = /türkçe|turkish|^tr$/i.test(label) ? 'tr' : 'und';
    track.src = proxiedUrl;  // her zaman proxy'den
    track.setAttribute('data-manual', 'true');
    track.setAttribute('data-manual-id', manualId);
    track.default = false;
    videoEl.appendChild(track);

    manualSubtitleRegistry.set(manualId, {
        id: manualId,
        label,
        src: proxiedUrl
    });

    track.addEventListener('load', () => {
        if (subtitleSessionId !== activeSubtitleSessionId) return;
        console.log('[Subtitle DEBUG] Track YÜKLENDİ!', label, proxiedUrl);
        // Cueların başarıyla ayrıştırıldığını görelim
        if (track.track && track.track.cues) {
            const cueCount = track.track.cues.length;
            console.log(`[Subtitle DEBUG] Parsing başarılı. Total Cues: ${cueCount}`);
            roomDebug('subtitle track loaded', {
                label,
                proxiedUrl,
                cueCount,
                forced: isForcedSubtitleLabelOrUrl(`${label} ${proxiedUrl}`)
            }, cueCount > 0 ? 'info' : 'warn');
        } else {
            console.warn('[Subtitle DEBUG] Track yüklendi ama CUE bulunamadı!');
            roomDebug('subtitle track loaded without cues', {
                label,
                proxiedUrl
            }, 'warn');
        }
        buildHlsControls(hls || { levels: [], audioTracks: [], subtitleTracks: [] });
    });

    track.addEventListener('error', (e) => {
        if (subtitleSessionId !== activeSubtitleSessionId) return;
        console.error('[Subtitle DEBUG] Track YÜKLENEMEDİ! CORS veya Format hatası olabilir:', label, proxiedUrl, e);
        roomDebug('subtitle track error', {
            label,
            proxiedUrl,
            error: String(e && e.message ? e.message : e)
        }, 'error');
    });

    // Cue change debugger
    track.track.oncuechange = () => {
        if (subtitleSessionId !== activeSubtitleSessionId) return;
        const active = track.track.activeCues;
        if (active && active.length > 0) {
            console.log('[Subtitle DEBUG] Aktif Cue Saptandı:', active[0].text);
        }
    };

    // Seçiciyi yenile
    buildHlsControls(hls || { levels: [], audioTracks: [], subtitleTracks: [] });
}

// ── HTML5 video olayları ──────────────────────────────────────────────────────
videoEl.addEventListener('play', () => { if (isSyncing || mode !== 'html5') return; socket.emit('play', { roomId, currentTime: videoEl.currentTime }); });
videoEl.addEventListener('pause', () => { if (isSyncing || mode !== 'html5') return; socket.emit('pause', { roomId, currentTime: videoEl.currentTime }); });

let lastSeek = 0;
videoEl.addEventListener('seeked', () => {
    if (isSyncing || mode !== 'html5') return;
    const now = Date.now(); if (now - lastSeek < 300) return; lastSeek = now;
    socket.emit('seek', { roomId, currentTime: videoEl.currentTime });
});

videoEl.addEventListener('waiting', () => { if (mode === 'html5') scheduleLocalBufferingStart(videoEl.currentTime); });
videoEl.addEventListener('stalled', () => { if (mode === 'html5') scheduleLocalBufferingStart(videoEl.currentTime); });
videoEl.addEventListener('playing', () => { if (mode === 'html5') emitLocalBufferingEnd(videoEl.currentTime); });
videoEl.addEventListener('canplay', () => { if (mode === 'html5') emitLocalBufferingEnd(videoEl.currentTime); });
videoEl.addEventListener('error', () => {
    if (mode !== 'html5') return;
    const code = videoEl.error ? videoEl.error.code : 0;
    const reason = ({
        1: 'Yukleme iptal edildi',
        2: 'Ag hatasi',
        3: 'Cozme hatasi',
        4: 'Format desteklenmiyor'
    })[code] || 'Bilinmeyen oynatma hatasi';
    showToast('❌ Video acilamadi');
    addSystemMessage(`❌ Oynatma hatasi: ${reason}`);
    emitLocalBufferingEnd(videoEl.currentTime || 0);
});

videoEl.addEventListener('loadedmetadata', () => { if (mode === 'html5' && hls) buildHlsControls(hls); });
if (videoEl.textTracks) {
    videoEl.textTracks.addEventListener('addtrack', () => {
        if (mode === 'html5') buildHlsControls(hls || { levels: [], audioTracks: [], subtitleTracks: [] });
    });
}

// ── URL yükleme butonu ────────────────────────────────────────────────────────
function handleLoadVideo() {
    const url = videoUrlInput.value.trim();
    if (!url) { videoUrlInput.classList.add('shake'); setTimeout(() => videoUrlInput.classList.remove('shake'), 500); return; }
    if (isYouTubeUrl(url) || /\.(mp4|webm|m3u8|ogg)($|\?)/i.test(url) || /m3u8/i.test(url)) {
        loadVideo(url, {});
        socket.emit('set-video', { roomId, videoUrl: url });
        addSystemMessage('📺 Video yüklendi');
    } else {
        loadVideo(url, {});
    }
}

loadVideoBtn.addEventListener('click', handleLoadVideo);
videoUrlInput.addEventListener('keydown', e => { if (e.key === 'Enter') handleLoadVideo(); });

videoEl.addEventListener('loadedmetadata', () => roomDebug('video loadedmetadata', {
    duration: videoEl.duration,
    videoWidth: videoEl.videoWidth,
    videoHeight: videoEl.videoHeight,
    src: videoEl.currentSrc || videoEl.src
}, 'info'));
videoEl.addEventListener('loadeddata', () => roomDebug('video loadeddata', { src: videoEl.currentSrc || videoEl.src }, 'log'));
videoEl.addEventListener('canplay', () => roomDebug('video canplay', { src: videoEl.currentSrc || videoEl.src }, 'log'));
videoEl.addEventListener('playing', () => roomDebug('video playing', { currentTime: videoEl.currentTime }, 'info'));
videoEl.addEventListener('pause', () => roomDebug('video pause', { currentTime: videoEl.currentTime }, 'log'));
videoEl.addEventListener('waiting', () => roomDebug('video waiting', { currentTime: videoEl.currentTime }, 'warn'));
videoEl.addEventListener('stalled', () => roomDebug('video stalled', { currentTime: videoEl.currentTime }, 'warn'));
videoEl.addEventListener('error', () => roomDebug('video error', {
    code: videoEl.error ? videoEl.error.code : null,
    message: videoEl.error ? videoEl.error.message : null,
    src: videoEl.currentSrc || videoEl.src
}, 'error'));

// ── Chat komut sistemi ─────────────────────────────────────────────────────────
// Komutlar tek bir kayıt (registry) tablosunda tutulur; /help buradan otomatik
// üretilir, yetki kontrolü merkezîdir ve yeni komut eklemek tek bir obje eklemektir.

// sn -> "s:ss" veya "s:dd:ss" biçimi
function fmtTime(totalSec) {
    totalSec = Math.max(0, Math.floor(Number(totalSec) || 0));
    const h = Math.floor(totalSec / 3600);
    const m = Math.floor((totalSec % 3600) / 60);
    const s = totalSec % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

// "1:45" / "1:02:03" / "105" -> saniye (geçersizse NaN)
function parseTimeArg(str) {
    if (!str) return NaN;
    if (str.includes(':')) {
        const p = str.split(':').map((x) => parseInt(x, 10));
        if (p.some((n) => isNaN(n))) return NaN;
        return p.length === 3 ? p[0] * 3600 + p[1] * 60 + p[2] : p[0] * 60 + p[1];
    }
    return parseInt(str, 10);
}

// Videoyu belirtilen saniyeye götür + oynat, herkese yay (lider mantığı)
function commandSeekTo(seconds) {
    socket.emit('seek', { roomId, currentTime: seconds });
    socket.emit('play', { roomId, currentTime: seconds });
    syncSeek(seconds);
    syncPlay(seconds);
}

function setLocalVideoVolume(percent) {
    const v = Math.max(0, Math.min(100, percent));
    if (mode === 'youtube' && ytPlayer && typeof ytPlayer.setVolume === 'function') {
        ytPlayer.setVolume(v);
    } else if (videoEl) {
        videoEl.volume = v / 100;
    }
    if (typeof showToast === 'function') showToast(`🔊 Ses: %${v}`);
}

const CHAT_COMMANDS = [
    // ── Genel (herkes) ─────────────────────────────────────────────
    {
        names: ['help', '?', 'komut', 'komutlar', 'yardim'],
        usage: '/help [komut]',
        desc: 'Komut listesini ya da bir komutun detayını gösterir',
        run: (ctx) => showHelp(ctx.args[0]),
    },
    {
        names: ['clear', 'cls', 'temizlekendi'],
        usage: '/clear',
        desc: 'Sohbet ekranını sadece senin için temizler',
        run: () => { chatMessages.innerHTML = ''; reply('🧹 Sohbet ekranın temizlendi.'); },
    },
    {
        names: ['nick', 'isim', 'ad'],
        usage: '/nick [yeni isim]',
        desc: 'Sahnede görünen ismini değiştirir (maks. 25 karakter)',
        run: (ctx) => {
            const newNick = ctx.rest.trim();
            if (!newNick) return replyErr('Kullanım: /nick Yeniİsim');
            if (newNick.length > 25) return replyErr('❌ İsim çok uzun, en fazla 25 karakter.');
            username = newNick;
            const newUrl = new URL(window.location);
            newUrl.searchParams.set('username', username);
            window.history.replaceState({}, '', newUrl);
            socket.emit('change-nick', { roomId, newName: username });
            reply(`✅ İsmin "<b>${escHtml(newNick)}</b>" olarak güncellendi.`);
        },
    },
    {
        names: ['users', 'kim', 'who', 'online'],
        usage: '/users',
        desc: 'Odadaki kişileri listeler',
        run: () => {
            const users = Array.from(document.querySelectorAll('.user-chip')).map((el) => el.textContent);
            reply(`👥 <b>Odada ${users.length || 1} kişi:</b> ${users.map(escHtml).join(', ') || escHtml(username)}`);
        },
    },
    {
        names: ['load', 'yukle', 'ac'],
        usage: '/load [video linki]',
        desc: 'Verilen bağlantıyı yükler ve odadakilerle paylaşır',
        run: (ctx) => {
            const url = ctx.rest.trim();
            if (!url) return replyErr('Kullanım: /load https://.../video.m3u8');
            videoUrlInput.value = url;
            handleLoadVideo();
        },
    },
    {
        names: ['fs', 'fullscreen', 'tamekran'],
        usage: '/fs',
        desc: 'Tam ekranı açar/kapatır',
        run: () => { toggleWatchFullscreen(); reply('⛶ Tam ekran değiştirildi.'); },
    },
    {
        names: ['vol', 'ses', 'volume'],
        usage: '/vol [0-100]',
        desc: 'Videonun ses seviyesini ayarlar (sadece sende)',
        run: (ctx) => {
            const p = parseInt(ctx.args[0], 10);
            if (isNaN(p)) return replyErr('Kullanım: /vol 0-100');
            setLocalVideoVolume(p);
        },
    },
    {
        names: ['time', 'sure', 'konum'],
        usage: '/time',
        desc: 'Videonun bulunduğu anı gösterir',
        run: () => reply(`⏱️ Konum: <b>${fmtTime(getCurrentPlaybackTime())}</b>`),
    },

    // ── Yönetici / Oda lideri ──────────────────────────────────────
    {
        names: ['play', 'oynat'],
        admin: true,
        usage: '/play',
        desc: 'Videoyu herkeste oynatır',
        run: () => { const t = getCurrentPlaybackTime(); socket.emit('play', { roomId, currentTime: t }); syncPlay(t); reply('▶️ Video başlatıldı.'); },
    },
    {
        names: ['pause', 'dur', 'duraklat'],
        admin: true,
        usage: '/pause',
        desc: 'Videoyu herkeste duraklatır',
        run: () => { const t = getCurrentPlaybackTime(); socket.emit('pause', { roomId, currentTime: t }); syncPause(t); reply('⏸️ Video durduruldu.'); },
    },
    {
        names: ['seek', 'git', 'atla'],
        admin: true,
        usage: '/seek [dk:sn]',
        desc: 'Belirtilen ana atlar (örn: /seek 1:45 veya /seek 105)',
        run: (ctx) => {
            const total = parseTimeArg(ctx.args[0]);
            if (isNaN(total) || total < 0) return replyErr('❌ Geçersiz süre. Örn: /seek 1:45');
            commandSeekTo(total);
            reply(`⏩ ${fmtTime(total)} konumuna atlandı.`);
        },
    },
    {
        names: ['forward', 'ileri', 'ff'],
        admin: true,
        usage: '/forward [saniye=10]',
        desc: 'Videoyu belirtilen saniye kadar ileri sarar',
        run: (ctx) => {
            const delta = parseInt(ctx.args[0], 10) || 10;
            const t = Math.max(0, getCurrentPlaybackTime() + delta);
            commandSeekTo(t);
            reply(`⏩ ${delta}sn ileri (${fmtTime(t)}).`);
        },
    },
    {
        names: ['back', 'geri', 'rw'],
        admin: true,
        usage: '/back [saniye=10]',
        desc: 'Videoyu belirtilen saniye kadar geri sarar',
        run: (ctx) => {
            const delta = parseInt(ctx.args[0], 10) || 10;
            const t = Math.max(0, getCurrentPlaybackTime() - delta);
            commandSeekTo(t);
            reply(`⏪ ${delta}sn geri (${fmtTime(t)}).`);
        },
    },
    {
        names: ['sync', 'resync', 'senkron'],
        admin: true,
        usage: '/sync',
        desc: 'Herkesi senin bulunduğun ana zorla senkronlar',
        run: () => {
            const t = getCurrentPlaybackTime();
            commandSeekTo(t);
            reply(`🔄 Herkes ${fmtTime(t)} konumuna senkronlandı.`);
        },
    },
    {
        names: ['clearvideo', 'cv', 'kapat'],
        admin: true,
        usage: '/clearvideo',
        desc: 'Herkes için videoyu kapatır',
        run: () => socket.emit('admin-command', { roomId, command: 'clearvideo', args: [], username }),
    },
    {
        names: ['clearall', 'temizle'],
        admin: true,
        usage: '/clearall',
        desc: 'Herkesin sohbet ekranını temizler',
        run: () => socket.emit('admin-command', { roomId, command: 'clearall', args: [], username }),
    },
    {
        names: ['announce', 'duyur', 'a'],
        admin: true,
        usage: '/announce [mesaj]',
        desc: 'Odaya duyuru gönderir',
        run: (ctx) => {
            if (!ctx.rest.trim()) return replyErr('Kullanım: /announce Mesajınız');
            socket.emit('admin-command', { roomId, command: 'announce', args: [ctx.rest.trim()], username });
        },
    },
    {
        names: ['kick', 'at'],
        admin: true,
        usage: '/kick [isim]',
        desc: 'Bir kullanıcıyı odadan atar ve tekrar girişini engeller',
        run: (ctx) => {
            const target = ctx.rest.trim();
            if (!target) return replyErr('Kullanım: /kick KullaniciIsmi');
            socket.emit('admin-command', { roomId, command: 'kick', args: [target], username });
        },
    },
];

// İsim/alias -> komut
function findChatCommand(name) {
    const n = String(name || '').toLowerCase();
    return CHAT_COMMANDS.find((c) => c.names.includes(n)) || null;
}

// Yardımcı yanıtlar (registry handler'larından çağrılır)
function reply(html) { addPrivateSystemMessage(html); }
function replyErr(html) { addPrivateSystemMessage(html); }

// /help çıktısını registry'den otomatik üret
function showHelp(specific) {
    if (specific) {
        const c = findChatCommand(specific);
        if (!c) return replyErr(`Bilinmeyen komut: /${escHtml(specific)}`);
        reply(
            `<b>/${c.names[0]}</b>${c.admin ? ' 🛡️' : ''}<br/>` +
            `📄 ${escHtml(c.desc)}<br/>` +
            `⌨️ Kullanım: <b>${escHtml(c.usage)}</b><br/>` +
            (c.names.length > 1 ? `🔁 Kısaltmalar: ${c.names.slice(1).map((a) => '/' + escHtml(a)).join(', ')}` : '')
        );
        return;
    }
    const general = CHAT_COMMANDS.filter((c) => !c.admin);
    const admin = CHAT_COMMANDS.filter((c) => c.admin);
    const line = (c) => `<b>${escHtml(c.usage)}</b> — ${escHtml(c.desc)}`;
    reply(
        `💡 <b>Genel Komutlar</b><br/>${general.map(line).join('<br/>')}<br/><br/>` +
        `🛡️ <b>Yönetici Komutları</b> ${isRoomLeader ? '' : '<i>(oda lideri gerekir)</i>'}<br/>${admin.map(line).join('<br/>')}<br/><br/>` +
        `<i>İpucu: <b>/help [komut]</b> ile tek bir komutun detayını görebilirsin.</i>`
    );
}

function processChatCommand(msg) {
    if (!msg.startsWith('/')) return false;

    const body = msg.trim().slice(1);
    const spaceIdx = body.indexOf(' ');
    const name = (spaceIdx === -1 ? body : body.slice(0, spaceIdx)).toLowerCase();
    const rest = spaceIdx === -1 ? '' : body.slice(spaceIdx + 1).trim();
    const args = rest ? rest.split(/\s+/) : [];

    if (!name) {
        replyErr('Komutları görmek için <b>/help</b> yazın.');
        return true;
    }

    const cmd = findChatCommand(name);
    if (!cmd) {
        replyErr(`Bilinmeyen komut: <b>/${escHtml(name)}</b>. Komutlar için <b>/help</b> yazın.`);
        return true;
    }

    if (cmd.admin && !isRoomLeader) {
        replyErr('⛔ Bu komutu yalnızca <b>oda lideri</b> kullanabilir.');
        return true;
    }

    try {
        cmd.run({ args, rest, isAdmin: isRoomLeader });
    } catch (err) {
        console.error('[Komut hatası]', err);
        replyErr('⚠️ Komut çalıştırılırken bir hata oluştu.');
    }
    return true;
}

function sendMessage() {
    const msg = chatInput.value.trim();
    if (!msg) return;
    
    if (processChatCommand(msg)) {
        chatInput.value = '';
        return;
    }

    socket.emit('chat-message', { roomId, message: msg, username });
    chatInput.value = '';
}
sendBtn.addEventListener('click', sendMessage);
chatInput.addEventListener('keydown', e => { if (e.key === 'Enter') sendMessage(); });

// ── UI yardımcıları ───────────────────────────────────────────────────────────
function addPrivateSystemMessage(htmlHTML) {
    const div = document.createElement('div');
    div.className = 'chat-message system';
    div.style.color = '#94a3b8';
    div.style.background = 'rgba(255, 255, 255, 0.05)';
    div.style.padding = '8px';
    div.style.borderRadius = '6px';
    div.style.fontSize = '13px';
    div.style.margin = '4px 0';
    div.style.textAlign = 'left';
    div.innerHTML = htmlHTML;
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function addChatMessage(u, message, time, isOwn) {
    const div = document.createElement('div');
    div.className = `chat-msg${isOwn ? ' own-msg' : ''}`;
    const meta = document.createElement('div');
    meta.className = 'msg-meta';

    const usernameEl = document.createElement('span');
    usernameEl.className = 'msg-username';
    usernameEl.textContent = u;

    const timeEl = document.createElement('span');
    timeEl.textContent = time;

    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    bubble.textContent = message;

    meta.appendChild(usernameEl);
    meta.appendChild(timeEl);
    div.appendChild(meta);
    div.appendChild(bubble);
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    removeChatMessageAfterFadeOut(div);

    triggerPrivateCoupleEffects(message, u);
}

// Tam ekranda mesajlar CSS animasyonuyla soluklaşıp kayboluyor; animasyon bitince
// DOM'dan da kaldırıyoruz ki konteyner büyümesin ve kaydırma çubuğu geride kalmasın.
// (Tam ekran değilken bu animasyon zaten hiç çalışmadığı için normal sohbet geçmişi bozulmaz.)
function removeChatMessageAfterFadeOut(el) {
    el.addEventListener('animationend', (ev) => {
        if (ev.animationName === 'fadeOutChatMessage') el.remove();
    });
}

function addSystemMessage(text) {
    const showInChat = /bağlantı sorunu|senkron için durduruldu|bağlantısı düzeldi|bağlantı kesildi|yeniden bağlanıldı|sistem|temizlendi|kapatıldı|duyuru|kullanıcı/i.test(text || '');
    if (!showInChat) { console.info('[ROOM]', text); return; }
    const div = document.createElement('div');
    div.className = 'chat-message system';
    div.style.color = '#8b9bb4';
    div.style.fontStyle = 'italic';
    div.style.fontSize = '12px';
    div.style.margin = '4px 0';
    div.innerHTML = `<em>${text}</em>`;
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    removeChatMessageAfterFadeOut(div);
}

function updateUserList(users) {
    onlineCount.textContent = users.length;
    chatUsersList.innerHTML = users.map(u => `<span class="user-chip">${escHtml(u)}</span>`).join('');
}

let toastTimer = null;
function showToast(msg) {
    syncToast.textContent = msg;
    syncToast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => syncToast.classList.remove('show'), 2200);
}

function setSyncStatus(connected) {
    syncStatusEl.innerHTML = connected
        ? '<span class="status-dot active"></span> Bağlı'
        : '<span class="status-dot warning"></span> Bağlantı kesildi';
}

function showExternalAssist(url) {
    if (!externalAssist) return;
    externalAssist.style.display = 'flex';
    externalAssistText.textContent = `Harici site modu: ${truncate(url, 60)}`;
}

function hideExternalAssist() {
    if (!externalAssist) return;
    externalAssist.style.display = 'none';
}

function escHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function truncate(s, n) {
    return s.length > n ? s.slice(0, n) + '…' : s;
}

openExternalBtn?.addEventListener('click', () => { if (externalUrl) window.open(externalUrl, '_blank', 'noopener,noreferrer'); });
copyExternalBtn?.addEventListener('click', () => { if (externalUrl) { navigator.clipboard.writeText(externalUrl); showToast('🔗 Harici site linki kopyalandı'); } });

// ── Altyazı ayarları modalı ───────────────────────────────────────────────────
const subSettingsModal = document.getElementById('sub-settings-modal');
const openSubSettingsBtn = document.getElementById('open-sub-settings-btn');
const closeSubSettingsBtn = document.getElementById('close-sub-settings');
const saveSubSettingsBtn = document.getElementById('save-sub-settings');
const subSizeSelect = document.getElementById('sub-size-select');
const subColorSelect = document.getElementById('sub-color-select');
const subBgSelect = document.getElementById('sub-bg-select');

if (openSubSettingsBtn) openSubSettingsBtn.addEventListener('click', () => subSettingsModal.style.display = 'flex');
if (closeSubSettingsBtn) closeSubSettingsBtn.addEventListener('click', () => subSettingsModal.style.display = 'none');

function loadSubtitleSettings() {
    // Eski sürümlerde boyut yüzde (%) olarak saklanıyordu; ::cue içinde yüzde,
    // videonun yüksekliğine değil miras alınan font-size'a göre hesaplanıp
    // neredeyse etkisiz kalıyordu. Eski değerleri vh tabanlıya taşı.
    let size = localStorage.getItem('perde_sub_size') || '4.2vh';
    if (size.includes('%')) {
        size = '4.2vh';
        localStorage.setItem('perde_sub_size', size);
    }
    const color = localStorage.getItem('perde_sub_color') || '#ffffff';
    const bg = localStorage.getItem('perde_sub_bg') || 'rgba(0,0,0,0)';
    if (subSizeSelect) subSizeSelect.value = size;
    if (subColorSelect) subColorSelect.value = color;
    if (subBgSelect) subBgSelect.value = bg;
    applySubtitleOverlayStyle(size, color, bg);
}

if (saveSubSettingsBtn) {
    saveSubSettingsBtn.addEventListener('click', () => {
        const size = subSizeSelect.value, color = subColorSelect.value, bg = subBgSelect.value;
        localStorage.setItem('perde_sub_size', size);
        localStorage.setItem('perde_sub_color', color);
        localStorage.setItem('perde_sub_bg', bg);
        applySubtitleOverlayStyle(size, color, bg);
        subSettingsModal.style.display = 'none';
        showToast('🎨 Altyazı ayarları kaydedildi');
    });
}

loadSubtitleSettings();

// ── WebRTC Sesli/Görüntülü Görüşme ───────────────────────────────────────────
let localStream = null;
let peerConnection = null;
let isMuted = false;
let isCamOff = false;
let isWaitingForAccept = false;

const startCallBtn = document.getElementById('start-call-btn');
const startAudioBtn = document.getElementById('start-audio-btn');
const endCallBtn = document.getElementById('end-call-btn');
let isVideoCall = false;
const muteBtn = document.getElementById('mute-btn');
const camBtn = document.getElementById('cam-btn');
const localVideo = document.getElementById('local-video');
const remoteVideo = document.getElementById('remote-video');
const voiceVideos = document.getElementById('voice-videos');
const voiceControls = document.getElementById('voice-controls');

// ── Tam ekranda görüşme balonu: WhatsApp tarzı sürükle + dokununca kontrolleri göster ──
const voicePanelEl = document.querySelector('.voice-panel');
const voiceResizeHandleEl = document.getElementById('voice-resize-handle');
const VOICE_PANEL_POS_KEY = 'perde-voice-panel-pos';
const DRAG_THRESHOLD_PX = 6; // Fare için: bunun altındaki hareket "sürükleme" değil "dokunma" sayılır
const DRAG_THRESHOLD_TOUCH_PX = 10; // Parmakla dokunuş fareden daha kararsız olduğu için eşik biraz daha yüksek
const CONTROLS_AUTO_HIDE_MS = 3000;
const VOICE_PANEL_MIN_WIDTH = 130; // CSS'teki min-width ile ayni (kucuk balonda ikonlar sigmiyordu)

function voicePanelMaxWidth() {
    return Math.min(420, window.innerWidth * 0.7);
}

function clampVoicePanelToViewport(left, top) {
    const rect = voicePanelEl.getBoundingClientRect();
    const maxLeft = Math.max(8, window.innerWidth - rect.width - 8);
    const maxTop = Math.max(8, window.innerHeight - rect.height - 8);
    return { left: Math.min(Math.max(8, left), maxLeft), top: Math.min(Math.max(8, top), maxTop) };
}

function restoreVoicePanelPosition(panel) {
    try {
        const saved = JSON.parse(localStorage.getItem(VOICE_PANEL_POS_KEY) || 'null');
        if (saved && typeof saved.width === 'number') {
            const width = Math.min(Math.max(VOICE_PANEL_MIN_WIDTH, saved.width), voicePanelMaxWidth());
            panel.style.width = width + 'px';
        }
        if (saved && typeof saved.left === 'number' && typeof saved.top === 'number') {
            const { left, top } = clampVoicePanelToViewport(saved.left, saved.top);
            panel.style.left = left + 'px';
            panel.style.top = top + 'px';
            panel.style.right = 'auto';
            panel.style.bottom = 'auto';
        }
    } catch (err) { /* localStorage yoksa (gizli sekme vb) sessizce yoksay */ }
}

function saveVoicePanelState() {
    try {
        localStorage.setItem(VOICE_PANEL_POS_KEY, JSON.stringify({
            left: parseFloat(voicePanelEl.style.left) || 0,
            top: parseFloat(voicePanelEl.style.top) || 0,
            width: voicePanelEl.getBoundingClientRect().width
        }));
    } catch (err) { /* yoksay */ }
}

let controlsHideTimer = null;
function showVoiceControlsOverlay() {
    voicePanelEl.classList.add('controls-visible');
    clearTimeout(controlsHideTimer);
    controlsHideTimer = setTimeout(() => voicePanelEl.classList.remove('controls-visible'), CONTROLS_AUTO_HIDE_MS);
}
function hideVoiceControlsOverlay() {
    clearTimeout(controlsHideTimer);
    voicePanelEl.classList.remove('controls-visible');
}

(function enableVoicePanelDragAndTap() {
    if (!voicePanelEl) return;
    let pointerDown = false, dragging = false;
    let startX = 0, startY = 0, startLeft = 0, startTop = 0;
    let dragThreshold = DRAG_THRESHOLD_PX;

    voicePanelEl.addEventListener('pointerdown', (e) => {
        if (!document.body.classList.contains('room-fullscreen')) return;
        if (e.target.closest('.voice-controls')) return; // Kontrol çubuğundaki tıklamalara/kaydırıcıya karışma
        if (e.target.closest('.voice-resize-handle')) return; // Boyutlandırma tutamacı kendi mantığını yürütür
        const rect = voicePanelEl.getBoundingClientRect();
        pointerDown = true;
        dragging = false;
        dragThreshold = e.pointerType === 'touch' ? DRAG_THRESHOLD_TOUCH_PX : DRAG_THRESHOLD_PX;
        startX = e.clientX; startY = e.clientY;
        startLeft = rect.left; startTop = rect.top;
        voicePanelEl.setPointerCapture(e.pointerId);
        e.preventDefault();
    });

    voicePanelEl.addEventListener('pointermove', (e) => {
        if (!pointerDown) return;
        const dx = e.clientX - startX, dy = e.clientY - startY;
        if (!dragging && Math.hypot(dx, dy) > dragThreshold) {
            dragging = true;
            voicePanelEl.classList.add('dragging');
            hideVoiceControlsOverlay(); // taşırken kontrol çubuğu araya girmesin
        }
        if (!dragging) return;
        const { left, top } = clampVoicePanelToViewport(startLeft + dx, startTop + dy);
        voicePanelEl.style.left = left + 'px';
        voicePanelEl.style.top = top + 'px';
        voicePanelEl.style.right = 'auto';
        voicePanelEl.style.bottom = 'auto';
    });

    function stopPointer() {
        if (!pointerDown) return;
        pointerDown = false;
        if (dragging) {
            dragging = false;
            voicePanelEl.classList.remove('dragging');
            saveVoicePanelState();
        } else {
            // Hareket etmeden bırakıldıysa: dokunma → kontrolleri göster/gizle
            if (voicePanelEl.classList.contains('controls-visible')) hideVoiceControlsOverlay();
            else showVoiceControlsOverlay();
        }
    }
    voicePanelEl.addEventListener('pointerup', stopPointer);
    voicePanelEl.addEventListener('pointercancel', stopPointer);

    // Kontrol çubuğu içindeyken (ör. ses kaydırıcısıyla oynarken) otomatik gizlenmeyi ertele
    voiceControls?.addEventListener('pointerdown', (e) => {
        if (!document.body.classList.contains('room-fullscreen')) return;
        e.stopPropagation();
        showVoiceControlsOverlay();
    });
})();

// Köşedeki tutamaçtan sürükleyerek balonu büyütme/küçültme (genişlik değişir, yükseklik
// aspect-ratio ile orantılı takip eder). Sabit köşe (left/top) yerinde kalır, sağ-alta doğru büyür.
(function enableVoicePanelResize() {
    if (!voicePanelEl || !voiceResizeHandleEl) return;
    let resizing = false;
    let startX = 0, startWidth = 0;

    voiceResizeHandleEl.addEventListener('pointerdown', (e) => {
        if (!document.body.classList.contains('room-fullscreen')) return;
        resizing = true;
        startX = e.clientX;
        startWidth = voicePanelEl.getBoundingClientRect().width;
        voicePanelEl.classList.add('resizing');
        voiceResizeHandleEl.setPointerCapture(e.pointerId);
        e.stopPropagation();
        e.preventDefault();
    });

    voiceResizeHandleEl.addEventListener('pointermove', (e) => {
        if (!resizing) return;
        const newWidth = Math.min(Math.max(VOICE_PANEL_MIN_WIDTH, startWidth + (e.clientX - startX)), voicePanelMaxWidth());
        voicePanelEl.style.width = newWidth + 'px';
        e.stopPropagation();
    });

    function stopResizing(e) {
        if (!resizing) return;
        resizing = false;
        voicePanelEl.classList.remove('resizing');
        // Büyüyünce ekran dışına taşmışsa konumu da geri çek.
        // NOT: .style.left henüz hiç sürüklenmediyse boştur (konum CSS'ten geliyordur);
        // gerçek konumu her zaman getBoundingClientRect ile okumak gerekir, yoksa panel (0,0)'a zıplar.
        const currentRect = voicePanelEl.getBoundingClientRect();
        const { left, top } = clampVoicePanelToViewport(currentRect.left, currentRect.top);
        voicePanelEl.style.left = left + 'px';
        voicePanelEl.style.top = top + 'px';
        saveVoicePanelState();
        showVoiceControlsOverlay(); // boyutlandırma bitince kontroller hemen kaybolmasın
        e?.stopPropagation();
    }
    voiceResizeHandleEl.addEventListener('pointerup', stopResizing);
    voiceResizeHandleEl.addEventListener('pointercancel', stopResizing);
})();

// ── WebRTC Ses Seviyesi (Discord Tarzı >100% Boost) ───────────
const remoteVolumeSlider = document.getElementById('remote-volume-slider');
const remoteVolumeLabel = document.getElementById('remote-volume-label');
let audioCtx = null;
let audioGain = null;
let mediaStreamSource = null;
let activeRemoteStream = null;

function applyRemoteVolume(val) {
    if (remoteVolumeLabel) {
        remoteVolumeLabel.textContent = Math.round(val * 100) + '%';
    }
    const safeVal = parseFloat(val) || 1.0;
    
    // Eğer 100%'ün altındaysak veya audioCtx kurulamadıysa (henüz tıklanmadıysa vb)
    if (safeVal <= 1.0 && !audioCtx) {
        remoteVideo.volume = safeVal;
        remoteVideo.muted = false;
        return;
    }

    // 100% üzeriyse veya zaten gainNode kurulduysa Amplifikasyon başlasın
    if (!audioCtx) {
        try {
            audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            audioGain = audioCtx.createGain();
            audioGain.connect(audioCtx.destination);
        } catch(e) {
            console.error('[WebRTC] AudioContext oluşturulamadı:', e);
            remoteVideo.volume = Math.min(safeVal, 1.0);
            return;
        }
    }
    
    // Stream varsa ve bağlanmamışsa bağla
    if (activeRemoteStream && !mediaStreamSource && audioCtx.state !== 'closed') {
        try {
            mediaStreamSource = audioCtx.createMediaStreamSource(activeRemoteStream);
            mediaStreamSource.connect(audioGain);
        } catch (e) {
            console.warn('[WebRTC] gainNode bağlama hatası:', e);
        }
    }
    
    if (mediaStreamSource) {
        remoteVideo.muted = true; // Kendi elementini sustur, AudioContext ile yayını ver
        audioGain.gain.value = safeVal;
    }
}

if (remoteVolumeSlider) {
    // LocalStorage veya varsayılan değer
    let storedVol = localStorage.getItem('perde_volume');
    if (storedVol) {
        remoteVolumeSlider.value = storedVol;
        applyRemoteVolume(storedVol);
    }
    
    remoteVolumeSlider.addEventListener('input', (e) => {
        applyRemoteVolume(e.target.value);
    });
    remoteVolumeSlider.addEventListener('change', (e) => {
        localStorage.setItem('perde_volume', e.target.value);
    });
}

// 🔥 Mobil Ağ (CGNAT) Geçişi ve Veri Tasarrufu İçin TURN Sunucusu + Optimize WebRTC Ayarları
const RTC_CONFIG = {
    iceServers: [
        { urls: 'stun:stun.l.google.com:19302' },
        { urls: 'stun:stun1.l.google.com:19302' },
        {
            urls: 'turn:openrelay.metered.ca:80',
            username: 'openrelayproject',
            credential: 'openrelayproject'
        },
        {
            urls: 'turn:openrelay.metered.ca:443',
            username: 'openrelayproject',
            credential: 'openrelayproject'
        },
        {
            urls: 'turn:openrelay.metered.ca:443?transport=tcp',
            username: 'openrelayproject',
            credential: 'openrelayproject'
        }
    ],
    iceCandidatePoolSize: 10
};

// 🔥 Mobil Cihazlar İçin Çözünürlük ve Saniye Başı Kare (FPS) Sınırlandırması (Kota Tasarrufu)
const MEDIA_CONSTRAINTS = {
    audio: { echoCancellation: true, noiseSuppression: true },
    video: {
        width: { max: 640 },
        height: { max: 480 },
        frameRate: { max: 20 },
        facingMode: 'user' // Mobilde varsayılan ön kamera
    }
};

let callTimeout = null;

function requestCall(videoEnabled = true) {
    if (isWaitingForAccept) {
        // İptal et
        socket.emit('webrtc-call-reject', { roomId });
        endCall(false, 'Arama iptal edildi');
        return;
    }
    if (localStream || peerConnection) {
        showToast('❌ Zaten bir görüşmedesiniz.');
        return;
    }
    isVideoCall = videoEnabled;
    isWaitingForAccept = true;
    startCallBtn.textContent = '❌ İptal Et';
    startCallBtn.classList.replace('btn-primary', 'btn-danger');
    
    if (startAudioBtn) { startAudioBtn.style.display = 'none'; }
    
    socket.emit('webrtc-call-request', { roomId, isVideo: videoEnabled, callerName: username });

    // 30 saniye yanıt gelmezse zaman aşımına uğrat
    callTimeout = setTimeout(() => {
        if (isWaitingForAccept) {
            socket.emit('webrtc-call-reject', { roomId });
            endCall(false, '⏰ Arama zaman aşımına uğradı');
        }
    }, 30000);
}

async function startCall(videoEnabled = true) {
    document.body.classList.add('call-active');
    if (callTimeout) { clearTimeout(callTimeout); callTimeout = null; }
    isVideoCall = videoEnabled;
    isWaitingForAccept = false;
    try {
        localStream = await navigator.mediaDevices.getUserMedia(
            videoEnabled ? { audio: MEDIA_CONSTRAINTS.audio, video: MEDIA_CONSTRAINTS.video } : { audio: MEDIA_CONSTRAINTS.audio, video: false }
        );
        if (videoEnabled) {
            localVideo.srcObject = localStream;
            localVideo.style.display = 'block';
            voiceVideos.style.display = 'block';
        } else {
            localVideo.style.display = 'none';
            voiceVideos.style.display = 'none';
        }
        voiceControls.style.display = 'flex';
        
        if (videoEnabled) camBtn.style.display = '';
        else camBtn.style.display = 'none';
        
        startCallBtn.textContent = '✅ Görüşmede';
        startCallBtn.disabled = true;
        startCallBtn.classList.remove('btn-danger');
        startCallBtn.classList.add('btn-primary');
        if (startAudioBtn) { startAudioBtn.style.display = 'none'; }

        peerConnection = new RTCPeerConnection(RTC_CONFIG);
        localStream.getTracks().forEach(track => {
            const sender = peerConnection.addTrack(track, localStream);
            // Mobilde donmaları önlemek için saniye başı video verisini sınırla (Örn: 400kbps)
            if (track.kind === 'video' && sender.getParameters) {
                const params = sender.getParameters();
                if (!params.encodings) params.encodings = [{}];
                params.encodings[0].maxBitrate = 400000;
                params.encodings[0].maxFramerate = 20;
                sender.setParameters(params).catch(() => {});
            }
        });

        // Kopmaları algılayıp uyarı veren dinleyici (ICE Restart veya Graceful Kapanış için)
        peerConnection.onconnectionstatechange = () => {
            if (peerConnection.connectionState === 'disconnected' || peerConnection.connectionState === 'failed') {
                showToast('⚠️ Bağlantı koptu. Zayıf ağ veya VPN engeli olabilir.');
                endCall(true, '⚠️ Bağlantı hatası.');
            }
        };

        peerConnection.onicecandidate = (e) => {
            if (e.candidate) socket.emit('webrtc-ice-candidate', { roomId, candidate: e.candidate });
        };

        peerConnection.ontrack = (e) => { 
        activeRemoteStream = e.streams[0];
        remoteVideo.srcObject = e.streams[0]; 
        
        // Ses ayarını yeniden uygula
        const currentVol = parseFloat(remoteVolumeSlider ? remoteVolumeSlider.value : 1);
        applyRemoteVolume(currentVol);

        const hasRemoteVideo = e.streams[0].getVideoTracks().length > 0;
        if (hasRemoteVideo) {
            voiceVideos.style.display = 'block';
            remoteVideo.style.display = 'block';
        }
    };

        peerConnection.onnegotiationneeded = async () => {
            const offer = await peerConnection.createOffer();
            await peerConnection.setLocalDescription(offer);
            socket.emit('webrtc-offer', { roomId, offer });
        };

    } catch (err) {
        showToast('❌ Mikrofon erişimi reddedildi');
        console.error('[WebRTC]', err);
        endCall(false); // Hata durumunda görüşme durumunu sıfırla
    }
}

function clearCallOverlay() {
    const overlay = document.getElementById('incoming-call-overlay');
    if (overlay) overlay.remove();
    if (callTimeout) { clearTimeout(callTimeout); callTimeout = null; }
    stopIncomingCallRing();
}

// ── Gelen arama sesi: film izlerken ekrana bakmıyorsan aramanın farkedilmesi için
// ufak sesli bir bildirim çalar; overlay kapanana kadar (kabul/red/karşı tarafın
// vazgeçmesi) tekrarlar. Ses dosyası gerekmez, Web Audio API ile anlık üretilir.
let incomingCallRingTimer = null;
// Karşı tarafı ararken senin kulağına gelen "çevirme/çalma sesi" (tek uzun ton,
// 440Hz+480Hz birlikte) neyse onun HIZLANDIRILMIŞ hali: çift vuruşlu bir "zil/ding-dong"
// değil, tek sürekli tonun kısa aralıklarla tekrarlandığı bir nabız gibi.
function playIncomingCallChime() {
    try {
        const AudioCtx = window.AudioContext || window.webkitAudioContext;
        if (!AudioCtx) return;
        const ctx = new AudioCtx();
        const RING_FREQS = [440, 480];
        const VOLUME = 0.09; // ufak sesli
        const TONE_DUR = 0.8; // tek uzun ton (çevirme sesi gibi), iki ayrı vuruş değil
        const ring = () => {
            const now = ctx.currentTime;
            const gain = ctx.createGain();
            gain.connect(ctx.destination);
            gain.gain.setValueAtTime(0, now);
            gain.gain.linearRampToValueAtTime(VOLUME, now + 0.05);
            gain.gain.setValueAtTime(VOLUME, now + TONE_DUR - 0.08);
            gain.gain.linearRampToValueAtTime(0, now + TONE_DUR);
            RING_FREQS.forEach((freq) => {
                const osc = ctx.createOscillator();
                osc.type = 'sine';
                osc.frequency.value = freq;
                osc.connect(gain);
                osc.start(now);
                osc.stop(now + TONE_DUR + 0.02);
            });
            setTimeout(() => ctx.close().catch(() => {}), (TONE_DUR + 0.2) * 1000);
        };
        if (ctx.state === 'suspended') ctx.resume().then(ring).catch(() => {});
        else ring();
    } catch (_) { /* AudioContext yoksa sessizce yoksay, arama yine de görsel olarak bildiriliyor */ }
}

function startIncomingCallRing() {
    stopIncomingCallRing();
    playIncomingCallChime();
    let elapsedMs = 0;
    // Standart çevirme sesi ritmi (~1sn çalma + 3-4sn sessizlik) yerine hızlandırılmış:
    // ~0.8sn çalma + ~0.8sn sessizlik, daha sık ve daha çabuk farkedilir.
    const RING_INTERVAL_MS = 1600;
    const RING_MAX_MS = 32000; // güvenlik: overlay bir şekilde kapanmazsa sonsuza dek çalmasın
    incomingCallRingTimer = setInterval(() => {
        elapsedMs += RING_INTERVAL_MS;
        if (elapsedMs >= RING_MAX_MS || !document.getElementById('incoming-call-overlay')) {
            stopIncomingCallRing();
            return;
        }
        playIncomingCallChime();
    }, RING_INTERVAL_MS);
}

function stopIncomingCallRing() {
    if (incomingCallRingTimer) { clearInterval(incomingCallRingTimer); incomingCallRingTimer = null; }
}

function endCall(emitEvent = true, toastMsg = '📵 Görüşme sonlandırıldı') {
    document.body.classList.remove('call-active');
    clearCallOverlay();
    isWaitingForAccept = false;
    if (peerConnection) { peerConnection.close(); peerConnection = null; }
    if (localStream) { localStream.getTracks().forEach(t => t.stop()); localStream = null; }
    localVideo.srcObject = null;
    remoteVideo.srcObject = null;
    voiceVideos.style.display = 'none';
    voiceControls.style.display = 'none';
    
    startCallBtn.textContent = '📹 Görüntülü';
    startCallBtn.disabled = false;
    startCallBtn.classList.remove('btn-danger');
    startCallBtn.classList.add('btn-primary');
    
    if (startAudioBtn) { 
        startAudioBtn.textContent = '🎤 Sesli'; 
        startAudioBtn.disabled = false; 
        startAudioBtn.style.display = 'inline-block';
    }
    
    isVideoCall = false;
    if (emitEvent) socket.emit('webrtc-hangup', { roomId });
    if (toastMsg) showToast(toastMsg);
    
    // Görüntülü konuşma sonlandığında paneli güvenlice yerine koy
    const voicePanel = document.querySelector('.voice-panel');
    const w2gRight = document.querySelector('.w2g-right');
    
    // EĞER ŞU AN TAM EKRANDAYSAK YERİNDEN OYNATMA (DOM bozulmasın diye style.css zaten display:none yapıyor). 
    // Sadece normal ekrandaysak yerine koy.
    const isActuallyFullscreen = (document.fullscreenElement || document.webkitFullscreenElement) != null;
    if (!isActuallyFullscreen && voicePanel && w2gRight && voicePanel.parentElement !== w2gRight) {
        w2gRight.prepend(voicePanel);
    }
    
    // Tam ekran senkronizasyonunu yeniden çalıştır (DOM bozulmasını engeller)
    if (typeof syncCallVisibilityForFullscreen === 'function') {
        syncCallVisibilityForFullscreen();
    }

    if (document.pictureInPictureElement === remoteVideo && document.exitPictureInPicture) {
        document.exitPictureInPicture().catch(() => { });
    }
}

muteBtn?.addEventListener('click', () => {
    if (!localStream) return;
    isMuted = !isMuted;
    localStream.getAudioTracks().forEach(t => t.enabled = !isMuted);
    setBtnIconLabel(muteBtn, isMuted ? '🔇' : '🎤', isMuted ? ' Sessiz (Kapalı)' : ' Sessiz');
    muteBtn.classList.toggle('btn-danger', isMuted);
});

camBtn?.addEventListener('click', () => {
    if (!localStream) return;
    isCamOff = !isCamOff;
    localStream.getVideoTracks().forEach(t => t.enabled = !isCamOff);
    // Track "enabled=false" olunca video elementi siyah kare göstermeye devam ediyordu
    // (balonun köşesinde siyah bir kutu kalıyordu); kamerayı kapatınca öğeyi tamamen
    // gizleyip sadece karşı tarafın görüntüsü kalsın, tekrar açınca geri getir.
    localVideo.style.display = isCamOff ? 'none' : 'block';
    setBtnIconLabel(camBtn, isCamOff ? '🚫' : '📷', isCamOff ? ' Kamera (Kapalı)' : ' Kamera Kapat');
    camBtn.classList.toggle('btn-danger', isCamOff);
});

// Tam ekranda ikon/etiket ayrı tutuluyor ki CSS etiketi gizleyip sadece ikonu bırakabilsin
// (buton çok küçük balonda 4 yazılı buton yan yana sığmıyordu).
function setBtnIconLabel(btn, icon, label) {
    const iconEl = btn.querySelector('.btn-icon');
    const labelEl = btn.querySelector('.btn-label');
    if (iconEl && labelEl) { iconEl.textContent = icon; labelEl.textContent = label; }
    else { btn.textContent = icon + label; }
}

startCallBtn?.addEventListener('click', () => requestCall(true));
startAudioBtn?.addEventListener('click', () => requestCall(false));
endCallBtn?.addEventListener('click', () => endCall(true));

// ── WebRTC Arama Sinyalleri (Yeni) ────────────────────────────────────────────────
socket.on('webrtc-call-request', ({ isVideo, callerName }) => {
    if (localStream || peerConnection || isWaitingForAccept) {
        socket.emit('webrtc-call-reject', { roomId });
        return;
    }
    isWaitingForAccept = true;
    
    const overlay = document.createElement('div');
    overlay.id = 'incoming-call-overlay';
    overlay.style.cssText = "position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.85); z-index:10000; display:flex; justify-content:center; align-items:center; backdrop-filter:blur(5px);";
    overlay.innerHTML = `
        <div style="background:#1e293b; padding:30px; border-radius:20px; text-align:center; border:1px solid #334155; min-width:320px; box-shadow: 0 10px 25px rgba(0,0,0,0.5);">
            <div style="font-size:50px; margin-bottom:15px; animation: incomingBounce 1s infinite alternate;">${isVideo ? '📹' : '🎤'}</div>
            <h3 style="margin:0 0 10px; color:#fff; font-size:24px;">Gelen Arama</h3>
            <p style="color:#94a3b8; margin-bottom:25px; font-size:16px;"><strong>${callerName || 'Birisi'}</strong> seni ${isVideo ? 'görüntülü' : 'sesli'} arıyor...</p>
            <div style="display:flex; gap:15px;">
                <button id="reject-call-btn" class="btn btn-danger" style="flex:1; padding:12px; font-size:16px; border-radius:10px;">🔴 Reddet</button>
                <button id="accept-call-btn" class="btn btn-primary" style="flex:1; padding:12px; font-size:16px; border-radius:10px; background:#10b981; border:none;">🟢 Aç</button>
            </div>
        </div>
        <style>
            @keyframes incomingBounce { from { transform: translateY(0); } to { transform: translateY(-10px); } }
        </style>
    `;
    
    // Tam ekrandayken (örneğin film izlerken) overlay dom'un arkasında kalmasın diye
    const actualFsEl = document.fullscreenElement || document.webkitFullscreenElement;
    if (actualFsEl) {
        actualFsEl.appendChild(overlay);
    } else {
        document.body.appendChild(overlay);
    }

    // Film izlerken ekrana bakmıyor olabilirsin diye ufak sesli bir bildirim çalıyoruz,
    // overlay kapanana (kabul/red/karşı taraf vazgeçene) kadar birkaç saniyede bir tekrarlanır.
    startIncomingCallRing();

    document.getElementById('reject-call-btn').addEventListener('click', () => {
        isWaitingForAccept = false;
        socket.emit('webrtc-call-reject', { roomId });
        clearCallOverlay();
    });

    document.getElementById('accept-call-btn').addEventListener('click', async () => {
        clearCallOverlay();
        
        try {
            document.body.classList.add('call-active');
            
            localStream = await navigator.mediaDevices.getUserMedia(
                isVideo ? { audio: MEDIA_CONSTRAINTS.audio, video: MEDIA_CONSTRAINTS.video } : { audio: MEDIA_CONSTRAINTS.audio, video: false }
            );
            isVideoCall = isVideo;
            
            if (isVideo) {
                localVideo.srcObject = localStream;
                localVideo.style.display = 'block';
                voiceVideos.style.display = 'block';
                camBtn.style.display = '';
            } else {
                localVideo.style.display = 'none';
                camBtn.style.display = 'none';
            }
            voiceControls.style.display = 'flex';
            startCallBtn.textContent = '✅ Görüşmede';
            startCallBtn.disabled = true;
            startCallBtn.classList.remove('btn-danger');
            startCallBtn.classList.add('btn-primary');
            if (startAudioBtn) { startAudioBtn.style.display = 'none'; }
            
            socket.emit('webrtc-call-accept', { roomId });
            showToast('✅ Arama kabul edildi. Bağlanıyor...');
            
            // Tam ekran varsa paneli hemen doğru yere taşı
            if (typeof syncCallVisibilityForFullscreen === 'function') {
                syncCallVisibilityForFullscreen();
            }
        } catch (err) {
            isWaitingForAccept = false;
            showToast('❌ Mikrofon/Kamera erişimi reddedildi');
            socket.emit('webrtc-call-reject', { roomId });
        }
    });
});

socket.on('webrtc-call-accept', () => {
    isWaitingForAccept = false;
    showToast('✅ Karşı taraf aramayı kabul etti.');
    startCall(isVideoCall);
});

socket.on('webrtc-call-reject', () => {
    isWaitingForAccept = false;
    // Bu olay hem "sen aradın karşı taraf reddetti" hem de "karşı taraf seni aradı ama
    // sen cevap vermeden o vazgeçti/zaman aşımına uğradı" durumunda gelir — ikinci durumda
    // gelen-arama overlay'i (ve sesi) hiç kapanmıyor, ekranda asılı kalıyordu.
    clearCallOverlay();
    startCallBtn.textContent = '📹 Görüntülü';
    startCallBtn.disabled = false;
    startCallBtn.classList.remove('btn-danger');
    startCallBtn.classList.add('btn-primary');
    if (startAudioBtn) { 
        startAudioBtn.textContent = '🎤 Sesli'; 
        startAudioBtn.disabled = false; 
        startAudioBtn.style.display = 'inline-block';
    }
    showToast('📵 Arama reddedildi veya karşı taraf meşgul');
});

// ── WebRTC sinyal olayları ────────────────────────────────────────────────────────
socket.on('webrtc-offer', async ({ offer }) => {
    if (!localStream) {
        // Gelen offer'da video track olup olmadığını kontrol et
        const offerHasVideo = offer.sdp && offer.sdp.includes('m=video');
        try {
            document.body.classList.add('call-active');
            localStream = await navigator.mediaDevices.getUserMedia(
                offerHasVideo ? { audio: MEDIA_CONSTRAINTS.audio, video: MEDIA_CONSTRAINTS.video } : { audio: MEDIA_CONSTRAINTS.audio, video: false }
            );
            isVideoCall = offerHasVideo;
            if (offerHasVideo) {
                localVideo.srcObject = localStream;
                localVideo.style.display = 'block';
                voiceVideos.style.display = 'block';
                camBtn.style.display = '';
            } else {
                localVideo.style.display = 'none';
                camBtn.style.display = 'none';
            }
            voiceControls.style.display = 'flex';
            startCallBtn.textContent = '✅ Görüşmede';
            startCallBtn.disabled = true;
            startCallBtn.classList.remove('btn-danger');
            startCallBtn.classList.add('btn-primary');
            if (startAudioBtn) { startAudioBtn.style.display = 'none'; }

            // Eğer tam ekrandaysak paneli hemen aktif et
            if (typeof syncCallVisibilityForFullscreen === 'function') {
                syncCallVisibilityForFullscreen();
            }
        } catch (err) {
            showToast('❌ Görüşme isteği var ama mikrofon erişimi reddedildi');
            return;
        }
    }
    peerConnection = new RTCPeerConnection(RTC_CONFIG);
    localStream.getTracks().forEach(track => {
        const sender = peerConnection.addTrack(track, localStream);
        if (track.kind === 'video' && sender.getParameters) {
            const params = sender.getParameters();
            if (!params.encodings) params.encodings = [{}];
            params.encodings[0].maxBitrate = 400000;
            params.encodings[0].maxFramerate = 20;
            sender.setParameters(params).catch(() => {});
        }
    });

    peerConnection.onconnectionstatechange = () => {
        if (peerConnection.connectionState === 'disconnected' || peerConnection.connectionState === 'failed') {
            endCall(true, '⚠️ Zayıf bağlantı nedeniyle görüşme koptu');
        }
    };

    peerConnection.onicecandidate = (e) => {
        if (e.candidate) socket.emit('webrtc-ice-candidate', { roomId, candidate: e.candidate });
    };
    peerConnection.ontrack = (e) => { 
        activeRemoteStream = e.streams[0];
        remoteVideo.srcObject = e.streams[0]; 
        const currentVol = parseFloat(remoteVolumeSlider ? remoteVolumeSlider.value : 1);
        applyRemoteVolume(currentVol);

        const hasRemoteVideo = e.streams[0].getVideoTracks().length > 0;
        if (hasRemoteVideo) {
            voiceVideos.style.display = 'block';
            remoteVideo.style.display = 'block';
        }
    };
    await peerConnection.setRemoteDescription(new RTCSessionDescription(offer));
    const answer = await peerConnection.createAnswer();
    await peerConnection.setLocalDescription(answer);
    socket.emit('webrtc-answer', { roomId, answer });
});

socket.on('webrtc-answer', async ({ answer }) => {
    if (!peerConnection) return;
    await peerConnection.setRemoteDescription(new RTCSessionDescription(answer));
});

socket.on('webrtc-ice-candidate', async ({ candidate }) => {
    if (!peerConnection) return;
    try { await peerConnection.addIceCandidate(new RTCIceCandidate(candidate)); } catch (e) { }
});

socket.on('webrtc-hangup', () => {
    endCall(false, '📵 Karşı taraf görüşmeyi sonlandırdı');
});
