"""Dizipal'e özel arama + dizi/sezon/bölüm gezinme.

Arama: POST /bg/searchcontent (JSON döner).
Bölümler: /series/<slug> sayfasındaki /bolum/<...-SxE> linklerinden türetilir.

Site-bağımsız veri modelleri (Series/Episode) ve saf liste yardımcıları
core/models.py'ye taşındı; burada geriye dönük uyumluluk için re-export
edilir (mevcut `from hayalet.core.catalog import Episode, Series` importları
değişmeden çalışmaya devam eder).
"""
from __future__ import annotations

import re

from hayalet import config
from hayalet.core.models import (Episode, Series, episodes_in_season,
                                 match_episode, next_episode, seasons_of)
from hayalet.core.network import ChallengeError, Network
from hayalet.core.session import SessionState
from hayalet.core.utils import normalize_poster_url

__all__ = ["Series", "Episode", "search", "suggest", "is_movie", "get_episodes",
          "is_dubbed", "base_title", "find_counterpart", "find_original_counterpart",
          "match_episode", "next_episode", "seasons_of", "episodes_in_season"]


# --- Arama ----------------------------------------------------------------
# cValue, ana sayfadan kazınan bir arama token'ı — her aramada sayfayı yeniden
# çekmek yerine process başına bir kez alınır (siteye giden gereksiz isteği
# azaltır). Domain değişirse (nadir) otomatik yeniden çekilir.
_cvalue_cache: dict[str, tuple[str, str]] = {}


def _tokens(net: Network, session: SessionState) -> tuple[str, str]:
    """Arama formundaki gizli token'lar: (cValue, cKey).

    Site formda cValue'nun yanına cKey'i de ekledi; ikisi de gönderilir.
    """
    cached = _cvalue_cache.get(session.base_url)
    if cached is not None:
        return cached
    home = net.get(session.base_url).text
    v = re.search(r'name="cValue"\s+value="([^"]+)"', home)
    k = re.search(r'name="cKey"\s+value="([^"]+)"', home)
    tokens = (v.group(1) if v else "", k.group(1) if k else "")
    _cvalue_cache[session.base_url] = tokens
    return tokens


class SearchError(Exception):
    """Arama isteği JSON döndürmedi — endpoint/domain ölü (bkz. resolver)."""


def _search_api(net: Network, session: SessionState, query: str) -> list[Series] | None:
    """Yeni sitenin araması. Site bu uca sahip değilse None (eski yola düşülür).

    Sitenin kendisi de iki eylemi birlikte çağırıyor: tam metin araması, kısa
    aramada ötekinin kaçırdığı kayıtları getiriyor. Yalnız biri cevap verirse
    onunla yetinilir.
    """
    base = session.base_url
    out: dict[str, Series] = {}
    answered = False
    for action in ("searchContent", "searchContentFullText"):
        try:
            data = net.post(base + config.SEARCH_API, referer=base + "/",
                            json={"action": action, "params": [query]},
                            headers={"Origin": base}).json()
        except ChallengeError:
            raise
        except Exception:
            continue
        if not isinstance(data, dict) or "ok" not in data:
            continue          # JSON'umuz yok: bu uç bu sitede yok
        answered = True       # ok:false da cevaptır (ör. 3 harften kısa sorgu)
        for r in data.get("result") or []:
            slug = (r.get("used_slug") or "").strip("/")
            typ = r.get("used_type") or ""
            if not slug or typ.lower() not in _SEARCH_TYPES or slug in out:
                continue          # oyuncu kayıtları da bu listede geliyor
            imdb = r.get("object_related_imdb_point")
            out[slug] = Series(
                name=r.get("object_name") or slug, slug=slug, type=typ,
                year=str(r.get("object_release_year") or ""),
                rating=str(imdb) if imdb else "",
                poster_url=str(normalize_poster_url(
                    str(r.get("object_poster_url") or ""), base)))
    return list(out.values()) if answered else None


