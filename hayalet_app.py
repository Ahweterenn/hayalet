"""Android arayuzu ile hayalet boru hatti arasindaki koprü.

Java tarafi buradaki fonksiyonlari cagirir; hepsi **JSON string** dondurur
(Chaquopy'de en az surtunmeli tip). Is mantigi burada YOK — her sey mevcut
adapter/proxy katmanina delege edilir.

Oynatma icin numara su: masaustundeki yerel HLS proxy'si (core/proxy.py) telefonda
da aynen calisir. ExoPlayer 127.0.0.1'e baglanir, boylece referer propagasyonu,
.jpg kiligindaki segmentler, coklu ses ve altyazi enjeksiyonu ZATEN cozulmus
olarak gelir — tarayici/hls.js sayfasina hic ihtiyac yok.
"""

import json
import os
import traceback
import concurrent.futures

_ctx = {}          # site adi -> (Network, SessionState)
_results = []      # son arama sonuclari (Series)
_episodes = []     # son bolum listesi (Episode)
_series = None     # bolum listesinin ait oldugu Series
_proxy = None      # calisan HLSProxy (tek seferde bir tane)


def _err(exc):
    return json.dumps({"error": "%s: %s" % (type(exc).__name__, exc),
                       "trace": traceback.format_exc()[-800:]})


def init(cache_dir, download_dir):
    """Yazilabilir dizinleri bildirir. hayalet.config import edilmeden ONCE cagrilmali."""
    os.environ["HAYALET_CACHE_DIR"] = cache_dir
    os.environ["HAYALET_DOWNLOAD_DIR"] = download_dir
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(download_dir, exist_ok=True)
    return json.dumps({"ok": True})


def _connect_all():
    """Kayitli tum sitelere paralel baglanir (cli.main'in interaktif modu gibi).
    Siteler tamamen bagimsiz Network/SessionState kullanir, ortak degisken yok."""
    global _ctx
    if _ctx:
        return
    from concurrent.futures import ThreadPoolExecutor

    from hayalet.core import personas
    from hayalet.core.network import Network
    from hayalet.core.session import SessionState
    from hayalet.core.sites import SITES
    import hayalet.sites  # noqa: F401

    def one(name):
        adapter = SITES[name]
        persona = personas.random_persona()
        session = SessionState(base_url=adapter.known_domain,
                               referer=adapter.known_domain,
                               user_agent=persona["user_agent"],
                               impersonate=persona["impersonate"])
        net = Network(session)
        adapter.resolve_domain(net, session, override=None, use_cache=True)
        return name, (net, session)

    with ThreadPoolExecutor(max_workers=len(SITES)) as ex:
        for fut in [ex.submit(one, n) for n in SITES]:
            try:
                name, pair = fut.result()
                _ctx[name] = pair
            except Exception:
                pass  # bir site cokerse digeri calismaya devam etsin


def search(term):
    """Tum sitelerde ayni anda arar, tek birlesik liste dondurur.

    Is mantigi `hayalet.core.sites`te: search_all her sitede once sorgunun
    kendisini, tutmazsa yazim varyantlarini (tire/bosluk/Turkce karakter,
    'spiderman'->'spider man', on-ek) paralel dener ve birlesik listeyi
    benzerlige gore siralar. Burada kopya mantik YOK — masaustu ve APK birebir
    ayni aramayi kullansin diye.
    """
    global _results
    try:
        _connect_all()
        if not _ctx:
            return json.dumps({"error": "Hicbir siteye baglanilamadi"})

        from hayalet.core import sites

        merged = sites.search_all(_ctx, term)
        if not merged:
            merged = sites.suggest_all(_ctx, term)

        _results = merged
        return json.dumps({"results": _as_items(merged, 0)})
    except Exception as e:
        return _err(e)


