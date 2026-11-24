"""Dizipal'in Next.js motorunu paylaşan siteler için ortak taban adapter.

asyawatch, selcukflix ve benzerleri aynı "Web Drama Turkey"/Dizipal-Next.js
arka ucunu kullanıyor. İki kuşak var; YENİSİ önce denenir, bir şey vermezse
ESKİSİNE düşülür (site geri dönerse ya da bir klon eski kuşakta kalırsa diye).

YENİ kuşak (2026-11, App Router — Dizipal'in kendi geçişiyle aynı; canlı doğrulandı):
  POST /api/mj {"action","params"} (Origin şart)  -> {"ok":true,"result":...}
       selcukflix: searchContent / searchContentFullText -> result[] kartlar
       asyawatch : searchContent "forbidden-action"; arama kutusu getBrowseTitles
                   [sorgu, "dizi"|"film", null×5, "rec", sayfa, adet] -> result.items[]
  GET  /<slug>                    -> __next_f içinde /<slug>/sezon-N/bolum-M bağlantıları
                                     (tüm sezonlar tek sayfada: The Boys 4×8 = 32)
  GET  /<bölüm|film sayfası>      -> __next_f içinde oynatıcı verisi, iki biçim:
       selcukflix: "player":{enc,k,sources[{href,dub,on}]} — Dizipal'in yeni şeması;
                   enc utils.decrypt_rmk(enc, k) ile çözülür -> pichive iframe.
                   Her kaynak (PUB, PUB+, Uptobox, Dublaj) kendi enc'li AYRI sayfa.
       asyawatch : "sources":[{lang,src}] — pichive iframe adresi açıkta.

ESKİ kuşak:
  POST /api/bg/searchContent?searchterm=   -> {"response": <yük>}; .result[] kartlar
  GET  /<slug>, /<slug>/sezon-N/bolum-M    -> __NEXT_DATA__.secureData
Yük (`secureData`/`response`) siteye göre düz base64 ya da AES; onu alt sınıf
`_decode(raw)` ile verir.
"""
from __future__ import annotations

import json
import re

from hayalet.core import remote, utils
from hayalet.core.extractor import ExtractError, _page_player, resolve_iframe
from hayalet.core.m3u8_parser import get_av_urls
from hayalet.core.merge import AudioSource, MergedStream
from hayalet.core.models import Episode, Series
from hayalet.core.network import BlockedError, ChallengeError, Network, media_headers
from hayalet.core.resolver import ResolverError
from hayalet.core.session import SessionState
from hayalet.sites import _pichive

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_IFRAME_SRC_RE = re.compile(r'src=["\']([^"\']+)["\']', re.I)
_NEXT_F_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
# Kart türleri: "movieseries" (seri-filmler/… koleksiyon sayfası) oynatılamaz,
# oyuncu kayıtları da aynı listede geliyor — ikisi de alınmaz.
_SERIES_TYPES = {"series", "dizi"}
_FILM_TYPES = {"movies", "movie", "film"}


def _flat(html: str) -> str:
    """App Router sayfasının `self.__next_f.push([1,"..."])` parçalarını birleştirir."""
    out = []
    for p in _NEXT_F_RE.findall(html):
        try:
            out.append(json.loads(f'"{p}"'))
        except ValueError:
            continue
    return "".join(out)


def _arrays(flat: str, key: str):
    """Düz metindeki her `"key":[...]` dizisini sırayla çözüp verir."""
    needle = f'"{key}":['
    dec = json.JSONDecoder()
    at = flat.find(needle)
    while at >= 0:
        try:
            val, _ = dec.raw_decode(flat, at + len(needle) - 1)
            if isinstance(val, list):
                yield val
        except ValueError:
            pass
        at = flat.find(needle, at + 1)


def _is_dub(src: dict) -> bool:
    lang = str(src.get("lang") or src.get("langLabel") or "").lower()
    return bool(src.get("dub")) or lang == "dub" or "dublaj" in lang


