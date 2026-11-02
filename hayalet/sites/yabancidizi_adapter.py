"""yabancidizi.news site adapter'ı (film + dizi, özel "mofy" motoru).

Arama hızlı bir JSON ucu; oynatıcı ise kaynak butonlarındaki `data-link`'ten
deterministik olarak kurulan `/api/ruplay/<data-link>` iframe'i (sunucu bunu
ok.ru gibi bir host'a çeviriyor). Sitenin `/ajax/service` stream akışı (videoGet/
get_stream) origin tarafında 520 verdiği için KULLANILMIYOR; `/api/ruplay` GET'i
o adımı atlıyor (canlı doğrulandı, 2026-11, tarayıcıdan çıkarıldı).

Zincir:
  POST /search?qr=<q>              -> {data:{result:[{s_id,s_type,s_link,s_name,s_year}]}}
                                      s_type "1"=film, "0"=dizi
  GET  /film/<slug> | /dizi/<slug> -> <div class="item" data-link="<tok>" ...>
  GET  /api/ruplay/<tok>           -> <iframe src="https://ok.ru/videoembed/<id>">
  -> _okru.resolve -> master m3u8

Diziler bölüm listesini ajax ile yüklüyor (o uç 520); bu yüzden dizide yalnız
ana sayfanın sunduğu (ilk) bölüm oynatılır — best-effort.
"""
from __future__ import annotations

import html
import re
import time
from urllib.parse import quote

from hayalet.core import remote
from hayalet.core.extractor import ExtractError
from hayalet.core.m3u8_parser import get_av_urls
from hayalet.core.merge import AudioSource, MergedStream
from hayalet.core.models import Episode, Series
from hayalet.core.network import BlockedError, Network
from hayalet.core.resolver import ResolverError
from hayalet.core.session import SessionState
from hayalet.core.sites import register
from hayalet.sites import _molystream, _okru

_IMG = "https://yabancidizi.news/uploads/series/"
_LINK_RE = re.compile(r'<div class="item[^"]*"[^>]*\bdata-link="([^"]+)"', re.I)
_IFRAME_RE = re.compile(r'''<iframe[^>]+src=["']([^"']+)["']''', re.I)


def _b64url(tok: str) -> str:
    """data-link'i tarayıcının yaptığı gibi base64url'e çevirir (+ -> -, / -> _)."""
    return tok.replace("+", "-").replace("/", "_")


def _udys() -> dict:
    """`/api/*` uçları JS'in set ettiği `udys=<Date.now()>` cookie'sini ister
    (`comolokko("udys", Date.now(), ...)`); tarayıcısız olduğumuz için elle veririz."""
    return {"Cookie": f"udys={int(time.time() * 1000)}"}


class YabancidiziAdapter:
    name = "yabancidizi"
    known_domain = "https://yabancidizi.news"

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

    def search(self, net: Network, session: SessionState, query: str) -> list[Series]:
        base = session.base_url
        resp = net.post(f"{base}/search?qr={quote(query)}", referer=base,
                        headers={"X-Requested-With": "XMLHttpRequest"}, data={})
        try:
            result = resp.json().get("data", {}).get("result") or []
        except ValueError as e:
            raise ExtractError(f"yabancidizi: arama yanıtı çözülemedi: {e}")
        out, seen = [], set()
        for it in result:
            link = it.get("s_link")
            name = it.get("s_name")
            if not link or not name:
                continue
            kind = "film" if str(it.get("s_type")) == "1" else "dizi"
            slug = f"{kind}/{link}"
            if slug in seen:
                continue
            seen.add(slug)
            img = it.get("s_image")
            out.append(Series(name=html.unescape(name).strip(), slug=slug,
                              type="Film" if kind == "film" else "Dizi", site=self.name,
                              year=str(it.get("s_year") or ""),
                              poster_url=_IMG + img if img else ""))
        return out

    def suggest(self, net: Network, session: SessionState, query: str) -> list[Series]:
        return self.search(net, session, query)

    def get_episodes(self, net: Network, session: SessionState,
                     series: Series) -> list[Episode]:
        base = session.base_url
        page_url = series.url(base)
        if series.type == "Film":
            return [Episode(season=1, number=1, url=page_url, title=series.name)]
        # Dizi: bölümler ajax ile geliyor (520); sayfadaki bölüm bağlantılarını
        # dene, bulunamazsa ana sayfanın sunduğu bölümü tek kayıt olarak ver.
        page = net.get(page_url, referer=base).text
        eps, seen = [], set()
        for url, s, n in re.findall(
                rf'href="({re.escape(page_url)}/sezon-(\d+)/bolum-(\d+)[^"]*)"', page):
            if url in seen:
                continue
            seen.add(url)
            eps.append(Episode(season=int(s), number=int(n), url=url))
        if eps:
            return sorted(eps, key=lambda e: (e.season, e.number))
        return [Episode(season=1, number=1, url=page_url, title=series.name)]

    def _resolve_embed(self, net: Network, session: SessionState,
                       embed: str, referer: str) -> MergedStream:
        if embed.startswith("//"):
            embed = "https:" + embed
        if "ok.ru" in embed:
            master, med_ref = _okru.resolve(net, embed, referer)
        elif "popcornvakti" in embed or "molystream" in embed:
            master, med_ref = _molystream.resolve(net, embed, referer)
        else:
            raise ExtractError(f"desteklenmeyen host: {embed[:50]}")
        _, tracks = get_av_urls(net, session, master, med_ref, "best")
        audios = [AudioSource(url=t.url, referer=med_ref, lang=(t.lang or "und"),
                              name=(t.name or "Ses"), is_turkish=t.is_turkish)
                  for t in tracks]
        for i, a in enumerate(audios):
            a.is_default = i == 0
        return MergedStream(video_master_url=master, video_referer=med_ref,
                            audios=audios, subtitle_url=None, subtitle_referer=med_ref)

    def build_stream(self, net: Network, session: SessionState,
                     episode: Episode, series: Series) -> MergedStream:
        base = session.base_url
        cookie = _udys()
        page = net.get(episode.url, referer=base, headers=cookie).text
        links = list(dict.fromkeys(_LINK_RE.findall(page)))
        if not links:
            raise ExtractError("yabancidizi: kaynak (data-link) bulunamadı.")

        # ok.ru kaynakları /api/ruplay'de, drive (popcornvakti) kaynakları
        # /api/drives'ta; token base64url'e çevrilir ve her ikisi de denenir.
        last_err: Exception | None = None
        for raw in links:
            tok = quote(_b64url(raw), safe="=-_")
            for ep in ("ruplay", "drives"):
                try:
                    api = f"{base}/api/{ep}/{tok}"
                    body = net.get(api, referer=episode.url, headers=cookie).text
                    m = _IFRAME_RE.search(body)
                    if not m:               # bu uç bu token için boş döndü
                        continue
                    return self._resolve_embed(net, session, m.group(1), api)
                except Exception as e:
                    last_err = e
                    continue
        raise ExtractError(f"yabancidizi: çalışan kaynak yok "
                           f"({len(links)} kaynak denendi): {last_err}")


register(YabancidiziAdapter())