def _as_items(series_list, start):
    """Java tarafinin bekledigi sade sozluk listesi. `idx` GLOBAL: `_results`
    icindeki konum — episodes(idx) bunu kullaniyor."""
    from hayalet.core import catalog
    return [{"idx": start + i, "name": s.name, "site": s.site, "type": s.type,
             "movie": bool(catalog.is_movie(s)), "year": s.year,
            "rating": s.rating, "poster": s.poster_url,
            "backdrop": s.backdrop_url, "description": s.description}
            for i, s in enumerate(series_list)]


def home():
    """Ana sayfa raflari (site ana sayfasindan kazinir, arama yapilmaz)."""
    global _results
    try:
        _connect_all()
        if not _ctx:
            return json.dumps({"error": "Hicbir siteye baglanilamadi"})
        from hayalet.core import sites

        rows = sites.home_rows(_ctx)
        flat, out = [], []
        for row in rows:
            items = row.get("items") or []
            if not items:
                continue
            out.append({"title": row.get("title", ""),
                        "items": _as_items(items, len(flat))})
            flat.extend(items)
        _results = flat
        return json.dumps({"rows": out})
    except Exception as e:
        return _err(e)


def browse(kind):
    """Diziler / Filmler sekmesi: katalog listeleme sayfasi (arama degil)."""
    global _results
    try:
        _connect_all()
        if not _ctx:
            return json.dumps({"error": "Hicbir siteye baglanilamadi"})
        from hayalet.core import sites

        items = sites.browse(_ctx, str(kind))
        _results = items
        return json.dumps({"results": _as_items(items, 0)})
    except Exception as e:
        return _err(e)


def genres():
    """Tür listesi (destekleyen sitelerden). Kategori sekmesinin çipleri."""
    try:
        _connect_all()
        from hayalet.core.sites import SITES
        out = []
        for name, (net, session) in _ctx.items():
            fn = getattr(SITES[name], "genres", None)
            if fn is None:
                continue
            try:
                for g in fn(net, session):
                    out.append({"name": g["name"], "url": g["url"], "site": name})
            except Exception:
                continue
        return json.dumps({"genres": out})
    except Exception as e:
        return _err(e)


def by_genre(site, url):
    """Bir tür sayfasındaki yapımlar."""
    global _results
    try:
        _connect_all()
        from hayalet.core.sites import SITES
        net, session = _ctx[site]
        items = SITES[site].by_genre(net, session, url)
        _results = items
        return json.dumps({"results": _as_items(items, 0)})
    except Exception as e:
        return _err(e)


def episodes(series_idx):
    """Secilen yapimin bolumleri. Film ise tek elemanli liste doner."""
    global _episodes, _series
    try:
        from hayalet.core.sites import SITES
        s = _results[int(series_idx)]
        net, session = _ctx[s.site]
        from hayalet.core.sites import enrich_series
        enrich_series(net, session, s)
        eps = SITES[s.site].get_episodes(net, session, s)
        _episodes, _series = eps, s
        from hayalet.core import catalog
        return json.dumps({
            # Yapim detayi ekrani bunlari gosteriyor: kapak icin ad+tur,
            # "Kaynak: ..." satiri icin site.
            "name": s.name, "type": s.type, "site": s.site,
            "movie": bool(catalog.is_movie(s)),
            "poster": s.poster_url,
            "year": s.year, "rating": s.rating, "description": s.description,
            "episodes": [{"idx": i, "season": e.season, "number": e.number,
                          "label": e.label} for i, e in enumerate(eps)]})
    except Exception as e:
        return _err(e)


