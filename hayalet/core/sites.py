"""Site adapter arayüzü + kayıt defteri.

Her site (Dizipal, hdfilmcehennemi, ...) bu Protocol'ü karşılayan bir adapter
sağlar; `cli.py` hangi siteyle çalıştığını bilmeden (search/get_episodes/
build_stream) çağırır. Ortak katmanlar (proxy, ffmpeg mux, menü akışı, oturum/
ağ, kimlik rotasyonu) hiçbir adapter'a bağımlı değildir, hiç değişmez.

Adapter'lar `hayalet/sites/` altında yaşar ve import edildiklerinde kendilerini
`register()` ile buraya kaydederler (bkz. hayalet/sites/__init__.py).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import time
from typing import Protocol

from hayalet.core import query as q
from hayalet.core.merge import MergedStream
from hayalet.core.models import Episode, Series
from hayalet.core.network import Network
from hayalet.core.session import SessionState


class SiteAdapter(Protocol):
    name: str
    known_domain: str

    def resolve_domain(self, net: Network, session: SessionState,
                       override: str | None = None, use_cache: bool = True) -> str: ...

    def search(self, net: Network, session: SessionState, query: str) -> list[Series]: ...

    def suggest(self, net: Network, session: SessionState, query: str) -> list[Series]: ...

    def get_episodes(self, net: Network, session: SessionState,
                     series: Series) -> list[Episode]: ...

    def build_stream(self, net: Network, session: SessionState,
                     episode: Episode, series: Series) -> MergedStream: ...

    # --- isteğe bağlı: katalog gezinme ------------------------------------
    # Aşağıdaki ikisi Protocol'ün zorunlu parçası DEĞİL (bkz. home_rows/browse
    # yardımcıları): destekleyen adapter uygular, desteklemeyen hiç yazmaz ve
    # arayüz o siteyi gezinme listelerinde atlar. Böylece Dizipal gibi yalnızca
    # arama sunan bir site için sahte/boş uygulama yazmak gerekmiyor.


SITES: dict[str, SiteAdapter] = {}


def home_rows(contexts: dict[str, tuple[Network, SessionState]]) -> list[dict]:
    """Ana sayfa rafları: [{"title": "Nette İlk", "items": [Series, ...]}, ...].

    Destekleyen her siteden paralel toplanır; desteklemeyen ya da hata veren
    site sessizce atlanır (bir sitenin çökmesi ana sayfayı boşaltmasın).
    """
    return _collect(contexts, "home_rows")


def browse(contexts: dict[str, tuple[Network, SessionState]], kind: str) -> list[Series]:
    """kind = "dizi" | "film" — katalog listeleme sayfaları, arama olmadan."""
    return _collect(contexts, "browse", kind)


def _collect(contexts: dict[str, tuple[Network, SessionState]],
             method: str, *args, timeout: float = 8.0) -> list:
    """`timeout` saniye sonra, henuz bitmemis site(ler)i beklemeden elde ne
    varsa onunla doner. Sebep: bir site yavas/erisilemez oldugunda (REQUEST_TIMEOUT=20sn
    x MAX_RETRIES=3 gibi) `with ThreadPoolExecutor(...)` bloğu TUM sonuclar
    gelene kadar bekliyordu — 5 siteden 4'u 1 saniyede donse bile tek yavas
    site butun ana sayfayi dakikaya yakin bekletiyordu. Gec kalan site'in
    thread'i arka planda kendi halinde biter, sadece ekranı bloklamaktan
    cikariyoruz."""
    out: list = []
    ready = {n: c for n, c in contexts.items() if hasattr(SITES[n], method)}
    if not ready:
        return out
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(ready))
    futs = [ex.submit(getattr(SITES[n], method), net, session, *args)
            for n, (net, session) in ready.items()]
    done, _pending = concurrent.futures.wait(futs, timeout=timeout)
    for fut in done:
        try:
            out.extend(fut.result() or [])
        except Exception:
            pass
    ex.shutdown(wait=False)
    return out


def register(adapter: SiteAdapter) -> None:
    SITES[adapter.name] = adapter


def _poster_missing(series: Series) -> bool:
    return (not series.poster_url
            or "/backdrop/" in series.poster_url.lower()
            or "\\/" in series.poster_url
            or "/artist/" in series.poster_url.lower())


def enrich_series(net: Network, session: SessionState, series: Series) -> None:
    """Detay sayfasından poster, açıklama, yıl ve puan bilgilerini çeker.

    Poster sırası: og:image / poster bloğu → AYNI sitenin arama listesindeki
    slug'ı birebir aynı kayıt → sayfanın ilk 15 KB'ı içindeki herhangi bir
    resim. Son adım en güvensizi (önerilen yapımların/üst şeridin görseli
    yanlış kapak olabilir), o yüzden sona kaldı; bazı sitelerde (Dizipal)
    detay sayfası hiç poster vermiyor ama arama listesi veriyor.
    """
    _enrich_from_page(net, session, series, page_scan=False)
    if _poster_missing(series) and series.slug:
        adapter = SITES.get(series.site)
        if adapter is not None:
            try:
                for m in adapter.search(net, session, series.name):
                    if m.slug == series.slug and m.poster_url:
                        series.poster_url = m.poster_url
                        break
            except Exception:
                pass
    if _poster_missing(series):
        _enrich_from_page(net, session, series, page_scan=True)


def _enrich_from_page(net: Network, session: SessionState, series: Series,
                      page_scan: bool) -> None:
    try:
        url = series.url(session.base_url)
        # Eskiden `session=session` geçiliyordu: curl-cffi bilinmeyen argümanla
        # TypeError veriyor, Network bunu ağ hatası sanıp ~3 sn yeniden deniyor,
        # en sonda burada sessizce yutuluyordu — zenginleştirme hiç çalışmıyordu.
        resp = net.get(url, referer=session.base_url)
        if resp.status_code != 200:
            return
        html = resp.text
        if not html:
            return

        import html as _html
        import re
        from hayalet.core.utils import normalize_poster_url, poster_url_from_html

        needs_poster = (not series.poster_url
                        or "/backdrop/" in series.poster_url.lower()
                        or "\\/" in series.poster_url
                        or "/artist/" in series.poster_url.lower())
        if needs_poster:
            m_og = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.I)
            if not m_og:
                m_og = re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', html, re.I)
            if not m_og:
                m_og = re.search(r'<meta\s+name=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']', html, re.I)

            p_url = ""
            if m_og:
                cand = m_og.group(1).strip()
                if "/artist/" not in cand.lower():
                    p_url = normalize_poster_url(cand, session.base_url)

            if not p_url:
                poster_block_match = re.search(
                    r'(<div[^>]*class=["\'][^"\']*(?:poster|cover|single-poster|film-info)[^"\']*["\'].*?</div>)',
                    html, re.S | re.I)
                if poster_block_match:
                    p_url = poster_url_from_html(poster_block_match.group(1), session.base_url)
                if not p_url and page_scan:
                    p_url = poster_url_from_html(html[:15000], session.base_url)

            if p_url:
                series.poster_url = p_url

        if not series.description:
            m_desc = re.search(r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']', html, re.I)
            if not m_desc:
                m_desc = re.search(r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)["\']', html, re.I)
            if m_desc:
                series.description = _html.unescape(m_desc.group(1)).strip()

        if not series.rating:
            m_rate = re.search(r'(?:imdb|puan|rating)[^>]*?>\s*([0-9]+[.,][0-9]+)', html, re.I)
            if not m_rate:
                m_rate = re.search(r'class=["\'][^"\']*(?:imdb|rating)[^"\']*["\'][^>]*>\s*([0-9]+[.,][0-9]+)', html, re.I)
            if m_rate:
                series.rating = m_rate.group(1).replace(",", ".").strip()

        if not series.year:
            # Tarih ("2026-10-02", Dizipal JSON-LD'si) ve URL ("w3.org/2000/svg")
            # içindeki sayılar yıl değil: Dizipal'de yapımın yılı yerine sayfanın
            # güncellenme yılı geliyordu (ölçüldü: 2021 dizisine 2026). Başta
            # yoksa sitenin yıl bağlantısı (Dizipal: href=".../yil/2021").
            # Arama tüm sayfada, ama yalnız ilk 4000 karakterde başlayan kabul:
            # html[:4000] kesimi tarihi ortadan bölünce ileri-bakış işe yaramıyordu.
            m_yr = re.search(r'(?<![\d\-/.:])(19[5-9]\d|20[0-2]\d)(?![\d\-])', html)
            if m_yr and m_yr.start() >= 4000:
                m_yr = None
            m_yr = m_yr or re.search(r'/yil/(19[5-9]\d|20[0-2]\d)/?["\']', html)
            if m_yr:
                series.year = m_yr.group(1).strip()
    except Exception:
        pass


# --- Çoklu-site paralel arama ----------------------------------------------
# `contexts`: site adı -> o sitenin (Network, SessionState) çifti. Siteler
# birbirinden tamamen bağımsız Network/SessionState kullanır (paylaşılan
# mutable state yok), o yüzden eşzamanlı çağrı güvenlidir.
def _run_all(method_name: str, contexts: dict[str, tuple[Network, SessionState]],
            query: str) -> list[Series]:
    if not contexts:
        return []
    results: list[Series] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(contexts)) as ex:
        futs = {
            ex.submit(getattr(SITES[name], method_name), net, session, query): name
            for name, (net, session) in contexts.items()
        }
        for fut in concurrent.futures.as_completed(futs):
            try:
                results.extend(fut.result())
            except Exception:
                pass  # bir site başarısız olursa diğerinin sonucu yine gösterilsin
    return results


# Sonuç sayısı bunun altındaysa liste "zayıf" sayılır ve yazım varyantları da
# denenir — asıl sorgu bir şeyler bulmuş olsa bile.
_ENOUGH_RESULTS = 5

# Çoklu-site aramada en fazla bu kadar saniye beklenir (bkz. search_all).
_SEARCH_DEADLINE = 7.0


def search_site(adapter: SiteAdapter, net: Network, session: SessionState,
                term: str) -> list[Series]:
    """Tek sitede "toleranslı" arama: gerekirse alternatif yazımları da dener.

    Sitelerin arama backend'i harfi harfine eşleşme arıyor (canlı: `spider-man`
    sonuç veriyor, `spiderman` hiçbir şey döndürmüyor). Asıl sorgu yeterince iyi
    bir sonuç vermezse `query.variants` ile bir avuç makul yazım **paralel**
    denenir ve birleşik liste benzerliğe göre sıralanıp eşiğin altı atılır —
    yani ağ genişler ama liste çöple dolmaz. Sorgu ilk denemede tuttuğunda
    (yaygın durum) hiç ek istek yapılmaz.
    """
    results = adapter.search(net, session, term)
    best = q.best_score(term, results)
    # Aranan şey **birebir** bulunduysa varyant atmak zararlı: "the last of us"
    # sitede tek sonuçla ve 1.00 puanla geliyordu, ama liste kısa diye geniş ağ
    # atılınca yanına 16 tane 'The Last ...' ekleniyordu. Uzunluk artık puana
    # yansıdığı için (bkz. query._score_one) bu "birebir" ölçüsü güvenilir:
    # 'Vjeran Tomic: The Spider-Man of Paris' eşiğin çok altında kalır.
    if best >= q.EXACT_SCORE:
        return q.rank(term, results)
    # Birebir olmayan iyi sonuçlarda TEK bir iyi sonuç yetmiyor: "spiderman"
    # sitede sadece "Vjeran Tomic: The Spider-Man of Paris"i getiriyordu —
    # başlık sorguyu içerdiği için puanı yüksek çıkıyor ama asıl aranan filmler
    # listede yok. O yüzden ek koşul: liste zaten doyurucu uzunlukta olmalı.
    if len(results) >= _ENOUGH_RESULTS and best >= q.GOOD_SCORE:
        return q.rank(term, results)

    alts = q.variants(term)
    if alts:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(alts)) as ex:
            for fut in [ex.submit(adapter.search, net, session, a) for a in alts]:
                try:
                    results.extend(fut.result())
                except Exception:
                    pass  # varyantın başarısızlığı asıl sonucu götürmesin

    seen: set[str] = set()
    unique = [r for r in results
              if not (r.slug in seen or seen.add(r.slug))]
    return q.rank(term, unique)


# Aynı yapım iki sitede birden bulunduğunda hangi kaynak tercih edilir.
# Kullanıcının kurgusu: **filmler hdfilmcehennemi, diziler Dizipal**. Bu bir
# TERCİH, süzgeç değil — aradaki fark önemli: eskiden Dizipal'in "Movies"
# kayıtları adapter'da tamamen atılıyordu ve yalnızca orada bulunan Türk
# filmleri (Sıfır Bir, Adana İşi…) hiçbir şekilde erişilemiyordu. Artık tekrar
# asıl yerinde, birleştirme anında çözülüyor: iki kaynakta da varsa tercih
# edilen site kazanır, tek kaynakta varsa o kayıt olduğu gibi kalır.
_PREFERRED_SITE = {True: "hdfilmcehennemi", False: "dizipal"}  # is_movie -> site


def _dedupe_cross_site(results: list[Series]) -> list[Series]:
    """Aynı yapımın farklı sitelerden gelen kopyalarını tek satıra indirir.

    Eşleşme `query.keys` üzerinden: katlanmış/gürültüsüz başlık, çok dilli ad
    parçaları dahil — "The Matrix 2 - The Matrix Reloaded" ile "The Matrix
    Reloaded" aynı yapımdır. Film ve dizi anahtarları AYRI tutulur: Dizipal'de
    "Sıfır Bir" hem dizi hem film olarak var ve bunlar tekrar değil.
    """
    from hayalet.core import catalog

    groups: list[list] = []          # [anahtar kümesi, seçilen Series]
    where: dict[tuple[bool, str], int] = {}
    for r in results:
        movie = bool(catalog.is_movie(r))
        ks = {(movie, k) for k in q.keys(r.name)}
        if not ks:
            groups.append([set(), r])
            continue
        idx = next((where[k] for k in ks if k in where), None)
        if idx is None:
            groups.append([ks, r])
            idx = len(groups) - 1
        else:
            groups[idx][0] |= ks
            want = _PREFERRED_SITE.get(movie)
            # Tercih edilen kaynak sonradan geldiyse onunla değiştir.
            if r.site == want and groups[idx][1].site != want:
                groups[idx][1] = r
        for k in ks:
            where.setdefault(k, idx)
    return [g[1] for g in groups]


def search_all(contexts: dict[str, tuple[Network, SessionState]], query: str) -> list[Series]:
    """Tüm sitelerde paralel arar, sonuçları birleştirir (her Series.site dolu).

    Her site kendi içinde `search_site` ile varyant denemesi yapar; birleşik
    listeden önce aynı yapımın site kopyaları elenir (`_dedupe_cross_site`),
    sonra tek seferde sorguya benzerliğe göre sıralanır (birebir eşleşmeler,
    hangi siteden gelirse gelsin, en üstte)."""
    if not contexts:
        return []
    results: list[Series] = []
    # `with` bloğu TÜM iş parçacıklarını beklerdi: tek yavaş/ölü site (yeniden
    # deneme + zaman aşımı bütçesiyle onlarca saniye) diğerleri çoktan cevap
    # vermişken bile aramayı bekletiyordu. Süre sınırından sonra gelenle
    # devam edilir; geç kalan site o aramada yok sayılır.
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(contexts))
    futs = [ex.submit(search_site, SITES[name], net, session, query)
            for name, (net, session) in contexts.items()]
    done, _pending = concurrent.futures.wait(futs, timeout=_SEARCH_DEADLINE)
    ex.shutdown(wait=False)
    for fut in done:
        try:
            results.extend(fut.result())
        except Exception:
            pass  # bir site başarısız olursa diğerinin sonucu yine gösterilsin
    # Kopya elemeden ÖNCE: tercih edilen sitenin kopyası kaynaksızsa öteki
    # sitedeki oynatılabilir kopya kalmalı.
    results = filter_available(contexts, results)
    return q.rank(query, _dedupe_cross_site(results))


def suggest_all(contexts: dict[str, tuple[Network, SessionState]], query: str) -> list[Series]:
    """search_all sonuç vermediğinde tüm sitelerde paralel öneri arar."""
    results = filter_available(contexts, _run_all("suggest", contexts, query))
    return q.rank(query, _dedupe_cross_site(results))


# --- Kaynağı olmayan yapımlar -------------------------------------------------
# hdfilmcehennemi vizyondaki filmleri kaynak yüklenmeden listeliyor: sayfa var,
# kaynak menüsü boş, yalnız fragman (ölçüldü 2026-10-04: Resident Evil 2026).
# Arama/raf kartlarında bunu ele veren bir işaret yok; tek yol yapımın sayfasına
# bakmak. Bunu `has_source` tanımlayan adapter'lar için paralel yapar, sonucu
# diske yazar: kaynağı olan uzun süre, olmayan kısa süre (yüklenince geri
# gelsin) hatırlanır. Süre sınırında cevap vermeyen yapım GÖSTERİLİR — emin
# olmadan bir şeyi gizlemek, kaynaksız bir kartı göstermekten kötü.
_AVAIL_TTL = {True: 30 * 86400, False: 6 * 3600}
_AVAIL_DEADLINE = 4.0
_avail_lock = threading.Lock()
_avail_cache: dict | None = None


def _avail_file():
    from hayalet import config
    return config.CACHE_DIR / "availability.json"


def _avail_get() -> dict:
    global _avail_cache
    if _avail_cache is None:
        try:
            _avail_cache = json.loads(_avail_file().read_text(encoding="utf-8"))
        except Exception:
            _avail_cache = {}
    return _avail_cache


def _avail_save() -> None:
    try:
        path = _avail_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_avail_get()), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def known_available(series: Series) -> bool | None:
    """Önbellekteki karar (True/False) ya da bilinmiyorsa None. Ağ yok."""
    if not hasattr(SITES.get(series.site), "has_source"):
        return True
    with _avail_lock:
        hit = _avail_get().get(f"{series.site}|{series.slug}")
    if hit and time.time() - hit[0] < _AVAIL_TTL[bool(hit[1])]:
        return bool(hit[1])
    return None


def filter_available(contexts: dict[str, tuple[Network, SessionState]],
                     results: list[Series],
                     timeout: float = _AVAIL_DEADLINE) -> list[Series]:
    """Kaynağı olmadığı KESİN olan yapımları çıkarır, sırayı korur."""
    verdict: dict[int, bool] = {}
    todo = []
    for i, s in enumerate(results):
        v = known_available(s)
        if v is None and s.site in contexts:
            todo.append((i, s))
        elif v is not None:
            verdict[i] = v
    if todo:
        def check(s):
            # Her iş parçacığına kendi Network'ü: curl-cffi istemcisi
            # eşzamanlı kullanılamaz.
            _net, session = contexts[s.site]
            return bool(SITES[s.site].has_source(Network(session), session, s))

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(todo)))
        futs = {ex.submit(check, s): (i, s) for i, s in todo}
        done, _ = concurrent.futures.wait(futs, timeout=timeout)
        ex.shutdown(wait=False)
        now = time.time()
        with _avail_lock:
            cache = _avail_get()
            for fut in done:
                i, s = futs[fut]
                try:
                    ok = fut.result()
                except Exception:
                    continue          # ağ hatası: karar yok, gösterilir
                verdict[i] = ok
                cache[f"{s.site}|{s.slug}"] = [now, ok]
            _avail_save()
    return [s for i, s in enumerate(results) if verdict.get(i, True)]
