// İçerik paneli: odadan çıkmadan arama, bölüm seçme, link, öneriler.
//
// Ev sahibi seçtiğini doğrudan oynatır; misafirin seçimi ev sahibine öneri
// olarak gider (yetkiyi sunucu denetler, burası yalnız doğru düğmeyi gösterir).

import { $, h, icon, toast, debounce, imgUrl, fill } from './ui.js';

export class Library {
    constructor({ socket, roomId, root, isLeader, onBadge }) {
        this.socket = socket;
        this.roomId = roomId;
        this.root = root;
        this.isLeader = isLeader;
        this.onBadge = onBadge || (() => { });
        this.catalog = false;
        this.now = {};
        this.suggestions = [];
        this.reqId = 0;
        this.detail = null;
        this.season = null;
        this._build();
        this._bindSocket();
    }

    _emit(ev, data = {}) { this.socket.emit(ev, { roomId: this.roomId, ...data }); }

    _build() {
        const r = this.root;
        this.nowBox = h('div', { class: 'now-card', hidden: true });
        this.sugBox = h('section', { class: 'lib-section', hidden: true },
            h('h3', { class: 'lib-h' }, icon('inbox'), 'Öneriler'),
            this.sugList = h('div', { class: 'sug-list' }));
        this.searchIn = h('input', {
            class: 'field', type: 'search', placeholder: 'Dizi ya da film ara…',
            autocomplete: 'off', enterkeyhint: 'search', 'aria-label': 'Katalogda ara'
        });
        this.searchBox = h('section', { class: 'lib-section', hidden: true },
            h('div', { class: 'field-wrap' }, icon('search', 'field-icon'), this.searchIn),
            this.status = h('div', { class: 'lib-status muted' }),
            this.results = h('div', { class: 'grid' }));
        this.detailBox = h('section', { class: 'lib-detail', hidden: true });
        this.linkIn = h('input', {
            class: 'field', type: 'url', inputmode: 'url', placeholder: 'YouTube, .m3u8 ya da .mp4 bağlantısı',
            autocomplete: 'off', 'aria-label': 'Video bağlantısı'
        });
        this.linkBtn = h('button', { class: 'btn primary', onclick: () => this._sendLink() }, 'Aç');
        const linkBox = h('section', { class: 'lib-section' },
            h('h3', { class: 'lib-h' }, icon('link'), 'Bağlantı ile'),
            h('div', { class: 'row gap' }, this.linkIn, this.linkBtn));
        this.linkIn.addEventListener('keydown', e => { if (e.key === 'Enter') this._sendLink(); });
        r.append(this.nowBox, this.sugBox, this.searchBox, this.detailBox, linkBox);

        const run = debounce(q => this._search(q), 400);
        this.searchIn.addEventListener('input', () => run(this.searchIn.value.trim()));
        this.searchIn.addEventListener('keydown', e => { if (e.key === 'Enter') this._search(this.searchIn.value.trim()); });
    }

    // --- rol ve durum ---------------------------------------------------------
    setCatalog(on) { this.catalog = !!on; this.searchBox.hidden = !on; }

    refreshRole() {
        const leader = this.isLeader();
        this.linkBtn.textContent = leader ? 'Aç' : 'Öner';
        this.sugBox.hidden = !leader || !this.suggestions.length;
        this._renderNow();
        if (this.detail) this._renderDetail();
    }

    setNow(now) {
        this.now = now || {};
        this._renderNow();
        // Açık bölüm listesinde oynayanı işaretle.
        if (this.detail && this.detail.seasons) this._renderDetail();
    }

    setLoading(on) { this.root.classList.toggle('loading', !!on); }

    _renderNow() {
        const n = this.now;
        this.nowBox.hidden = !n || !n.title;
        if (this.nowBox.hidden) return;
        const leader = this.isLeader();
        fill(this.nowBox,
            n.poster ? h('img', { class: 'now-poster', src: imgUrl(n.poster), alt: '', loading: 'lazy' })
                : h('div', { class: 'now-poster ph' }, icon(n.kind === 'youtube' ? 'play' : 'film')),
            h('div', { class: 'now-text' },
                h('div', { class: 'eyebrow' }, 'Şu an'),
                h('div', { class: 'now-title' }, n.title),
                n.subtitle ? h('div', { class: 'muted' }, n.subtitle) : null),
            leader && n.hasNext ? h('button', { class: 'btn primary sm', onclick: () => this._emit('next-episode') },
                icon('next'), 'Sonraki') : null);
    }

    // --- arama -------------------------------------------------------------------
    _search(q) {
        if (!this.catalog) return;
        if (q.length < 2) { this.results.replaceChildren(); this.status.textContent = ''; return; }
        const id = ++this.reqId;
        this.status.textContent = 'Aranıyor…';
        this._emit('catalog-search', { q, reqId: id });
    }

    _renderResults(items) {
        this.status.textContent = items.length ? '' : 'Sonuç bulunamadı.';
        this.results.replaceChildren(...items.map(it => h('button', {
            class: 'card', onclick: () => this._open(it), 'aria-label': it.title
        },
            it.poster ? h('img', { class: 'card-poster', src: imgUrl(it.poster), alt: '', loading: 'lazy' })
                : h('div', { class: 'card-poster ph' }, icon('film')),
            h('div', { class: 'card-title' }, it.title),
            h('div', { class: 'card-meta' }, [it.kind === 'film' ? 'Film' : 'Dizi', it.year].filter(Boolean).join(' · ')))));
    }