def resume_posters(cards_json):
    """Eski devam kayıtlarının posterlerini detay sayfasından yeniler."""
    try:
        from hayalet.core.models import Series
        from hayalet.core.sites import SITES, enrich_series
        cards = json.loads(cards_json)
        out = []
        _connect_all()
        for card in cards:
            old_poster = card.get("poster", "")
            site = card.get("site", "")
            print("HayaletResume: kart site=%r name=%r slug=%r poster=%r" % (
                site, card.get("name", ""), card.get("slug", ""),
                card.get("poster", "")), flush=True)
            if site not in _ctx:
                print("HayaletResume: site context yok: %r" % site, flush=True)
                if old_poster:
                    out.append({"key": card.get("key", ""), "poster": old_poster})
                continue
            slug = card.get("slug", "")
            s = Series(name=card.get("name", ""), slug=slug,
                       type=card.get("type", "Series"), site=site,
                       poster_url="")
            net, session = _ctx[site]
            if slug:
                enrich_series(net, session, s)
            else:
                matches = SITES[site].search(net, session, s.name)
                if matches:
                    s = matches[0]
            print("HayaletResume: sonuç poster=%r" % s.poster_url, flush=True)
            candidates = []
            if s.poster_url and "/artist/" not in s.poster_url.lower() and "/backdrop/" not in s.poster_url.lower():
                candidates.append((s.name, s.poster_url, site))
            other_sites = [
                (other_site, other_net, other_session)
                for other_site, (other_net, other_session) in _ctx.items()
                if other_site != site
            ]

            def find_other(item):
                other_site, other_net, other_session = item
                try:
                    matches = SITES[other_site].search(other_net, other_session, s.name)
                    return [
                        (match.name, match.poster_url, other_site)
                        for match in matches[:4] if match.poster_url and "/artist/" not in match.poster_url.lower()
                    ]
                except Exception:
                    return []

            # Bir sitedeki düşük kalite/eksik kapak diğer sitelerin sonucuyla
            # aynı anda karşılaştırılır; seri seri beklemek açılışı yavaşlatır.
            if other_sites:
                with concurrent.futures.ThreadPoolExecutor(
                        max_workers=len(other_sites)) as executor:
                    for found in executor.map(find_other, other_sites):
                        candidates.extend(found)

            def poster_score(item):
                name, poster, candidate_site = item
                if not poster or "/artist/" in poster.lower():
                    return -100
                score = 0
                if "/poster/" in poster.lower() or "/cover/" in poster.lower():
                    score += 30
                if "/backdrop/" in poster.lower() or "banner" in poster.lower():
                    score -= 30
                if any(token in poster.lower() for token in ("@2x", "original", "full", "large")):
                    score += 15
                if name.casefold() == s.name.casefold():
                    score += 20
                return score

            best = max(candidates, key=poster_score, default=(s.name, "", site))
            if best[1] and best[1] != s.poster_url:
                print("HayaletResume: daha iyi alternatif poster=%r site=%r" %
                      (best[1], best[2]), flush=True)
            s.poster_url = best[1] or old_poster
            # Geçici ağ/site hatası eski çalışan posteri silmemeli.
            poster = s.poster_url or old_poster
            if poster:
                out.append({"key": card.get("key", ""), "poster": poster})
        return json.dumps({"cards": out})
    except Exception as e:
        return _err(e)


def result_posters(indices_json):
    """Arama sonuçlarında kart HTML'inde eksik kalan posterleri tamamlar."""
    try:
        from hayalet.core.sites import enrich_series
        indices = json.loads(indices_json)
        out = []
        for idx in indices:
            s = _results[int(idx)]
            net, session = _ctx[s.site]
            enrich_series(net, session, s)
            if s.poster_url:
                out.append({"idx": int(idx), "poster": s.poster_url})
        return json.dumps({"results": out})
    except Exception as e:
        return _err(e)


