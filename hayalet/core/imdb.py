"""IMDb bilgisi: puan, özet, tür, süre, oyuncular, fragman, popüler listeler.

Anahtarsız iki uç kullanılıyor (canlı doğrulandı, 2026-09-27):
  * öneri servisi (`v3.sg.media-imdb.com/suggestion`) — addan IMDb kimliği.
    Türkçe adı da tanıyor: "Kara Şövalye" -> tt0468569 (The Dark Knight).
  * sitenin kendi GraphQL'i (`caching.graphql.imdb.com`) — ayrıntılar ve
    "en popüler" listeleri. **`Origin: https://www.imdb.com` başlığı şart**:
    onsuz düz 403 dönüyor. `x-imdb-user-language: tr-TR` ile başlık Türkçe
    geliyor ("Kara Şövalye"); özet ve türler çoğu yapımda yalnız İngilizce
    (türler burada Türkçeleştirilir).
Başlık sayfaları (`imdb.com/title/...`) bot sınamasına takılıyor (202 + JS);
onlar KULLANILMAZ.

IMDb bu veriyi kişisel ve ticari olmayan kullanım için serbest bırakıyor
(GraphQL cevabındaki `disclaimer`).

Eşleştirme sitenin verdiği ad + yıl + türle yapılır (`pick`); yıl uyuşmazsa
yanlış yapımın puanını göstermektense hiç göstermiyoruz.
"""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import quote

from hayalet.core import query

GQL_URL = "https://caching.graphql.imdb.com/"
SUGGEST_URL = "https://v3.sg.media-imdb.com/suggestion/x/{}.json"
_HEADERS = {
    "content-type": "application/json",
    "origin": "https://www.imdb.com",
    "referer": "https://www.imdb.com/",
    "x-imdb-user-country": "TR",
    "x-imdb-user-language": "tr-TR",
}
_TIMEOUT = 10
_TTL = 24 * 3600

_MOVIE_TYPES = {"movie", "tvMovie", "video"}
_SERIES_TYPES = {"tvSeries", "tvMiniSeries"}

GENRES_TR = {
    "Action": "Aksiyon", "Adventure": "Macera", "Animation": "Animasyon",
    "Biography": "Biyografi", "Comedy": "Komedi", "Crime": "Suç",
    "Documentary": "Belgesel", "Drama": "Dram", "Family": "Aile",
    "Fantasy": "Fantastik", "Film-Noir": "Kara film", "History": "Tarih",
    "Horror": "Korku", "Music": "Müzik", "Musical": "Müzikal",
    "Mystery": "Gizem", "Romance": "Romantik", "Sci-Fi": "Bilim kurgu",
    "Sport": "Spor", "Thriller": "Gerilim", "War": "Savaş", "Western": "Western",
    "Reality-TV": "Gerçeklik", "Talk-Show": "Talk show", "Game-Show": "Yarışma",
    "News": "Haber", "Short": "Kısa",
}

_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


class ImdbError(Exception):
    pass


