// Ortak yardımcılar: güvenli DOM üretimi, bildirimler, avatar, biçimler.
//
// Kural: kullanıcıdan gelen hiçbir metin innerHTML'e girmez. `h()` metni
// her zaman textContent olarak yazar. Tek istisna sunucunun kendisinin
// kaçırarak ürettiği sistem mesajları (bkz. oda.js addSystem).

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** h('button', {class: 'x', onclick: fn}, 'metin', altEleman) */
export function h(tag, attrs = {}, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
        if (v === undefined || v === null || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
        else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
        else if (k === 'dataset') Object.assign(el.dataset, v);
        else el.setAttribute(k, v === true ? '' : String(v));
    }
    for (const c of children.flat()) {
        if (c === null || c === undefined || c === false) continue;
        el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
}

/** replaceChildren'in boş değerleri atan hâli. Tarayıcı null'ı "null" metni
 *  olarak basıyor (ekranda görüldü: "Şu an" kartında ve avatar dizisinde). */
export function fill(el, ...kids) {
    el.replaceChildren(...kids.flat().filter(k => k !== null && k !== undefined && k !== false));
    return el;
}

/** Sprite'taki bir ikon (index.html içindeki <symbol id="i-...">). */
export function icon(name, cls = '') {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('class', ('icon ' + cls).trim());
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(ns, 'use');
    use.setAttribute('href', '#i-' + name);
    svg.append(use);
    return svg;
}

// Profil renkleri: uygulamanın vurgu paletiyle aynı aile.
export const PALETTE = ['#EA5814', '#2F80ED', '#27AE60', '#9B51E0', '#E84393',
    '#D63031', '#00A3A3', '#C9A227'];

export function colorFor(name) {
    let x = 0;
    for (const ch of String(name || '?')) x = (x * 31 + ch.codePointAt(0)) >>> 0;
    return PALETTE[x % PALETTE.length];
}

export function initials(name) {
    const parts = String(name || '?').trim().split(/\s+/).filter(Boolean);
    const a = parts[0] ? Array.from(parts[0])[0] : '?';
    const b = parts[1] ? Array.from(parts[1])[0] : '';
    return (a + b).toLocaleUpperCase('tr-TR');
}

export function avatar(name, color, size = '') {
    return h('span', {
        class: ('avatar ' + size).trim(),
        style: { background: color || colorFor(name) },
        title: name
    }, initials(name));
}

export function fmtTime(sec) {
    if (!isFinite(sec) || sec < 0) sec = 0;
    sec = Math.floor(sec);
    const hh = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60), ss = sec % 60;
    const p = n => String(n).padStart(2, '0');
    return hh ? `${hh}:${p(mm)}:${p(ss)}` : `${mm}:${p(ss)}`;
}

let toastBox = null;
/** Kısa bildirim. kind: '' | 'ok' | 'warn' | 'err' */
export function toast(message, kind = '') {
    toastBox = toastBox || $('#toasts');
    if (!toastBox) return;
    // Aynı uyarı zaten ekrandaysa tekrar etme (hızlı iki dokunuş iki kopya basıyordu).
    const last = toastBox.lastElementChild;
    if (last && !last.classList.contains('out') && last.textContent === String(message)) return;
    const t = h('div', { class: 'toast ' + kind, role: 'status' }, message);
    toastBox.append(t);
    // Aynı anda en fazla üç bildirim: sohbet hızlıyken ekranı kaplamasın.
    while (toastBox.children.length > 3) toastBox.firstChild.remove();
    setTimeout(() => t.classList.add('out'), 2600);
    setTimeout(() => t.remove(), 3000);
}

/** Güvenli localStorage: gizli pencere / engelli depolamada da çalışsın. */
export const store = {
    get(k, d = null) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (_) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (_) { } },
};

export function debounce(fn, ms) {
    let t = null;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/** Görsel adresi: posterler bazı sitelerde yalnız TLS taklidiyle alınıyor. */
export function imgUrl(u) {
    return u ? '/api/img?u=' + encodeURIComponent(u) : '';
}

/** Uygulama içi köprü (Android WebView'de var, tarayıcıda yok). */
export const app = window.HayaletApp || null;

export async function copyText(text) {
    if (app && app.copy) { app.copy(text); return true; }
    try { await navigator.clipboard.writeText(text); return true; }
    catch (_) {
        const ta = h('textarea', { style: { position: 'fixed', opacity: '0' } }, text);
        document.body.append(ta); ta.select();
        const ok = document.execCommand('copy'); ta.remove();
        return ok;
    }
}

export async function shareText(text, title) {
    if (app && app.share) { app.share(text); return true; }
    if (navigator.share) {
        try { await navigator.share({ title, text, url: text }); return true; } catch (_) { return false; }
    }
    return copyText(text);
}