def _verified_stream(ep, series):
    """Kısa kaynakları eleyip tam bölümü veya mutabık yayın kurgusunu seçer."""
    from hayalet.core import catalog, verifier, query
    from hayalet.core.sites import SITES

    net, session = _ctx[series.site]
    merged = SITES[series.site].build_stream(net, session, ep, series)
    if catalog.is_movie(series):
        return merged, net, session

    primary_refs, secondary_refs = verifier.get_reference_runtimes(series.name, ep.season, ep.number)
    print(f"HayaletDebug: {series.name} S{ep.season}E{ep.number} referanslar -> primary: {primary_refs}, secondary: {secondary_refs}", flush=True)

    init_dur = verifier.get_stream_duration(net, merged)
    init_p_ok = any(abs(init_dur - ref) <= 12 for ref in primary_refs) if init_dur and primary_refs else False

    # 1. Hızlı yol: İlk tıklanan kaynak doğrudan TVMaze/IMDb birincil bölüm süresiyle uyuşuyorsa anında aç
    if init_p_ok:
        print(f"HayaletDebug: {series.site} birincil sureyle tam uyustu ({init_dur:.1f} dk), aninda aciliyor!", flush=True)
        return merged, net, session

    dur_text = f"{init_dur:.1f} dk" if init_dur else "bilinmiyor"
    print(f"HayaletDebug: {series.site} ({dur_text}) birincil sureyle ({primary_refs}) tam uyusmadi, alternatifler taraniyor...", flush=True)

    candidates = [(series.site, merged, net, session, init_dur, init_p_ok)]
    target_keys = query.keys(series.name) or {query.key(series.name)}

    # 2. Diğer sitelerdeki alternatifleri topla
    for site_name, (alt_net, alt_session) in _ctx.items():
        if site_name == series.site:
            continue
        try:
            adapter = SITES[site_name]
            results = adapter.search(alt_net, alt_session, series.name)
            if not results:
                for var in query.variants(series.name):
                    results = adapter.search(alt_net, alt_session, var)
                    if results:
                        break

            alt_series = next(
                (item for item in results
                 if query.keys(item.name).intersection(target_keys) or query.score(series.name, item.name) >= query.GOOD_SCORE),
                None,
            )
            if alt_series is None:
                continue

            alt_eps = adapter.get_episodes(alt_net, alt_session, alt_series)
            alt_ep = catalog.match_episode(alt_eps, ep.season, ep.number)
            if alt_ep is None:
                continue

            alt_merged = adapter.build_stream(alt_net, alt_session, alt_ep, alt_series)
            alt_dur = verifier.get_stream_duration(alt_net, alt_merged)
            alt_p_ok = any(abs(alt_dur - ref) <= 12 for ref in primary_refs) if alt_dur and primary_refs else False

            print(f"HayaletDebug: [{site_name}] Sure: {alt_dur:.1f} dk, primary_match: {alt_p_ok}", flush=True)
            candidates.append((site_name, alt_merged, alt_net, alt_session, alt_dur, alt_p_ok))

            # Eğer tam birincil süreyi yakalayan bir alternatif bulduysak (örn. Mezarlık'ta Diziyou 106.8 dk)
            # başka arama yapmaya gerek yok, en mükemmel kaynağı bulduk demektir!
            if alt_p_ok:
                print(f"HayaletDebug: [{site_name}] tam bolum suresiyle ({primary_refs} dk) eslesti, donuyorum!", flush=True)
                return alt_merged, alt_net, alt_session
        except Exception as e:
            print(f"HayaletDebug: [{site_name}] tarama hatasi: {e}", flush=True)
            continue

    # 3. Hiçbir site birincil süreyle eşleşmedi (La Casa de Papel gibi TV yayını vs. Netflix kurgusu)
    # Siteler arası mutabakat (consensus) var mı kontrol et:
    valid_candidates = [c for c in candidates if c[4] and c[4] >= 15]
    if len(valid_candidates) >= 2:
        consensus_groups = []
        for c1 in valid_candidates:
            grp = [c2 for c2 in valid_candidates if abs(c1[4] - c2[4]) <= 8]
            if len(grp) >= 2:
                consensus_groups.append(grp)
        if consensus_groups:
            best_group = max(consensus_groups, key=lambda g: (len(g), max(c[4] for c in g)))
            best = max(best_group, key=lambda c: c[4])
            print(f"HayaletDebug: Siteler arasi mutabakat bulundu ({len(best_group)} site ayni surede)! Secilen: {best[0]} ({best[4]:.1f} dk)", flush=True)
            return best[1], best[2], best[3]

    # 4. İkincil süre (genel dizi ortalaması) ile eşleşen varsa
    if secondary_refs:
        secondary_matches = [
            c for c in valid_candidates
            if any(abs(c[4] - ref) <= 12 for ref in secondary_refs)
        ]
        if secondary_matches:
            best = max(secondary_matches, key=lambda c: c[4])
            print(f"HayaletDebug: Ikincil dizi ortalamasi ile eslesti! Secilen: {best[0]} ({best[4]:.1f} dk)", flush=True)
            return best[1], best[2], best[3]

    # 5. Dış referans yoksa veya dış referans hatalıysa (örn. film kurgusu vs. dizi bölümü):
    # Adaylar arasında tam bölüm niteliğinde (>= 20 dk) video varsa en eksiksiz olanı aç
    if valid_candidates:
        longest = max(valid_candidates, key=lambda c: c[4])
        if longest[4] >= 20:
            print(f"HayaletDebug: Kaynak suresi tam bolum niteliginde ({longest[4]:.1f} dk >= 20 dk). Secilen: {longest[0]}", flush=True)
            return longest[1], longest[2], longest[3]

    # 6. Hiçbir kriter tutmadıysa (video gerçekten eksik/kırpık/fragman ve alternatiflerde de tam bölüm yok)
    display_refs = primary_refs or secondary_refs
    ref_str = f"Olması Gereken: {', '.join(str(r) for r in display_refs)} dk" if display_refs else "Doğrulanamadı"
    found_str = f"Kaynakta Bulunan: {int(init_dur)} dk" if init_dur else "Kaynak süresi okunamadı"
    err_msg = f"Bölüm süresi eksik/hatalı! {ref_str}, {found_str}."
    print(f"HayaletDebug: HICBIR SITE GECERLI BULUNAMADI! Hata firlatiyorum: {err_msg}", flush=True)
    raise RuntimeError(err_msg)