    _open(item) {
        this.detail = { ...item, seasons: null };
        this.season = null;
        this._renderDetail();
        this._emit('catalog-detail', { ref: item.ref, reqId: ++this.reqId });
    }

    _closeDetail() {
        this.detail = null;
        this.detailBox.hidden = true;
        this.searchBox.hidden = !this.catalog;
    }

    _renderDetail() {
        const d = this.detail; if (!d) return;
        this.searchBox.hidden = true;
        this.detailBox.hidden = false;
        const leader = this.isLeader();
        const head = h('div', { class: 'detail-head' },
            h('button', { class: 'btn icon ghost', 'aria-label': 'Geri', onclick: () => this._closeDetail() }, icon('back')),
            d.poster ? h('img', { class: 'detail-poster', src: imgUrl(d.poster), alt: '' }) : null,
            h('div', { class: 'detail-text' },
                h('div', { class: 'detail-title' }, d.title),
                h('div', { class: 'muted' }, [d.kind === 'film' ? 'Film' : 'Dizi', d.year].filter(Boolean).join(' · '))));
        const body = [];
        if (!d.seasons) body.push(h('div', { class: 'lib-status muted' }, 'Bölümler yükleniyor…'));
        else {
            const seasons = d.seasons;
            if (this.season === null) this.season = seasons[0] ? seasons[0].season : null;
            if (d.kind !== 'film' && seasons.length > 1) {
                body.push(h('div', { class: 'chips', role: 'tablist' }, ...seasons.map(s => h('button', {
                    class: 'chip' + (s.season === this.season ? ' on' : ''), role: 'tab',
                    'aria-selected': String(s.season === this.season),
                    onclick: () => { this.season = s.season; this._renderDetail(); }
                }, `${s.season}. Sezon`))));
            }
            const cur = seasons.find(s => s.season === this.season) || seasons[0];
            body.push(h('div', { class: 'ep-list' }, ...(cur ? cur.episodes : []).map(ep => {
                const playing = this.now && this.now.ref === ep.ref;
                return h('div', { class: 'ep' + (playing ? ' on' : '') },
                    h('span', { class: 'ep-no' }, d.kind === 'film' ? icon('film') : String(ep.number)),
                    h('span', { class: 'ep-label' }, ep.label),
                    h('button', {
                        class: 'btn sm ' + (leader ? 'primary' : 'ghost'),
                        onclick: () => this._pick(d, ep)
                    }, icon(leader ? 'play' : 'send'), leader ? 'Oynat' : 'Öner'));
            })));
        }
        this.detailBox.replaceChildren(head, ...body);
    }

    _pick(d, ep) {
        this._emit('catalog-play', {
            ref: ep.ref, title: d.title, poster: d.poster,
            subtitle: d.kind === 'film' ? '' : ep.label
        });
        if (this.isLeader()) toast('Açılıyor…');
    }

    _sendLink() {
        const url = this.linkIn.value.trim();
        if (!/^https?:\/\//i.test(url)) { this.linkIn.focus(); return toast('Geçerli bir bağlantı yapıştır.', 'warn'); }
        this._emit('set-video', { videoUrl: url });
        this.linkIn.value = '';
        if (this.isLeader()) toast('Açılıyor…');
    }

    // --- öneriler ------------------------------------------------------------------
    setSuggestions(items) {
        this.suggestions = items || [];
        this.onBadge(this.suggestions.length);
        this.sugBox.hidden = !this.isLeader() || !this.suggestions.length;
        this.sugList.replaceChildren(...this.suggestions.map(s => h('div', { class: 'sug' },
            s.item.poster ? h('img', { class: 'sug-poster', src: imgUrl(s.item.poster), alt: '' })
                : h('div', { class: 'sug-poster ph' }, icon(s.item.kind === 'hayalet' ? 'film' : 'link')),
            h('div', { class: 'sug-text' },
                h('div', { class: 'sug-title' }, s.item.title || s.item.url || 'Öneri'),
                h('div', { class: 'muted' }, [s.item.subtitle, `${s.by} önerdi`].filter(Boolean).join(' · '))),
            h('button', { class: 'btn icon ghost', 'aria-label': 'Kaldır', onclick: () => this._emit('suggestion-dismiss', { id: s.id }) }, icon('close')),
            h('button', { class: 'btn primary sm', onclick: () => this._emit('suggestion-accept', { id: s.id }) }, icon('play'), 'Aç'))));
    }

    _bindSocket() {
        const s = this.socket;
        s.on('catalog-results', ({ reqId, items }) => { if (reqId === this.reqId) this._renderResults(items || []); });
        s.on('catalog-detail', data => {
            if (data.reqId !== this.reqId || !this.detail) return;
            this.detail = { ...this.detail, ...data };
            this._renderDetail();
        });
        s.on('catalog-error', ({ reqId, message }) => {
            if (reqId && reqId !== this.reqId) return;
            this.status.textContent = '';
            if (this.detail && !this.detail.seasons) this._closeDetail();
            toast(message || 'Bir şeyler ters gitti.', 'err');
        });
        s.on('suggestions', ({ items }) => this.setSuggestions(items));
        s.on('suggestion-sent', ({ title }) => toast(`Önerin ev sahibine gitti: ${title || ''}`.trim(), 'ok'));
    }
}