def search(net: Network, session: SessionState, query: str) -> list[Series]:
    found = _search_api(net, session, query)
    if found is not None:
        return found

    cvalue, ckey = _tokens(net, session)

    def _post(term: str):
        payload = {"searchterm": term, "cValue": cvalue}
        if ckey:
            payload["cKey"] = ckey
        return net.post(
            session.base_url + config.SEARCH_ENDPOINT,
            data=payload,
            referer=session.base_url,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    resp = _post(query)
    try:
        data = resp.json()
    except Exception:
        # Ham JSONDecodeError teşhisi gizliyordu: bu noktada gelen şey neredeyse
        # her zaman aynanın/park sayfasının HTML'i, yani "domain ölü" demek.
        ctype = (resp.headers.get("content-type") or "?").split(";")[0]
        raise SearchError(
            f"Arama JSON yerine {ctype} döndürdü ({len(resp.text)} bayt) — "
            f"{session.base_url} muhtemelen ayna/park sayfası, arama backend'i yok. "
            f"Güncel adresi --domain ile ver ya da önbelleği temizle (--no-cache)."
        ) from None

    results = (data.get("data") or {}).get("result") or []
    # Bazı kısa/özel kelimelerde ("gibi", "ben" vb.) Dizipal sunucusu stopword hatası ('state: false') veya boş döner.
    # Bu durumlarda " izle" ya da " dizi" eklenmiş sorgu arka plandaki tam metin aramasını tetikler ve gerçek kaydı getirir.
    if not results:
        q_clean = query.strip()
        for fallback_term in (f"{q_clean} izle", f"{q_clean} dizi"):
            try:
                f_resp = _post(fallback_term)
                f_data = f_resp.json()
                f_res = (f_data.get("data") or {}).get("result") or []
                if f_res:
                    results = f_res
                    break
            except Exception:
                pass

    out: list[Series] = []
    for r in results:
        slug = r.get("used_slug") or ""
        name = r.get("object_name") or slug
        typ = r.get("used_type") or "Series"
        if slug:
            poster = normalize_poster_url(
                str(r.get("object_poster_url") or r.get("poster") or r.get("poster_url") or r.get("image")
                    or r.get("thumbnail") or ""),
                session.base_url,
            )
            out.append(Series(name=name, slug=slug, type=typ,
                              poster_url=str(poster)))
    return out


def suggest(net: Network, session: SessionState, query: str, limit: int = 5) -> list[Series]:
    """Arama sonuç vermediğinde olası eşleşmeleri döndürür ("şunu mu demek istedin?").

    Sorgudaki kelimeleri (en uzundan başlayarak) tek tek arayıp bulunan adayları
    orijinal sorguya benzerliğine göre sıralar — yazım hatalarına karşı basit tolerans.
    Benzerlik ölçüsü `core/query`den gelir: tire/boşluk/Türkçe karakter farkı
    ceza saymaz ("spiderman" ≙ "Spider-Man").
    """
    from hayalet.core import query as q

    words = sorted((w for w in re.split(r"\s+", query.strip()) if len(w) >= 3),
                   key=len, reverse=True)

    candidates: dict[str, Series] = {}
    for w in words:
        for r in search(net, session, w):
            candidates.setdefault(r.slug, r)
        if candidates:
            break
    if not candidates:
        return []

    ranked = q.rank(query, list(candidates.values()), min_score=0.4)
    return ranked[:limit]


_MOVIE_TYPES = {"movies", "movie", "film", "movieseries"}
_SEARCH_TYPES = _MOVIE_TYPES | {"series"}


def is_movie(series: "Series") -> bool:
    return series.type.lower() in _MOVIE_TYPES


# --- Bölümler -------------------------------------------------------------
def _abs(link: str, session: SessionState) -> str:
    """Göreli bölüm linkini mutlak URL'ye çevirir."""
    if link.startswith("http"):
        return link
    return session.base_url + ("" if link.startswith("/") else "/") + link


def get_episodes(net: Network, session: SessionState, series: Series) -> list[Episode]:
    if is_movie(series):
        # Movie'lerde /bolum/ alt sayfası yok — kaynak (data-rm-k) doğrudan
        # filmin kendi sayfasında. Tek "bölüm" olarak filmin URL'sini döndür.
        return [Episode(season=1, number=1, url=series.url(session.base_url),
                        title=series.name)]

    html = net.get(series.url(session.base_url), referer=session.base_url).text

    seen: set[str] = set()
    episodes: list[Episode] = []

    # Sayfada başka dizilere giden kartlar da olabilir; yalnız bu dizinin
    # yolundakiler sayılır ("dizi/sherlock-dizi-izle" -> "/dizi/sherlock/").
    own = re.fullmatch(r"dizi/([a-z0-9\-]+?)-dizi-izle", series.slug.strip("/"), re.I)
    prefix = f"/dizi/{own.group(1).lower()}/" if own else ""

    # YENİ biçim (2026-10): /dizi/<slug>/3-sezon/7-bolum — sezon/bölüm yolda.
    for link, sez, bol in re.findall(config.SELECTORS["bolum_link_sezon"],
                                     html, re.I):
        if prefix and prefix not in link.lower():
            continue
        # Aynı bölüm hem göreli hem http://<alan>/... olarak geçiyor; tek adrese indir.
        url = _abs(re.sub(r"^https?://[^/]+", "", link), session)
        if url in seen:
            continue
        seen.add(url)
        episodes.append(Episode(season=int(sez), number=int(bol), url=url))

    if episodes:
        episodes.sort(key=lambda e: (e.season, e.number))
        return episodes

    # ESKİ biçim: /bolum/<slug>-3x7 (site geri dönerse diye duruyor)
    links = re.findall(r'href="([^"]*?/bolum/[a-z0-9\-]+)"', html, re.I)
    sxe_re = re.compile(config.SELECTORS["sxe_from_slug"], re.I)

    for link in links:
        url = link if link.startswith("http") else session.base_url + \
            ("" if link.startswith("/") else "/") + link
        if url in seen:
            continue
        seen.add(url)
        slug = url.rsplit("/bolum/", 1)[-1]
        mm = sxe_re.search(slug)
        if not mm:
            continue
        episodes.append(Episode(
            season=int(mm.group(1)),
            number=int(mm.group(2)),
            url=url,
        ))

    episodes.sort(key=lambda e: (e.season, e.number))
    return episodes


_DUB_RE = re.compile(r"\b(t[uü]rk[cç]e\s*dublaj|dublaj)\b", re.I)


def is_dubbed(series: "Series") -> bool:
    return bool(_DUB_RE.search(series.name))


def base_title(name: str) -> str:
    """'... Türkçe Dublaj' -> '...' (dublaj etiketini temizler)."""
    return _DUB_RE.sub("", name).strip(" -–—·")


def find_counterpart(net: Network, session: SessionState, series: "Series",
                     want_dubbed: bool) -> "Series | None":
    """Bir dizinin karşı sürümünü (dublaj↔orijinal) arama ile bulur.

    want_dubbed=True  → dublajlı muadili ara (orijinalden yola çıkarak)
    want_dubbed=False → orijinal/altyazılı muadili ara (dublajdan yola çıkarak)
    """
    import difflib

    base = base_title(series.name)
    if not base:
        return None
    cands = [r for r in search(net, session, base)
             if r.type == series.type and is_dubbed(r) == want_dubbed
             and r.slug != series.slug]
    if not cands:
        return None

    def score(r: Series) -> float:
        return difflib.SequenceMatcher(
            None, base_title(r.name).lower(), base.lower()).ratio()

    cands.sort(key=score, reverse=True)
    # Anlamlı bir eşleşme yoksa boş dön
    return cands[0] if score(cands[0]) >= 0.6 else None


def find_original_counterpart(net: Network, session: SessionState,
                              dubbed: "Series") -> "Series | None":
    """Dublajlı bir dizinin orijinal/altyazılı muadilini bulur (geriye dönük ad)."""
    return find_counterpart(net, session, dubbed, want_dubbed=False)