def play(episode_idx):
    """Bolumu cozer, yerel proxy'de sentetik master kurar ve ExoPlayer'in
    dogrudan acabilecegi 127.0.0.1 URL'sini dondurur."""
    global _proxy
    try:
        from hayalet.core.actions import _build_watch_master
        from hayalet.core.proxy import HLSProxy
        from hayalet.core.sites import SITES

        ep = _episodes[int(episode_idx)]
        merged, net, session = _verified_stream(ep, _series)

        stop()  # onceki oynatmanin proxy'si acik kalmasin
        _proxy = HLSProxy(session, merged.video_referer).start()
        url, n_aud, has_sub = _build_watch_master(_proxy, net, session, merged)

        title = _series.name if len(_episodes) == 1 else \
            "%s · %sx%02d" % (_series.name, ep.season, ep.number)

        # "Sonraki bolum" icin: sezon sinirini da asarak siradaki bolumun indeksi.
        # Oynatici bolum bitince bunu kullanip terminale/listeye donmeden geciyor.
        from hayalet.core.models import next_episode
        nxt = next_episode(_episodes, ep)
        next_idx = _episodes.index(nxt) if nxt is not None else -1

        # Kaldigin yerden devam anahtari: URL her acilista degistigi icin
        # konum kaydi site+dizi+sezon+bolume gore tutulmali.
        key = "%s|%s|%s|%s" % (_series.site, _series.slug, ep.season, ep.number)

        # Ana sayfadaki "kaldigin yerden devam" rafi icin: karti CIZMEK (ad, tur)
        # ve tiklaninca yapimi ARAMA YAPMADAN yeniden acmak (play_key) icin gereken
        # her sey. Anahtarin kendisi de icinde — Java tarafi tek kayit tutsun.
        resume = {"key": key, "site": _series.site, "slug": _series.slug,
                  "type": _series.type, "name": _series.name,
                  "poster": _series.poster_url,
                  "season": ep.season, "number": ep.number, "title": title}

        return json.dumps({"url": url, "title": title, "audios": n_aud,
                           "subtitle": has_sub, "next": next_idx, "key": key,
                           "resume": resume})
    except Exception as e:
        return _err(e)


