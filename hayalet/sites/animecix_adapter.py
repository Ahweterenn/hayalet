"""animecix.tv site adapter'ı (anime — Türkçe altyazılı/fansub).

Saf JSON uygulama API'si (CF yok). Zincir (canlı doğrulandı, 2026-11):
  GET /secure/search/<q>?limit=20
      -> {"results":[{"id","name","title_type","year","poster"}]}
  GET /secure/episode-videos?titleId=<id>&episode=N&season=N
      -> [{"name","language","type":"embed","url"}]   (fansub oynatıcı embed'leri)
  tau-video embed -> _tauvideo.resolve -> DOĞRUDAN MP4 (kind="file")

**Bölüm listesi neden yoklamayla çıkarılıyor:** başlık detay ucu
(`/secure/titles/<id>`) browserless istemciye auth/token ister ve id ne olursa
olsun sabit bir default başlık döndürür (cloaking) — sezon/bölüm sayısı oradan
ALINAMAZ. `episode-videos` ise titleId'e doğru yanıt verir ama sayı vermez. Bu
yüzden sezon/bölüm sayıları `episode-videos`'u ÜSSEL + İKİLİ ARAMAYLA yoklayarak
bulunur (~log₂(N) istek; One Piece 1000+ bölüm, Frieren 28 — ikisi de doğrulandı).
Bölümlerin bitişik olduğu varsayılır (anime fansub'ında olağan); boş yanıt = son.

Anime altyazısı videoya gömülüdür → ayrı altyazı/ses kanalı yok, dublaj yok.
"""
from __future__ import annotations

import html
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
from hayalet.sites import _tauvideo, _uqload

_API_JSON = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
# Üssel yoklamada ulaşılabilecek en yüksek bölüm/sezon (sonsuz döngü koruması).
_MAX_EPISODE = 5000
_MAX_SEASON = 40


class AnimecixAdapter:
    name = "animecix"
    known_domain = "https://animecix.tv"

    # ---- bağlantı ----
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

    # ---- arama ----
    def search(self, net: Network, session: SessionState, query: str) -> list[Series]:
        base = session.base_url
        url = f"{base}/secure/search/{quote(query)}?limit=20"
        try:
            results = net.get(url, referer=base, headers=_API_JSON).json().get("results") or []
        except Exception as e:
            raise ExtractError(f"animecix: arama yanıtı çözülemedi: {e}")
        out, seen = [], set()
        for it in results:
            tid = it.get("id")
            name = it.get("name")
            if not tid or not name or tid in seen:
                continue
            seen.add(tid)
            is_movie = str(it.get("title_type", "")).lower() == "movie"
            out.append(Series(
                name=html.unescape(str(name)).strip(),
                slug=f"t/{tid}",                 # titleId'i taşır (url() kullanılmaz)
                type="Film" if is_movie else "Dizi",
                site=self.name,
                year=str(it.get("year") or ""),
                poster_url=it.get("poster") or ""))
        return out

    def suggest(self, net: Network, session: SessionState, query: str) -> list[Series]:
        return self.search(net, session, query)

    # ---- bölüm listesi ----
    def _title_id(self, series: Series) -> str:
        return series.slug.split("/", 1)[-1]

    def _ep_url(self, base: str, tid: str, season: int, number: int) -> str:
        return (f"{base}/secure/episode-videos"
                f"?titleId={tid}&episode={number}&season={season}")

    def _has_videos(self, net: Network, base: str, tid: str,
                    season: int, number: int) -> bool:
        try:
            data = net.get(self._ep_url(base, tid, season, number),
                           referer=base, headers=_API_JSON).json()
        except Exception:
            return False
        return isinstance(data, list) and len(data) > 0

    def _last_episode(self, net: Network, base: str, tid: str, season: int) -> int:
        """O sezonun son (dolu) bölüm numarası; üssel + ikili arama. 0 = sezon yok."""
        def present(n: int) -> bool:
            return self._has_videos(net, base, tid, season, n)

        if not present(1):
            return 0
        lo, hi = 1, 2
        while present(hi):          # üssel: doluyu geçene kadar ikiye katla
            lo = hi
            hi *= 2
            if hi > _MAX_EPISODE:
                return lo
        # değişmez: present(lo)=True, present(hi)=False → son dolu [lo, hi)'de
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if present(mid):
                lo = mid
            else:
                hi = mid
        return lo

    def get_episodes(self, net: Network, session: SessionState,
                     series: Series) -> list[Episode]:
        base = session.base_url
        tid = self._title_id(series)
        if series.type == "Film":
            return [Episode(season=1, number=1,
                            url=self._ep_url(base, tid, 1, 1), title=series.name)]
        eps: list[Episode] = []
        for season in range(1, _MAX_SEASON + 1):
            last = self._last_episode(net, base, tid, season)
            if last == 0:                       # bu sezon yok → dizi bitti
                break
            for n in range(1, last + 1):
                eps.append(Episode(season=season, number=n,
                                   url=self._ep_url(base, tid, season, n)))
        if not eps:
            raise ExtractError("animecix: bölüm bulunamadı.")
        return eps

    # ---- akış ----
    def build_stream(self, net: Network, session: SessionState,
                     episode: Episode, series: Series) -> MergedStream:
        base = session.base_url
        try:
            videos = net.get(episode.url, referer=base, headers=_API_JSON).json()
        except Exception as e:
            raise ExtractError(f"animecix: bölüm kaynakları alınamadı: {e}")
        embeds = [v.get("url") for v in (videos or [])
                  if v.get("type") == "embed" and v.get("url")]
        last_err: Exception | None = None

        # 1) tau-video önce: doğrudan MP4, en sağlam/yaygın kaynak.
        for u in (e for e in embeds if "tau-video" in e):
            try:
                mp4 = _tauvideo.resolve(net, u, base)
                return MergedStream(video_master_url=mp4, video_referer=base,
                                    audios=[], subtitle_url=None, kind="file")
            except ExtractError as e:
                last_err = e

        # 2) uqload yedeği: packer → HLS master (bölümde tau-video yoksa).
        for u in (e for e in embeds if "uqload" in e):
            try:
                master, med_ref, sub = _uqload.resolve(net, u, base)
                _, tracks = get_av_urls(net, session, master, med_ref, "best")
                audios = [AudioSource(url=t.url, referer=med_ref,
                                      lang=(t.lang or "und"), name=(t.name or "Ses"),
                                      is_turkish=t.is_turkish) for t in tracks]
                for i, a in enumerate(audios):
                    a.is_default = i == 0
                return MergedStream(video_master_url=master, video_referer=med_ref,
                                    audios=audios, subtitle_url=sub,
                                    subtitle_referer=med_ref, kind="hls")
            except ExtractError as e:
                last_err = e

        if embeds:
            # sibnet (bize 403/WARP) ve doodstream (anti-bot) henüz desteksiz.
            raise ExtractError(
                "animecix: oynatılabilir kaynak çözülemedi (tau-video/uqload yok "
                f"ya da düştü). Hostlar: {[u.split('/')[2] for u in embeds][:5]}. "
                f"Son hata: {last_err}")
        raise ExtractError(f"animecix: oynatılabilir kaynak yok ({last_err}).")


register(AnimecixAdapter())
