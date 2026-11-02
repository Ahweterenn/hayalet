"""dizibox.live site adapter'ı (yabancı diziler, WordPress "dbx18" teması).

Eskiden vardı, "yavaş" diye kaldırılmıştı; geri getirildi ve oynatıcı zinciri
güncellendi (molystream katmanı eklendi).

WordPress'in `/?s=<q>` araması ÇOK YAVAŞ (20 sn+ timeout — sitenin eskiden
kaldırılma sebebi buydu). Onun yerine `/arama/` tüm dizi arşivini (~4760 kayıt)
0,7 sn'de veriyor; arama istemci tarafında bu listeyi süzerek yapılıyor.

Zincir (canlı doğrulandı, 2026-11):
  GET /arama/                               -> tüm diziler: <a title="X izle" .../diziler/<slug>/>
  GET /diziler/<slug>/                      -> /<slug>-N-sezon-M-bolum-izle/ bölüm linkleri
  GET /<slug>-N-sezon-M-bolum-izle/         -> TEK TIRNAKLI iframe '…/player/king/king.php?v=<hash>'
  GET …/player/king/king.php?v=<hash>       -> iç iframe 'dbx.molystream.org/embed/<id>'
  -> _molystream.resolve -> /embed/sheila/<id> = master m3u8
"""
from __future__ import annotations

import html
import re
from urllib.parse import urlparse

from hayalet.core import remote
from hayalet.core.extractor import ExtractError
from hayalet.core.m3u8_parser import get_av_urls
from hayalet.core.merge import AudioSource, MergedStream
from hayalet.core.models import Episode, Series
from hayalet.core.network import BlockedError, Network
from hayalet.core.resolver import ResolverError
from hayalet.core.session import SessionState
from hayalet.core.sites import register
from hayalet.sites import _molystream

# Arşiv kartı: <a title="X izle" href=".../diziler/<slug>/">
_CARD_RE = re.compile(
    r'<a title="([^"]+?)\s+izle"\s+href="(https?://[^"]+/diziler/[^"/]+/)"', re.I)

# Tüm dizi arşivi (~4760 kayıt, ~785 KB) süreç başına BİR kez çekilir; sonraki
# aramalar bellekteki listeyi süzer (anında). base_url -> [(ad, slug)].
_archive_cache: dict[str, list[tuple[str, str]]] = {}
_ARCHIVE_TIMEOUT = 40   # büyük sayfa, yük altında yavaş olabilir — bol zaman tanı
# iframe (tek VEYA çift tırnak) — site tek tırnak kullanıyor, bu yüzden şart.
_IFRAME_RE = re.compile(r'''<iframe[^>]+src=["']([^"']+)["']''', re.I)


class DiziboxAdapter:
    name = "dizibox"
    known_domain = "https://www.dizibox.live"
    # Mobil UA'da (iPhone/Android) site arşiv/sayfaları BOŞ (~140 bayt) dönüyor;
    # yalnız masaüstü kimlikleri tam içeriği alıyor (canlı doğrulandı 2026-11).
    impersonates = ("chrome", "edge", "firefox", "safari")

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

    def _archive(self, net: Network, session: SessionState) -> list[tuple[str, str]]:
        """Tüm dizi arşivini (ad, slug) olarak döndürür; süreç başına cache'li."""
        base = session.base_url
        cached = _archive_cache.get(base)
        if cached is not None:
            return cached
        # WP araması (/?s=) çok yavaş; bunun yerine tam arşiv (bkz. modül başı).
        resp = net.get(base + "/arama/", referer=base, timeout=_ARCHIVE_TIMEOUT)
        items, seen = [], set()
        for title, href in _CARD_RE.findall(resp.text):
            slug = urlparse(href).path.strip("/")
            if slug and slug not in seen:
                seen.add(slug)
                items.append((html.unescape(title).strip(), slug))
        if items:                       # boş geldiyse cache'leme, sonra tekrar dene
            _archive_cache[base] = items
        return items

    def search(self, net: Network, session: SessionState, query: str) -> list[Series]:
        q = query.casefold().strip()
        out = []
        for name, slug in self._archive(net, session):
            if not q or q in name.casefold():
                out.append(Series(name=name, slug=slug, type="Dizi", site=self.name))
        return out

    def suggest(self, net: Network, session: SessionState, query: str) -> list[Series]:
        return self.search(net, session, query)

    def get_episodes(self, net: Network, session: SessionState,
                     series: Series) -> list[Episode]:
        base = session.base_url
        page = net.get(series.url(base), referer=base).text
        # Dizinin KENDİ bölümleri slug ile başlar (dizi kök sayfasının kenar
        # çubuğu başka dizilerin bölümlerini listeliyor — slug ile elenir).
        stub = series.slug.split("diziler/")[-1].strip("/")
        # Bazı slug'lar "-izle" / "-izle-1" ekli (lost-izle-1); bölüm URL'leri
        # bu eki taşımaz (lost-1-sezon-1-bolum-izle) → eki atıp öyle kapsa.
        stub = re.sub(r"-izle(-\d+)?$", "", stub)
        own_re = re.compile(
            rf'href="(https?://[^"]+/{re.escape(stub)}-(\d+)-sezon-(\d+)-bolum[^"]*)"', re.I)
        episodes, seen = [], set()
        for url, season, number in own_re.findall(page):
            if url in seen:
                continue
            seen.add(url)
            episodes.append(Episode(season=int(season), number=int(number), url=url))
        return sorted(episodes, key=lambda e: (e.season, e.number))

    def build_stream(self, net: Network, session: SessionState,
                     episode: Episode, series: Series) -> MergedStream:
        base = session.base_url
        page = net.get(episode.url, referer=base).text
        # 1) king.php oynatıcı iframe'i (varsa onu tercih et, yoksa YouTube trailer'ı atla)
        iframes = [u for u in _IFRAME_RE.findall(page) if "youtube" not in u.lower()]
        king = next((u for u in iframes if "king.php" in u), iframes[0] if iframes else None)
        if not king:
            raise ExtractError("dizibox: oynatıcı iframe'i bulunamadı.")
        if king.startswith("//"):
            king = "https:" + king

        # 2) king.php -> iç iframe (molystream embed)
        kh = net.get(king, referer=episode.url).text
        inner = _IFRAME_RE.search(kh)
        if not inner:
            raise ExtractError("dizibox: king.php içinde kaynak iframe'i yok.")
        embed = inner.group(1)
        if embed.startswith("//"):
            embed = "https:" + embed

        # 3) molystream -> master m3u8
        if "molystream" not in embed:
            raise ExtractError(f"dizibox: bilinmeyen kaynak host'u: {embed[:60]}")
        master, referer = _molystream.resolve(net, embed, king)

        _, tracks = get_av_urls(net, session, master, referer, "best")
        audios = [AudioSource(url=t.url, referer=referer, lang=(t.lang or "und"),
                              name=(t.name or "Ses"), is_turkish=t.is_turkish)
                  for t in tracks]
        for i, a in enumerate(audios):
            a.is_default = i == 0
        return MergedStream(video_master_url=master, video_referer=referer,
                            audios=audios, subtitle_url=None, subtitle_referer=referer)


register(DiziboxAdapter())