def play_key(meta_json):
    """Kaydedilmis bir "devam" kaydindan dogrudan oynatir — arama yapmadan.

    Ana sayfa rafi elinde yalnizca site/slug/tur/ad ve sezon/bolum tutuyor;
    burada Series/Episode yeniden kurulup `_results`/`_episodes` durumu
    tazeleniyor, sonra normal play() akisi kullaniliyor (sonraki bolum, kalite,
    indirme hepsi ayni sekilde calissin diye).
    """
    global _episodes, _series
    try:
        from hayalet.core.models import Series, match_episode
        from hayalet.core.sites import SITES

        meta = json.loads(meta_json) if isinstance(meta_json, str) else meta_json
        _connect_all()
        site = meta["site"]
        if site not in _ctx:
            return json.dumps({"error": "%s sitesine baglanilamadi" % site})

        s = Series(name=meta.get("name", ""), slug=meta["slug"],
                   type=meta.get("type", "Dizi"), site=site,
                   poster_url="")
        net, session = _ctx[site]
        from hayalet.core.sites import enrich_series
        enrich_series(net, session, s)
        eps = SITES[site].get_episodes(net, session, s)
        if not eps:
            return json.dumps({"error": "Bolum listesi bos"})
        _episodes, _series = eps, s

        ep = match_episode(eps, int(meta.get("season", 1)),
                           int(meta.get("number", 1))) or eps[0]
        return play(_episodes.index(ep))
    except Exception as e:
        return _err(e)


def dbg_probe(out_path):
    """Gelistirme kancasi: hangi siteye baglanilabildigini diske yazar.

    Masaustunden Dizipal'in tum domain ailesi erisilemez gorunuyor; telefonun
    baglantisi farkli olabilir. "Calisiyor mu" sorusunu tahminle degil olcerek
    cevaplamak icin.
    """
    try:
        from hayalet.core.sites import SITES
        _connect_all()
        lines = []
        for name in SITES:
            if name in _ctx:
                net, session = _ctx[name]
                try:
                    n = len(SITES[name].search(net, session, "a"))
                    lines.append("%s OK %s (arama: %d sonuc)"
                                 % (name, session.base_url, n))
                except Exception as e:
                    lines.append("%s DOMAIN-OK ama arama patladi: %s: %s"
                                 % (name, type(e).__name__, e))
            else:
                lines.append("%s BAGLANILAMADI" % name)
        with open(out_path, "w", encoding="utf8") as f:
            f.write("\n".join(lines))
        return json.dumps({"ok": True, "sonuc": lines})
    except Exception as e:
        return _err(e)


def dbg_fetch(url, out_path, site="hdfilmcehennemi"):
    """Gelistirme kancasi: bir sayfayi telefonun oturumuyla cekip diske yazar.

    Neden var: her iki site de masaustu baglantisindan erisilemiyor
    (hdfilmcehennemi Cloudflare dogrulamasina takiliyor, Dizipal'in domain
    ailesi bolgesel engelli) ama telefondan ikisi de aciliyor. Yeni kazima
    (raflar, kategoriler) yazarken sayfanin gercek HTML'ini gormek gerekiyor;
    bu kanca onu adb ile almayi sagliyor. Arayuzden erisilemez.
    """
    try:
        _connect_all()
        if site not in _ctx:
            return json.dumps({"error": "baglanti yok"})
        net, session = _ctx[site]
        full = url if url.startswith("http") else session.base_url + url
        html = net.get(full, referer=session.base_url).text
        with open(out_path, "w", encoding="utf8") as f:
            f.write(html)
        return json.dumps({"ok": True, "bytes": len(html), "url": full})
    except Exception as e:
        return _err(e)


def _title_for(ep):
    return _series.name if len(_episodes) == 1 else \
        "%s %sx%02d" % (_series.name, ep.season, ep.number)