def _cached(key: str, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    value = fn()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def _http():
    # Çağrı başına ayrı istemci: curl-cffi istemcisi iş parçacıkları arasında
    # paylaşılamıyor (bkz. resolver._scan_reachable).
    from curl_cffi import requests as cr
    return cr.Session(impersonate="chrome")


def _gql(q: str) -> dict:
    r = _http().post(GQL_URL, json={"query": q}, headers=_HEADERS, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise ImdbError(f"IMDb HTTP {r.status_code}")
    data = r.json()
    if data.get("errors") and not data.get("data"):
        raise ImdbError(str(data["errors"])[:200])
    return data.get("data") or {}


def sized(url: str, width: int = 400) -> str:
    """IMDb görselinin küçültülmüş hâli (asıl dosya birkaç MB olabiliyor)."""
    if not url:
        return ""
    return re.sub(r"\._V1_.*?\.(jpg|png)$", rf"._V1_QL75_UX{width}_.\1", url)


def _year(value) -> int | None:
    m = re.search(r"(19|20)\d{2}", str(value or ""))
    return int(m.group(0)) if m else None


# --- eşleştirme ---------------------------------------------------------------
def pick(suggestions: list[dict], year=None, movie: bool | None = None) -> str | None:
    """Öneri listesinden yapımın kimliği. Saf: ağ yok (testlenebilir).

    Tür (film/dizi) tutmayan atlanır; yıl biliniyorsa ±1 dışındaki atlanır
    (sitenin yılı ile IMDb'ninki bazen bir yıl kayıyor). Kalanlardan IMDb'nin
    kendi sıralamasındaki ilk — o sıra popülerliğe göre ve çoğunlukla doğru.
    """
    y = _year(year)
    for s in suggestions or []:
        sid, qid = s.get("id", ""), s.get("qid", "")
        if not sid.startswith("tt"):
            continue
        if movie is True and qid not in _MOVIE_TYPES:
            continue
        if movie is False and qid not in _SERIES_TYPES:
            continue
        if movie is None and qid not in _MOVIE_TYPES | _SERIES_TYPES:
            continue
        sy = s.get("y")
        if y and sy and abs(int(sy) - y) > 1:
            continue
        return sid
    return None


def _suggest(text: str) -> list[dict]:
    t = text.strip()[:60]
    if not t:
        return []
    r = _http().get(SUGGEST_URL.format(quote(t)), timeout=_TIMEOUT)
    if r.status_code != 200:
        return []
    return r.json().get("d") or []


def find(name: str, year=None, movie: bool | None = None) -> str | None:
    """Sitenin verdiği addan IMDb kimliği; emin olunamazsa None.

    hdfilmcehennemi adı birkaç dilde birden taşıyor ("Kara Şövalye - The Dark
    Knight"): parçalar sırayla denenir. Gürültü ekleri (Türkçe Dublaj, izle)
    atılır.
    """
    def work():
        parts = []
        for t in query.alt_titles(name or ""):
            words = [w for w in t.split() if query.normalize(w) not in query._NOISE]
            c = " ".join(words).strip()
            if c and c not in parts:
                parts.append(c)
        for part in parts[:3]:
            sid = pick(_suggest(part), year, movie)
            if sid:
                return sid
        return None
    return _cached(f"find:{name}|{year}|{movie}", work)


# --- ayrıntı -------------------------------------------------------------------
_DETAIL_Q = """{ title(id:"%s"){ id titleText{text} originalTitleText{text}
  releaseYear{year} titleType{id}
  ratingsSummary{aggregateRating voteCount} plot{plotText{plainText}}
  genres{genres{text}} runtime{seconds} primaryImage{url}
  principalCredits{category{id} credits(limit:5){name{nameText{text}}}}
  primaryVideos(first:1){edges{node{name{value} runtime{value}
    playbackURLs{url videoMimeType}}}} } }"""


def details(imdb_id: str) -> dict:
    if not re.fullmatch(r"tt\d{5,10}", imdb_id or ""):
        raise ImdbError("geçersiz IMDb kimliği")

    def work():
        t = (_gql(_DETAIL_Q % imdb_id).get("title") or {})
        if not t:
            raise ImdbError("IMDb'de bulunamadı")
        credits = {}
        for c in t.get("principalCredits") or []:
            cat = ((c.get("category") or {}).get("id") or "")
            names = [((x.get("name") or {}).get("nameText") or {}).get("text", "")
                     for x in c.get("credits") or []]
            credits[cat] = [n for n in names if n]
        trailer = None
        for e in ((t.get("primaryVideos") or {}).get("edges") or []):
            node = e.get("node") or {}
            urls = [u for u in node.get("playbackURLs") or []
                    if (u.get("videoMimeType") or "").upper() in ("MP4", "M3U8")
                    or ".mp4" in (u.get("url") or "")]
            if urls:
                trailer = {"url": urls[0]["url"],
                           "name": (node.get("name") or {}).get("value", ""),
                           "seconds": (node.get("runtime") or {}).get("value") or 0}
                break
        rs = t.get("ratingsSummary") or {}
        genres = [g.get("text", "") for g in ((t.get("genres") or {}).get("genres") or [])]
        return {
            "id": imdb_id,
            "title": (t.get("titleText") or {}).get("text", ""),
            "original": (t.get("originalTitleText") or {}).get("text", ""),
            "year": (t.get("releaseYear") or {}).get("year"),
            "type": (t.get("titleType") or {}).get("id", ""),
            "rating": rs.get("aggregateRating"),
            "votes": rs.get("voteCount") or 0,
            "plot": ((t.get("plot") or {}).get("plotText") or {}).get("plainText", ""),
            "genres": [GENRES_TR.get(g, g) for g in genres if g],
            "runtime": int(((t.get("runtime") or {}).get("seconds") or 0) // 60),
            "poster": sized((t.get("primaryImage") or {}).get("url", "")),
            "directors": credits.get("director", []),
            "creators": credits.get("creator", []),
            "stars": credits.get("cast", []),
            "trailer": trailer,
        }
    return _cached(f"detail:{imdb_id}", work)


def lookup(name: str, year=None, movie: bool | None = None) -> dict | None:
    """Ad + yıl + türden ayrıntı; eşleşme yoksa None."""
    sid = find(name, year, movie)
    return details(sid) if sid else None


# --- popüler listeler --------------------------------------------------------------
_CHART_Q = """{ chartTitles(first:%d, chart:{chartType:%s}){edges{node{
  id titleText{text} originalTitleText{text} releaseYear{year}
  ratingsSummary{aggregateRating} primaryImage{url} titleType{id} }}} }"""


def popular(kind: str = "movie", n: int = 20) -> list[dict]:
    """IMDb'nin "en popüler" listesi (`kind`: movie | tv)."""
    chart = "MOST_POPULAR_MOVIES" if kind == "movie" else "MOST_POPULAR_TV_SHOWS"

    def work():
        edges = ((_gql(_CHART_Q % (max(1, min(n, 50)), chart)).get("chartTitles") or {})
                 .get("edges") or [])
        out = []
        for e in edges:
            t = e.get("node") or {}
            out.append({
                "id": t.get("id", ""),
                "title": (t.get("titleText") or {}).get("text", ""),
                "original": (t.get("originalTitleText") or {}).get("text", ""),
                "year": (t.get("releaseYear") or {}).get("year"),
                "rating": (t.get("ratingsSummary") or {}).get("aggregateRating"),
                "poster": sized((t.get("primaryImage") or {}).get("url", "")),
                "movie": (t.get("titleType") or {}).get("id", "") in _MOVIE_TYPES,
            })
        return out
    return _cached(f"chart:{chart}:{n}", work)