def _find(obj, key):
    """İç içe dict/list içinde `key` anahtarının ilk değerini döndürür."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find(v, key)
            if found is not None:
                return found
    return None


class DizipalNextAdapter:
    """Ortak taban. Alt sınıf `name`, `known_domain` ve `_decode(raw)` verir."""

    name = ""
    known_domain = ""
    # Yeni kuşak arama eylemi: "content" = searchContent(+FullText),
    # "browse" = getBrowseTitles (searchContent'i yasaklayan siteler).
    search_mode = "content"
    # pichive akışı yalnız chrome kimliğine düzgün yanıt veriyor (vidmoly gibi).
    impersonates = ("chrome",)

    def _decode(self, raw: str):
        """Sitenin `secureData`/`response` yükünü Python nesnesine çevirir."""
        raise NotImplementedError

    def resolve_domain(self, net: Network, session: SessionState,
                       override: str | None = None, use_cache: bool = True) -> str:
        domain = (override or remote.get(self.name + ".known_domain",
                                         self.known_domain)).rstrip("/")
        try:
            r = net.get(domain, referer=domain)
        except BlockedError as e:
            raise ResolverError(f"{domain}'e ulaşılamadı: {e}")
        if r.status_code != 200:
            raise ResolverError(f"{domain} yanıt vermiyor (HTTP {r.status_code}).")
        session.base_url = domain
        session.referer = domain
        return domain

    # --- Sayfa verisi -----------------------------------------------------
    def _secure_data(self, net: Network, session: SessionState, url: str,
                     html: str | None = None):
        base = session.base_url
        if html is None:
            html = net.get(url, referer=base + "/",
                           headers=media_headers(base + "/")).text
        m = _NEXT_DATA_RE.search(html)
        if not m:
            raise ExtractError(f"{self.name}: __NEXT_DATA__ bulunamadı.")
        pp = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
        secure = pp.get("secureData")
        if not isinstance(secure, str):
            raise ExtractError(f"{self.name}: secureData yok (sayfa yapısı değişmiş).")
        return self._decode(secure)

    # --- Arama ------------------------------------------------------------
    def _card(self, item: dict) -> Series | None:
        slug = item.get("used_slug")                 # örn. "dizi/spooks"
        name = item.get("object_name")
        if not slug or not name:
            return None
        typ = (item.get("used_type") or "").lower()
        if typ and typ not in _SERIES_TYPES | _FILM_TYPES:
            return None          # oyuncu / film serisi koleksiyonu
        is_series = typ in _SERIES_TYPES
        year = item.get("object_release_year")
        rating = item.get("object_related_imdb_point") or item.get("imdb_point")
        return Series(name=name, slug=slug, site=self.name,
                      type="Dizi" if is_series else "Film",
                      year=str(year) if year else "",
                      rating=str(rating) if rating else "")

    def _mj(self, net: Network, session: SessionState, action: str, params: list):
        """Yeni kuşağın `/api/mj` çağrısı. Uç bu sitede yoksa None, varsa yanıt dict'i."""
        base = session.base_url
        try:
            data = net.post(base + "/api/mj", referer=base + "/", retries=1,
                            json={"action": action, "params": params},
                            headers={"Origin": base}).json()
        except ChallengeError:
            raise
        except Exception:
            return None
        return data if isinstance(data, dict) and "ok" in data else None

    def _browse_card(self, item: dict) -> Series | None:
        """`getBrowseTitles` öğesi -> Series (asyawatch arama kutusunun kaynağı)."""
        slug = (item.get("slug") or "").strip("/")
        name = item.get("title") or ""
        orig = item.get("original_title") or ""
        if orig and not re.search(r"[A-Za-zÇĞİÖŞÜçğıöşü]", name):
            name = orig       # ör. "고백의 역사" yerine "Love Untangled"
        if not slug or not name:
            return None
        year, imdb = item.get("year"), item.get("imdb")
        return Series(name=name, slug=slug, site=self.name,
                      type="Dizi" if item.get("kind") == "dizi" else "Film",
                      year=str(year) if year else "",
                      rating=str(imdb) if imdb else "",
                      poster_url=item.get("poster_url") or item.get("brand_url") or "")

    def _search_new(self, net: Network, session: SessionState,
                    query: str) -> list[Series] | None:
        """Yeni kuşak arama; uç yoksa None (eski yola düşülür)."""
        out: dict[str, Series] = {}
        answered = False
        if self.search_mode == "browse":
            for kind in ("dizi", "film"):
                d = self._mj(net, session, "getBrowseTitles",
                             [query, kind, None, None, None, None, None, "rec", 1, 40])
                if d is None:
                    continue
                answered = True
                res = d.get("result")
                for item in (res.get("items") if isinstance(res, dict) else None) or []:
                    s = self._browse_card(item)
                    if s and s.slug not in out:
                        out[s.slug] = s
        else:
            # Sitenin kendisi de ikisini birlikte çağırıyor (bkz. catalog._search_api).
            for action in ("searchContent", "searchContentFullText"):
                d = self._mj(net, session, action, [query])
                if d is None:
                    continue
                answered = True          # ok:false da cevaptır (kısa sorgu vb.)
                for item in d.get("result") or []:
                    s = self._card(item)
                    if s and s.slug not in out:
                        out[s.slug] = s
        return list(out.values()) if answered else None

    def search(self, net: Network, session: SessionState, query: str) -> list[Series]:
        found = self._search_new(net, session, query)
        if found is not None:
            return found
        base = session.base_url
        resp = net.post(base + "/api/bg/searchContent", params={"searchterm": query},
                        referer=base + "/",
                        headers={**media_headers(base + "/"),
                                 "X-Requested-With": "XMLHttpRequest"})
        try:
            env = self._decode(resp.json()["response"])
        except (ValueError, KeyError) as e:
            raise ExtractError(f"{self.name}: arama yanıtı çözülemedi: {e}")
        out, seen = [], set()
        for item in env.get("result") or []:
            s = self._card(item)
            if s and s.slug not in seen:
                seen.add(s.slug)
                out.append(s)
        return out

    def suggest(self, net: Network, session: SessionState, query: str) -> list[Series]:
        return self.search(net, session, query)

    # --- Bölümler ---------------------------------------------------------
    def get_episodes(self, net: Network, session: SessionState,
                     series: Series) -> list[Episode]:
        base = session.base_url
        page_url = series.url(base)
        if series.type == "Film":
            return [Episode(season=1, number=1, url=page_url, title=series.name)]
        html = net.get(page_url, referer=base + "/",
                       headers=media_headers(base + "/")).text
        flat = _flat(html)
        if flat:
            return self._episodes_new(net, session, series, html, flat)
        sd = self._secure_data(net, session, page_url, html)
        seasons = _find(sd, "getSerieSeasonAndEpisodes") or {}
        result = seasons.get("result") if isinstance(seasons, dict) else None
        episodes: list[Episode] = []
        for season_block in result or []:
            for ep in season_block.get("episodes") or []:
                s = ep.get("season_no")
                n = ep.get("episode_no")
                if not s or not n:
                    continue
                episodes.append(Episode(
                    season=int(s), number=int(n),
                    url=f"{page_url}/sezon-{s}/bolum-{n}",
                    title=ep.get("episode_text") or ""))
        return sorted(episodes, key=lambda e: (e.season, e.number))

    def _episodes_new(self, net: Network, session: SessionState, series: Series,
                      html: str, flat: str) -> list[Episode]:
        """Yeni kuşak: bölüm adresleri sayfa verisinde düz bağlantı olarak duruyor.

        Başlıklar varsa (selcukflix `"episodes":[{n,title,href}]`) eklenir. Dizi
        sayfası şimdilik tüm sezonları veriyor; vermediği bir sezonun bağlantısı
        görünürse o sezon sayfası ayrıca okunur.
        """
        base = session.base_url
        slug = "/" + series.slug.strip("/")
        ep_re = re.compile(re.escape(slug) + r"/sezon-(\d+)/bolum-(\d+)(?![\w\-])")
        season_re = re.compile(re.escape(slug) + r"/sezon-(\d+)(?![\w\-/])")
        titles: dict[str, str] = {}
        for arr in _arrays(flat, "episodes"):
            for e in arr:
                if isinstance(e, dict) and e.get("href") and e.get("title"):
                    titles[e["href"].split("?")[0]] = str(e["title"])

        found: dict[tuple[int, int], Episode] = {}

        def collect(text: str) -> None:
            for m in ep_re.finditer(text):
                key = (int(m.group(1)), int(m.group(2)))
                if key[0] < 1 or key[1] < 1 or key in found:
                    continue
                path = f"{slug}/sezon-{key[0]}/bolum-{key[1]}"
                found[key] = Episode(season=key[0], number=key[1], url=base + path,
                                     title=titles.get(path, ""))

        collect(flat + html)
        seen_seasons = {s for s, _ in found}
        for n in sorted({int(x) for x in season_re.findall(flat + html)} - seen_seasons):
            try:
                h = net.get(f"{base}{slug}/sezon-{n}", referer=series.url(base)).text
            except Exception:
                continue
            collect(_flat(h) + h)
        if not found:
            raise ExtractError(f"{self.name}: bölüm listesi bulunamadı (sayfa yapısı değişmiş).")
        return sorted(found.values(), key=lambda e: (e.season, e.number))

    # --- Akış -------------------------------------------------------------
    def _candidates_new(self, net: Network, session: SessionState,
                        page_url: str, html: str, flat: str):
        """Yeni kuşak oynatıcı adayları (pichive iframe adresleri), tembel üretici.

        Orijinal/altyazılı kaynaklar dublajdan önce gelir: video ve Türkçe altyazı
        orijinalden alınır (Dizipal merge'deki tercihle aynı); Türkçe ses master'da
        ayrı rendition olarak zaten geliyor.
        """
        base = session.base_url
        player = _page_player(html) or {}
        pages = [s for s in player.get("sources") or []
                 if isinstance(s, dict) and s.get("href") and not s.get("src")]
        # selcukflix: Dizipal şeması (enc + kaynak başına ayrı sayfa). asyawatch'ta da
        # bir "player" nesnesi var ama kaynakları `src` taşıyor — o aşağıda okunur.
        if player.get("enc") or pages:
            pages.sort(key=_is_dub)
            if player.get("enc"):
                yield utils.decrypt_rmk(json.dumps(player["enc"]), player.get("k") or None)
            for s in pages:
                if s.get("on"):
                    continue                 # açık sayfa: yukarıda çözüldü
                try:
                    h = net.get(base + s["href"], referer=page_url).text
                    p = _page_player(h) or {}
                    if p.get("enc"):
                        yield utils.decrypt_rmk(json.dumps(p["enc"]), p.get("k") or None)
                except Exception:
                    continue         # bu kaynak sayfası açılmadıysa sıradaki
        # asyawatch: "sources":[{lang,src}] — ilk dolu dizi bu sayfanın kendisi.
        for arr in _arrays(flat, "sources"):
            srcs = [s for s in arr if isinstance(s, dict) and s.get("src")
                    and not s.get("trailer")]
            if srcs:
                srcs.sort(key=_is_dub)
                for s in srcs:
                    yield s["src"]
                return
        for arr in _arrays(flat, "rawSources"):
            for s in arr:
                m = _IFRAME_SRC_RE.search((s or {}).get("source_content") or "")
                if m:
                    yield m.group(1)
            return

    def _iframes(self, sd) -> list[str]:
        srcs = _find(sd, "getEpisodeSources") or {}
        result = srcs.get("result") if isinstance(srcs, dict) else None
        urls: list[str] = []
        for s in result or []:
            m = _IFRAME_SRC_RE.search(s.get("source_content") or "")
            if m:
                urls.append(m.group(1))
        return urls

    def build_stream(self, net: Network, session: SessionState,
                     episode: Episode, series: Series) -> MergedStream:
        base = session.base_url
        html = net.get(episode.url, referer=base + "/",
                       headers=media_headers(base + "/")).text
        flat = _flat(html)
        if flat:
            iframes = self._candidates_new(net, session, episode.url, html, flat)
        else:
            iframes = self._iframes(self._secure_data(net, session, episode.url, html))

        last_err: Exception | None = None
        tried = 0
        for iframe in iframes:
            tried += 1
            try:
                try:
                    master, referer, sub = _pichive.resolve(net, iframe, episode.url)
                except ExtractError as pe:
                    # pichive.online (four. değil) Dizipal'in openPlayer/source2
                    # oynatıcısını sunuyor; onu Dizipal çıkarıcısı çözer.
                    try:
                        si = resolve_iframe(net, iframe, episode.url)
                    except Exception:
                        raise pe
                    master, referer, sub = si.m3u8_url, si.referer, si.subtitle_url
                _, tracks = get_av_urls(net, session, master, referer, "best")
                audios = [AudioSource(url=t.url, referer=referer, lang=(t.lang or "und"),
                                      name=(t.name or "Ses"), is_turkish=t.is_turkish)
                          for t in tracks]
                for i, a in enumerate(audios):
                    a.is_default = i == 0
                return MergedStream(video_master_url=master, video_referer=referer,
                                    audios=audios, subtitle_url=sub,
                                    subtitle_referer=referer)
            except Exception as e:   # bu kaynak ölüyse sıradakini dene
                last_err = e
                continue
        if not tried:
            raise ExtractError(f"{self.name}: bu bölümde oynatılacak kaynak yok.")
        raise ExtractError(f"{self.name}: çalışan kaynak yok "
                           f"({tried} denendi): {last_err}")