def qualities(episode_idx):
    """Kaynagin GERCEKTEN sundugu kalite varyantlari. Sabit 1080/720 listesi
    uydurmuyoruz — bazi kaynakta tek kalite var, bazisinda 4 tane."""
    try:
        from hayalet.core.m3u8_parser import list_variants
        from hayalet.core.sites import SITES

        ep = _episodes[int(episode_idx)]
        net, session = _ctx[_series.site]
        merged = SITES[_series.site].build_stream(net, session, ep, _series)
        try:
            vs = list_variants(net, session, merged.video_master_url,
                               merged.video_referer)
        except Exception:
            vs = []
        items = [{"h": v.height, "label": v.label} for v in vs]
        if not items:
            items = [{"h": 0, "label": "Tek kalite"}]
        return json.dumps({"items": items, "title": _title_for(ep)})
    except Exception as e:
        return _err(e)


def seasons():
    """Mevcut dizinin sezonlari (toplu indirme icin)."""
    try:
        from hayalet.core.models import seasons_of
        return json.dumps({"seasons": seasons_of(_episodes)})
    except Exception as e:
        return _err(e)


def _provider(site):
    from hayalet.core.sites import SITES
    net, session = _ctx[site]
    return net, session, SITES[site]


def download(episode_idx, height=0, max_seconds=0):
    """Tek bolumu indirme kuyruguna alir. Hemen doner."""
    try:
        import hayalet_dl
        hayalet_dl.set_context_provider(_provider)
        ep = _episodes[int(episode_idx)]
        title = _title_for(ep)
        job = hayalet_dl.add(_series, ep, title, _series.site,
                             int(height), int(max_seconds))
        return json.dumps({"job": job, "title": title})
    except Exception as e:
        return _err(e)


def download_season(season, height=0, max_seconds=0):
    """Bir sezonun tum bolumlerini sirayla kuyruga alir."""
    try:
        import hayalet_dl
        from hayalet.core.models import episodes_in_season
        hayalet_dl.set_context_provider(_provider)
        eps = episodes_in_season(_episodes, int(season))
        jobs = []
        for ep in eps:
            jobs.append(hayalet_dl.add(_series, ep, _title_for(ep), _series.site,
                                       int(height), int(max_seconds)))
        return json.dumps({"jobs": jobs, "count": len(jobs)})
    except Exception as e:
        return _err(e)


def download_series(height=0, max_seconds=0):
    """Dizinin tum sezon ve bolumlerini sirayla kuyruga alir."""
    try:
        import hayalet_dl
        hayalet_dl.set_context_provider(_provider)
        jobs = []
        for ep in _episodes:
            jobs.append(hayalet_dl.add(_series, ep, _title_for(ep), _series.site,
                                       int(height), int(max_seconds)))
        return json.dumps({"jobs": jobs, "count": len(jobs)})
    except Exception as e:
        return _err(e)


def dl_status(job_id=None):
    import hayalet_dl
    return hayalet_dl.status(job_id or None)


def dl_cancel(job_id):
    import hayalet_dl
    return hayalet_dl.cancel(job_id)


def dl_retry(job_id):
    import hayalet_dl
    return hayalet_dl.retry(job_id)


def dl_forget(job_id):
    import hayalet_dl
    return hayalet_dl.forget(job_id)


def dl_clear():
    import hayalet_dl
    return hayalet_dl.clear_finished()


def library():
    """Indirilmis basliklarin listesi (cevrimdisi izlemek icin)."""
    import hayalet_dl
    return hayalet_dl.library(os.environ.get("HAYALET_DOWNLOAD_DIR", ""))


def dl_remove(path):
    import hayalet_dl
    return hayalet_dl.remove(path)


def stop():
    global _proxy
    if _proxy is not None:
        try:
            _proxy.stop()
        except Exception:
            pass
        _proxy = None
    return json.dumps({"ok": True})
