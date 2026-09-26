// Oda sayfasının ana modülü: katılma, socket, sohbet, kişiler, ayarlar.

import { $, $$, h, icon, toast, avatar, colorFor, PALETTE, store, copyText, shareText, app } from './ui.js';
import { Player } from './player.js';
import { Call } from './call.js';
import { Library } from './library.js';

// Özelliğin ekranda görünen adı — tek yer.
const FEATURE_NAME = 'Birlikte izle';

const params = new URLSearchParams(location.search);
const roomId = params.get('room') || '';
const hostToken = params.get('hostToken') || '';
const inApp = params.get('app') === '1' || !!app;

// --- tema ---------------------------------------------------------------------
(function theme() {
    const t = params.get('theme');
    const dark = t ? t === 'dark' : !matchMedia('(prefers-color-scheme: light)').matches;
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    const accent = params.get('accent');
    if (/^#[0-9a-f]{6}$/i.test(accent || '')) document.documentElement.style.setProperty('--accent', accent);
    $('meta[name=theme-color]').setAttribute('content', dark ? '#121620' : '#F3F5F9');
    document.title = `hayalet · ${FEATURE_NAME}`;
    $$('[data-name]').forEach(el => { el.textContent = FEATURE_NAME; });
})();

// --- profil ---------------------------------------------------------------------
const me = {
    id: '',
    name: (params.get('name') || store.get('oda.name') || '').trim().slice(0, 24),
    color: /^#[0-9a-f]{6}$/i.test(params.get('color') || '') ? params.get('color') : (store.get('oda.color') || ''),
};

const state = {
    isLeader: false, controlMode: 'all', people: [], now: {}, catalog: false,
    unreadChat: 0, suggestions: 0, lastSender: null, publicUrl: '',
};

let socket = null, player = null, call = null, library = null;

// --- katılma ekranı -----------------------------------------------------------------
function showJoin() {
    const form = $('#join form');
    const name = $('#join-name');
    name.value = me.name;
    if (!me.color) me.color = PALETTE[Math.floor(Math.random() * PALETTE.length)];
    const sw = $('#join-colors');
    const paint = () => sw.replaceChildren(...PALETTE.map(c => h('button', {
        type: 'button', class: 'swatch' + (c === me.color ? ' on' : ''), style: { background: c },
        role: 'radio', 'aria-checked': String(c === me.color), 'aria-label': 'Renk',
        onclick: () => { me.color = c; paint(); }
    })));
    paint();
    // Android tarayıcısı: uygulama yüklüyse oda orada açılsın. Tarayıcılar
    // sayfa açılırken uygulamaya kendiliğinden geçmiyor (Chrome sunucunun /i
    // yönlendirmesini de yok sayıyor), dokunuşla açılan linke izin veriyor.
    // Düz hayalet:// kullanılıyor: intent:// biçimini Chrome açıyor ama Mi
    // tarayıcısı yok sayıyor (tablette denendi).
    // Tabletlerde Chrome varsayılan olarak "masaüstü sitesi" kimliği gönderiyor
    // (Android yazmıyor, X11; Linux diyor); dokunmatik Linux da Android sayılır.
    const android = /Android/.test(navigator.userAgent)
        || (/Linux/.test(navigator.userAgent) && navigator.maxTouchPoints > 0);
    if (android && !inApp) {
        const oda = `${location.origin}/room.html?room=${encodeURIComponent(roomId)}`;
        const a = $('#join-app a');
        a.href = `hayalet://oda?u=${encodeURIComponent(oda)}`;
        a.onclick = () => setTimeout(() => {
            // Uygulama açıldıysa sayfa arka plana geçmiştir. (toast olmaz:
            // bildirim kutusu henüz gizli olan #app'in içinde.)
            if (!document.hidden) $('#join-app .join-or').textContent =
                'Uygulama açılmadı — yüklü değilse aşağıdan tarayıcıda devam et.';
        }, 1500);
        $('#join-app').hidden = false;
    }
    $('#join').hidden = false;
    // Uygulama düğmesi varken klavye açılıp onu ekrandan itmesin.
    if ($('#join-app').hidden) setTimeout(() => name.focus(), 50);
    form.onsubmit = e => {
        e.preventDefault();
        const v = name.value.trim().slice(0, 24);
        if (!v) return name.focus();
        me.name = v;
        store.set('oda.name', v); store.set('oda.color', me.color);
        $('#join').hidden = true;
        start();
    };
}

// --- oda --------------------------------------------------------------------------------
function start() {
    if (!me.color) me.color = colorFor(me.name);
    $('#app').hidden = false;
    socket = io({
        query: hostToken ? { hostToken } : {},
        transports: ['websocket', 'polling'],
        timeout: 20000, reconnection: true, reconnectionAttempts: Infinity,
        reconnectionDelay: 900, reconnectionDelayMax: 5000,
    });
    const emit = (ev, data = {}) => socket.emit(ev, { roomId, ...data });

    player = new Player($('#stage'), { roomId, emit });
    player.onDoubleTap = toggleFullscreen;
    call = new Call({ socket, roomId, me: () => me, dock: $('#call-dock'), onState: renderCallState });
    library = new Library({
        socket, roomId, root: $('#library'), isLeader: () => state.isLeader,
        onBadge: n => { state.suggestions = n; renderBadges(); if (n > (library._lastN || 0) && state.isLeader) toast('Yeni öneri geldi.', 'ok'); library._lastN = n; },
    });

    bindSocket(emit);
    bindUi(emit);
}

function bindSocket(emit) {
    socket.on('connect', () => {
        me.id = socket.id;
        emit('join-room', { username: me.name, hostToken, color: me.color });
    });
    socket.on('disconnect', () => addSystem('Bağlantı koptu, yeniden bağlanılıyor…'));
    socket.io.on('reconnect', () => addSystem('Yeniden bağlanıldı.'));

    socket.on('room-state', s => {
        // Sohbet geçmişi: yeniden bağlanmada da tekrar gelir, o yüzden kutu
        // her seferinde geçmişle baştan kurulur (çift mesaj olmasın).
        $('#messages').replaceChildren();
        state.lastSender = null;
        (s.chat || []).forEach(m => addChat(m, true));
        state.controlMode = s.controlMode || 'all';
        state.catalog = !!s.catalog;
        library.setCatalog(state.catalog);
        setPeople(s.people || []);
        setNow(s.now || {});
        if (s.videoUrl) {
            player.load(s.videoUrl, s.subtitles || [], {
                startTime: s.currentTime || 0, autoplay: s.isPlaying && !s.isBuffering,
                onReady: () => {
                    if (s.isBuffering) { player.setRoomBuffering(true, s.currentTime); }
                    else if (s.isPlaying) player.syncPlay(s.currentTime);
                    else player.syncSeek(s.currentTime);
                },
            });
        } else player.clear();
        applyControl();
    });
    socket.on('role-updated', ({ isLeader }) => {
        state.isLeader = !!isLeader;
        player.setLeader(state.isLeader);
        library.refreshRole();
        applyControl();
        renderPeople();
    });
    socket.on('people', ({ people }) => setPeople(people || []));
    socket.on('user-joined', ({ username }) => addSystem(`<b>${esc(username)}</b> odaya katıldı.`, true));
    socket.on('user-left', ({ username }) => addSystem(`<b>${esc(username)}</b> ayrıldı.`, true));

    socket.on('video-changed', ({ videoUrl, subtitles, now }) => {
        setNow(now || {});
        if (!videoUrl) { player.clear(); return; }
        player.load(videoUrl, subtitles || []);
        toast(now && now.title ? `Açıldı: ${now.title}` : 'Yeni içerik açıldı.', 'ok');
    });
    socket.on('content-loading', ({ loading }) => { $('#stage').classList.toggle('opening', !!loading); library.setLoading(loading); });

    socket.on('play', ({ currentTime }) => player.remote('play', currentTime));
    socket.on('pause', ({ currentTime }) => player.remote('pause', currentTime));
    socket.on('seek', ({ currentTime }) => player.remote('seek', currentTime));
    socket.on('sync-heartbeat', d => player.heartbeat(d));
    socket.on('control-denied', ({ currentTime, isPlaying }) => {
        toast('Kumanda şu an yalnız ev sahibinde.', 'warn');
        if (isPlaying) player.syncPlay(currentTime); else player.syncPause(currentTime);
    });
    socket.on('room-buffering', ({ isBuffering, username, currentTime }) => {
        player.setRoomBuffering(isBuffering, currentTime);
        const chip = $('#buffer-chip');
        chip.hidden = !isBuffering;
        if (isBuffering) chip.textContent = `${username || 'Biri'} bekleniyor…`;
    });
    socket.on('room-settings', ({ controlMode }) => { state.controlMode = controlMode; applyControl(); renderPeople(); });

    socket.on('chat-message', m => addChat(m));
    socket.on('system-message', ({ message }) => addSystem(message, true));
    socket.on('clear-chat', () => { $('#messages').replaceChildren(); state.lastSender = null; });
    socket.on('kicked', ({ by }) => {
        call.end(false, '');
        player.clear();
        $('#kicked-by').textContent = by ? `${by} seni odadan çıkardı.` : '';
        $('#kicked').hidden = false;
        socket.disconnect();
    });
}

function esc(s) {
    return String(s || '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// --- kişiler ----------------------------------------------------------------------------
function setPeople(list) {
    state.people = list;
    const mine = list.find(p => p.id === socket.id);
    if (mine) state.isLeader = !!mine.leader;
    renderPeople();
    renderFaces();
    $('#people-count').textContent = String(list.length);
}

function renderFaces() {
    const shown = state.people.slice(0, 4);
    const extra = state.people.length - shown.length;
    $('#faces').replaceChildren(...shown.map(p => avatar(p.name, p.color)),
        ...(extra > 0 ? [h('span', { class: 'more' }, `+${extra}`)] : []));
}

function renderPeople() {
    const leader = state.isLeader;
    $('#people').replaceChildren(...state.people.map(p => h('div', { class: 'person' },
        avatar(p.name, p.color),
        h('div', { class: 'person-name' }, p.name, p.id === socket.id ? h('small', {}, '(sen)') : null),
        p.leader ? h('span', { class: 'tag' }, icon('crown'), 'Ev sahibi') : null,
        leader && p.id !== socket.id ? h('button', {
            class: 'btn icon ghost', 'aria-label': `${p.name} kişisini çıkar`, title: 'Odadan çıkar',
            onclick: () => confirmKick(p)
        }, icon('kick')) : null)));

    const box = $('#host-box');
    box.hidden = !leader;
    if (leader) {
        const seg = h('div', { class: 'segmented', role: 'radiogroup' },
            ...[['all', 'Herkes'], ['host', 'Yalnız ben']].map(([v, l]) => h('button', {
                class: state.controlMode === v ? 'on' : '', role: 'radio', 'aria-checked': String(state.controlMode === v),
                onclick: () => socket.emit('room-settings', { roomId, controlMode: v })
            }, l)));
        box.replaceChildren(h('h3', {}, 'Kumanda'),
            h('p', { class: 'muted' }, 'Oynatma, durdurma ve ileri-geri sarma kimde olsun?'), seg);
    }
}

function confirmKick(p) {
    openSheet('kick', [
        h('h2', {}, `${p.name} odadan çıkarılsın mı?`),
        h('p', { class: 'muted' }, 'Bu odaya bir daha giremez.'),
        h('div', { class: 'row gap' },
            h('button', { class: 'btn ghost block', onclick: closeSheet }, 'Vazgeç'),
            h('button', { class: 'btn danger block', onclick: () => { socket.emit('kick-user', { roomId, id: p.id }); closeSheet(); } }, 'Çıkar')),
    ]);
}

function applyControl() {
    const can = state.controlMode === 'all' || state.isLeader;
    player.setCanControl(can);
    $('#empty-hint').textContent = state.isLeader
        ? 'İçerik sekmesinden bir dizi ya da film seç.'
        : 'Ev sahibi bir şey açınca burada başlayacak. İstersen İçerik sekmesinden öneride bulun.';
    $('#empty-pick').lastChild.textContent = state.isLeader ? 'İçerik seç' : 'Öneride bulun';
    syncNext();
}

/** Oynatıcıdaki "Sonraki bölüm": yalnız ev sahibine, sırada bölüm varsa. */
function syncNext() {
    player.onPrefetch = () => socket.emit('prefetch-next', { roomId });
    player.setNext(state.isLeader && state.now.hasNext ? () => socket.emit('next-episode', { roomId }) : null);
}

function setNow(now) {
    state.now = now || {};
    library.setNow(state.now);
    const t = $('#now-title');
    t.replaceChildren();
    if (state.now.title) {
        t.append(h('b', {}, state.now.title));
        if (state.now.subtitle) t.append(' · ', state.now.subtitle);
    }
    $('#p-title').textContent = state.now.title || '';
    $('#p-sub').textContent = state.now.subtitle || '';
    syncNext();
}

// --- sohbet -----------------------------------------------------------------------------
function atBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 60; }

function addChat({ id, username, color, message, time }, history = false) {
    const box = $('#messages');
    const stick = atBottom(box);
    const mine = id === socket.id;
    const cont = state.lastSender === id;
    state.lastSender = id;
    box.append(h('div', { class: 'msg' + (mine ? ' me' : '') + (cont ? ' cont' : '') },
        avatar(username, color),
        h('div', { class: 'msg-body' },
            h('span', { class: 'msg-name', style: { color: color || colorFor(username) } }, username),
            h('div', { class: 'msg-bubble' }, message),
            h('span', { class: 'msg-time' }, time || ''))));
    if (stick || mine) box.scrollTop = box.scrollHeight;
    if (!mine && !history) {
        if (!chatVisible()) { state.unreadChat++; renderBadges(); }
        floatChat(username, color, message);
    }
    if (history) box.scrollTop = box.scrollHeight;
}

/** Sistem mesajı. Sunucudan gelen metin sunucuda kaçırılmıştır (events.py _esc). */
function addSystem(html, trusted = false) {
    const box = $('#messages');
    const el = h('div', { class: 'sys' });
    if (trusted) el.innerHTML = html; else el.textContent = html;
    const stick = atBottom(box);
    box.append(el);
    state.lastSender = null;
    if (stick) box.scrollTop = box.scrollHeight;
}

/** Sinema düzeni (video her yeri kaplar, panel çekmece): yatay telefon ya da
 *  tam ekran. Karar cihaz ekranına göre verilir; klavye görünen alanı
 *  küçültünce tablet yanlışlıkla sinemaya geçmesin. */
function isDrawerLayout() {
    const app = $('#app');
    return app.classList.contains('is-fs') || app.classList.contains('cinema');
}

function layout() {
    const short = Math.min(screen.width, screen.height) < 560;
    const landscape = screen.orientation ? /landscape/.test(screen.orientation.type) : innerWidth > innerHeight;
    const cinema = short && landscape;
    const app = $('#app');
    if (app.classList.contains('cinema') !== cinema) {
        app.classList.toggle('cinema', cinema);
        if (!cinema && !app.classList.contains('is-fs')) app.classList.remove('drawer-open');
    }
}

function floatChat(name, color, text) {
    const app = $('#app');
    if (!isDrawerLayout() || app.classList.contains('drawer-open')) return;
    const box = $('#float-chat');
    const m = h('div', { class: 'float-msg' }, h('b', { style: { color: color || colorFor(name) } }, name), text);
    box.append(m);
    while (box.children.length > 3) box.firstChild.remove();
    setTimeout(() => m.classList.add('out'), 6000);
    setTimeout(() => m.remove(), 6700);
}

function chatVisible() {
    const app = $('#app');
    const tabOn = $('.tab[data-tab=chat]').classList.contains('on');
    if (!tabOn) return false;
    return !isDrawerLayout() || app.classList.contains('drawer-open');
}

function renderBadges() {
    const set = (tab, n) => { const b = $(`.tab[data-tab=${tab}] .badge`); b.hidden = !n; b.textContent = n > 9 ? '9+' : String(n); };
    set('chat', state.unreadChat);
    set('library', state.isLeader ? state.suggestions : 0);
    $('#side-dot').hidden = !(state.unreadChat || (state.isLeader && state.suggestions));
}

// --- görüşme durumu -------------------------------------------------------------------
function renderCallState() {
    const st = call.state();
    const box = $('#call-actions');
    if (st === 'idle') {
        box.replaceChildren(
            h('button', { class: 'btn primary', onclick: () => call.request(true) }, icon('cam'), 'Görüntülü ara'),
            h('button', { class: 'btn', onclick: () => call.request(false) }, icon('phone'), 'Sesli ara'));
    } else if (st === 'calling') {
        box.replaceChildren(h('button', { class: 'btn danger block', onclick: () => call.cancel() }, icon('hangup'), 'Aranıyor… iptal'));
    } else if (st === 'active') {
        box.replaceChildren(h('button', { class: 'btn danger block', onclick: () => call.end(true) }, icon('hangup'), 'Görüşmeyi bitir'));
    } else box.replaceChildren();
    if (st === 'ringing') openSheet('incoming', [call.incomingCard()]);
    else if (sheetKind === 'incoming') closeSheet();
}

// --- sayfa (sheet) ----------------------------------------------------------------------
let sheetKind = null;
function openSheet(kind, children) {
    sheetKind = kind;
    const s = $('#sheet');
    s.replaceChildren(...children.filter(Boolean));
    s.hidden = false; $('#scrim').hidden = false;
}
function closeSheet() {
    if (sheetKind === 'incoming' && call.incoming) call.reject();
    sheetKind = null;
    $('#sheet').hidden = true; $('#scrim').hidden = true;
}

/** Oda menüsündeki "Oynatıcı ayarları": oynatıcının kendi çark menüsü. */
function openPlayerSheet() {
    closeSheet();
    player.openMenu();
}

async function openInvite() {
    let url = state.publicUrl;
    try {
        const r = await fetch('/api/state', { cache: 'no-store' });
        const d = await r.json();
        url = d.publicUrl ? d.publicUrl + '/i' : '';
    } catch (_) { }
    if (!url && !/^(127\.|localhost)/.test(location.hostname)) url = location.origin + '/i';
    state.publicUrl = url;
    const kids = [h('h2', {}, 'Arkadaşını davet et')];
    if (url) {
        kids.push(h('p', { class: 'muted' }, 'Bu bağlantıyı gönder; tarayıcıdan açıp adını yazınca odaya girer, uygulama gerekmez.'),
            h('div', { class: 'invite-link' }, h('span', {}, url),
                h('button', { class: 'btn icon', 'aria-label': 'Kopyala', onclick: async () => { if (await copyText(url)) toast('Bağlantı kopyalandı.', 'ok'); } }, icon('copy'))),
            h('button', { class: 'btn primary block', style: { marginTop: '14px' }, onclick: () => shareText(url, FEATURE_NAME) }, icon('share'), 'Paylaş'));
    } else {
        kids.push(h('p', { class: 'muted' }, 'Uzaktan bağlantı hâlâ kuruluyor. Birkaç saniye sonra tekrar dene.'),
            h('button', { class: 'btn block', onclick: openInvite }, 'Yenile'));
    }
    openSheet('invite', kids);
}

function openMenu() {
    const leader = state.isLeader;
    const opt = (ic, label, fn, cls = '') => h('button', { class: 'opt ' + cls, onclick: () => { closeSheet(); fn(); } }, icon(ic), label);
    const kids = [h('h2', {}, 'Oda')];
    kids.push(h('div', { class: 'opt-list' },
        opt('users', 'Adımı değiştir', renameSheet),
        opt('gear', 'Oynatıcı ayarları', openPlayerSheet),
        leader ? opt('film', 'Videoyu kapat', () => socket.emit('admin-command', { roomId, command: 'clearvideo' })) : null,
        leader ? opt('chat', 'Sohbeti temizle', () => socket.emit('admin-command', { roomId, command: 'clearall' })) : null,
        leader ? opt('inbox', 'Duyuru yap', announceSheet) : null,
        inApp && app && app.minimize
            ? (app.role && app.role() === 'guest'
                ? opt('leave', 'Odadan çık', () => app.minimize())
                : opt('back', 'Odayı arka plana al', () => app.minimize()))
            : null,
        inApp && app && app.endRoom && leader ? opt('leave', 'Odayı kapat', () => app.endRoom(), 'danger') : null));
    openSheet('menu', kids);
}

function renameSheet() {
    const f = h('input', { class: 'field big', maxlength: 24, value: me.name, 'aria-label': 'Adın' });
    const save = () => {
        const v = f.value.trim().slice(0, 24); if (!v) return;
        me.name = v; store.set('oda.name', v);
        socket.emit('change-nick', { roomId, newName: v, color: me.color });
        closeSheet();
    };
    f.addEventListener('keydown', e => { if (e.key === 'Enter') save(); });
    openSheet('rename', [h('h2', {}, 'Adın'), f,
        h('div', { class: 'swatches', style: { margin: '14px 0' } }, ...PALETTE.map(c => h('button', {
            class: 'swatch' + (c === me.color ? ' on' : ''), style: { background: c }, 'aria-label': 'Renk',
            onclick: e => { me.color = c; store.set('oda.color', c); $$('.swatch', e.target.parentNode).forEach(s => s.classList.toggle('on', s === e.target)); }
        }))),
        h('button', { class: 'btn primary block', onclick: save }, 'Kaydet')]);
    setTimeout(() => f.focus(), 50);
}

function announceSheet() {
    const f = h('input', { class: 'field big', maxlength: 200, placeholder: 'Herkese duyurulacak metin', 'aria-label': 'Duyuru' });
    const send = () => { const v = f.value.trim(); if (v) socket.emit('admin-command', { roomId, command: 'announce', args: [v] }); closeSheet(); };
    f.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
    openSheet('announce', [h('h2', {}, 'Duyuru'), f, h('button', { class: 'btn primary block', style: { marginTop: '14px' }, onclick: send }, 'Gönder')]);
    setTimeout(() => f.focus(), 50);
}

// --- düzen, sekmeler, tam ekran ----------------------------------------------------------
function selectTab(name) {
    $$('.tab').forEach(t => t.classList.toggle('on', t.dataset.tab === name));
    $$('.panel').forEach(p => p.classList.toggle('on', p.dataset.panel === name));
    if (name === 'chat') {
        state.unreadChat = 0; renderBadges();
        const box = $('#messages'); box.scrollTop = box.scrollHeight;
    }
}

function openDrawer(tab) {
    const app = $('#app');
    if (tab) selectTab(tab);
    app.classList.add('drawer-open');
    if ($('.tab[data-tab=chat]').classList.contains('on')) { state.unreadChat = 0; renderBadges(); }
}

function fsElement() { return document.fullscreenElement || document.webkitFullscreenElement; }

function toggleFullscreen() {
    const root = $('#app');
    if (fsElement()) {
        (document.exitFullscreen || document.webkitExitFullscreen).call(document);
        return;
    }
    const req = root.requestFullscreen || root.webkitRequestFullscreen;
    if (!req) return toast('Bu cihaz tam ekranı desteklemiyor.');
    Promise.resolve(req.call(root)).then(() => {
        if (screen.orientation && screen.orientation.lock) screen.orientation.lock('landscape').catch(() => { });
    }).catch(() => toast('Tam ekrana geçilemedi.'));
}

function onFsChange() {
    const on = !!fsElement();
    $('#app').classList.toggle('is-fs', on);
    if (!on) { $('#app').classList.remove('drawer-open'); if (screen.orientation && screen.orientation.unlock) try { screen.orientation.unlock(); } catch (_) { } }
    $('[data-act=fs]').replaceChildren(icon(on ? 'fs-exit' : 'fs'));
}

function bindUi(emit) {
    $$('.tab').forEach(t => t.addEventListener('click', () => selectTab(t.dataset.tab)));
    $('#side-close').addEventListener('click', () => $('#app').classList.remove('drawer-open'));
    $('[data-act=side]').addEventListener('click', () => openDrawer(state.isLeader && state.suggestions ? 'library' : 'chat'));
    $('[data-act=fs]').addEventListener('click', toggleFullscreen);
    $('#invite-btn').addEventListener('click', openInvite);
    $('#menu-btn').addEventListener('click', openMenu);
    $('#scrim').addEventListener('click', closeSheet);
    $('#empty-pick').addEventListener('click', () => {
        if (isDrawerLayout()) openDrawer('library'); else selectTab('library');
        setTimeout(() => { const s = $('#library input[type=search]'); if (s && !s.closest('[hidden]')) s.focus(); }, 80);
    });
    layout();
    addEventListener('resize', layout);
    if (screen.orientation) screen.orientation.addEventListener('change', layout);
    document.addEventListener('fullscreenchange', onFsChange);
    document.addEventListener('webkitfullscreenchange', onFsChange);

    $('#composer').addEventListener('submit', e => {
        e.preventDefault();
        const f = $('#chat-input');
        const v = f.value.trim();
        if (!v) return;
        emit('chat-message', { message: v });
        f.value = '';
    });

    // Klavye kısayolları (yazı alanındayken devre dışı).
    document.addEventListener('keydown', e => {
        if (e.target.closest('input, textarea') || e.ctrlKey || e.metaKey || e.altKey) return;
        if (e.key === 'Escape') { if (!$('#sheet').hidden) closeSheet(); else $('#app').classList.remove('drawer-open'); return; }
        const k = e.key.toLowerCase();
        if (k === ' ' || k === 'k') { e.preventDefault(); player.toggle(); }
        else if (k === 'arrowright') player.nudge(10);
        else if (k === 'arrowleft') player.nudge(-10);
        else if (k === 'f') toggleFullscreen();
        else if (k === 'm') player.video.muted = !player.video.muted;
        else if (k === 'c') player.toggleSub();
        else return;
        player.showControls(true);
    });

    // Sayfa arka plana düşünce (uygulama küçültüldü) kalp atışı sürer; geri
    // gelince sohbet sayacını sıfırlamak için görünürlüğü izle.
    document.addEventListener('visibilitychange', () => { if (!document.hidden && chatVisible()) { state.unreadChat = 0; renderBadges(); } });
    renderCallState();
    applyControl();
}

// --- açılış -----------------------------------------------------------------------------
if (!roomId) {
    location.replace('/i');
} else if (me.name && (params.get('name') || hostToken)) {
    // Uygulamadan gelen (ad ve anahtar adreste): isim ekranı yok.
    start();
} else {
    showJoin();
}
