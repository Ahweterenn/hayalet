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
        resp = net.get(url, session=session)
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
            m_yr = re.search(r'\b(19[5-9]\d|20[0-2]\d)\b', html[:4000])
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
    return q.rank(query, _dedupe_cross_site(results))


def suggest_all(contexts: dict[str, tuple[Network, SessionState]], query: str) -> list[Series]:
    """search_all sonuç vermediğinde tüm sitelerde paralel öneri arar."""
    return q.rank(query, _dedupe_cross_site(_run_all("suggest", contexts, query)))
