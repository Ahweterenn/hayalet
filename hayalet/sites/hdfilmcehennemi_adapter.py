"""hdfilmcehennemi.nl site adapter'ı — film odaklı, dublaj/orijinal ayrımı yok.

Zincir (canlı doğrulandı, saf Python / headless):
  1) GET /search?q=<sorgu> (header X-Requested-With: fetch) -> JSON {"results":[html,...]}
     — Dizipal'in temiz JSON alanlarından farklı olarak sonuçlar hazır HTML
     parçacıkları; href/başlık/tür regex ile ayıklanır.
  2) Film sayfası -> "alternative-link" butonları (data-video="<id>") -> her biri için
     GET /video/<id>/ -> {"data":{"html":"<iframe data-src=...>"}}. Site-içi
     (/rplayer/...) kaynak önceliklendirilir (harici .mobi domaine göre tek
     domain/TLS-oturumu kalır).
  3) /rplayer/<hash>/ sayfası JW Player kullanır; gerçek m3u8 URL'si paketlenmiş
     ("Dean Edwards packer", a=62 varyantı) bir JS bloğunda, üstüne kendi özel
     "unmix" şifrelemesiyle gizli: ters çevirme + harf-ROT-kaydırma + base64 çözme
     adımlarının sayısı VE sırası her sayfa yüklemesinde rastgele değişiyor (bkz.
     _parse_ops/_apply_ops) — son adım her zaman sabit bir sayısal bayt-kaydırma.
     Aynı sayfada düz (paketlenmemiş) bir `tracks:[...]` JSON'unda Türkçe altyazı
     da bulunur.
  4) Çözülen master.m3u8 zaten çoklu ses (Türkçe dublaj + orijinal) içeriyor —
     Dizipal'deki gibi ayrı dublaj/orijinal listelerini birleştirmeye gerek yok;
     generic m3u8_parser.get_av_urls ile doğrudan MergedStream kurulur.
"""
from __future__ import annotations

import base64
import html
import json
import re
from urllib.parse import urljoin, urlparse

from hayalet.core.extractor import ExtractError
from hayalet.core.m3u8_parser import get_av_urls
from hayalet.core.merge import AudioSource, MergedStream
from hayalet.core.models import Episode, Series
from hayalet.core.network import BlockedError, Network
from hayalet.core.resolver import ResolverError
from hayalet.core.session import SessionState
from hayalet.core.sites import register

_ALPHA = "0123456789abcdefghijklmnopqrstuvwxyz"


def _packer_unpack(p: str, a: int, c: int, k: list[str]) -> str:
    """Dean Edwards 'packer' açımlaması (a=62 varyantı) — sitenin embed
    sayfasındaki eval(function(p,a,c,k,e,d){...}(...)) bloğunu düzleştirir.
    """
    def tochar(x: int) -> str:
        return chr(x + 29) if x > 35 else _ALPHA[x]

    def enc(num: int) -> str:
        return (enc(num // a) if num >= a else "") + tochar(num % a)

    out = p
    i = c
    while i:
        i -= 1
        if k[i]:
            token = enc(i)
            out = re.sub(r"\b" + re.escape(token) + r"\b",
                         lambda m, val=k[i]: val, out)
    return out


_PACKER_RE = re.compile(
    r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.*)',(\d+),(\d+),'(.*)'\.split\('\|'\),0,\{\}\)\)",
    re.S,
)
_ARRAY_CALL_RE = re.compile(r'=\s*[A-Za-z0-9_$]+\(\[\s*((?:"[^"]*"\s*,\s*)*"[^"]*")\s*\]\)')


def _unescape_js_single_quoted(s: str) -> str:
    return s.replace("\\\\", "\x00").replace("\\'", "'").replace("\x00", "\\")


# 'unmix' algoritması üç işlem türünden oluşuyor — ters çevirme, harf-ROT-kaydırma,
# base64 çözme — ama bunların SAYISI ve SIRASI (birbirine göre) HER SAYFA
# YÜKLEMESİNDE RASTGELE DEĞİŞİYOR (canlı doğrulandı: 15 ayrı istekte hepsi farklı
# kombinasyon/sıradaydı — bazen rot→reverse→atob, bazen atob→rot→rot→atob vb.).
# Bu yüzden "N kere şunu, sonra M kere bunu" gibi sabit bir sıra varsayılamaz;
# işlemler kaynak koddaki GÖRÜNME SIRASINA göre ayrıştırılıp uygulanır. Tek sabit
# ve her zaman son adım, sayısal (magic/offset) bayt-kaydırma karıştırmasıdır.
_OP_RE = re.compile(
    r"result=result\.split\(''\)\.reverse\(\)\.join\(''\)"
    r"|result=result\.replace\(/\[a-zA-Z\]/g,function\(c\)\{var o=c\.charCodeAt\(0\),base=\(o<=90\)\?65:97;"
    r"return String\.fromCharCode\(\(o-base\+(\d+)\)%26\+base\)\}\)"
    r"|result=atob\(result\)"
)
_MAGIC_RE = re.compile(r"\((\d+)%\(i\+(\d+)\)\)")


def _parse_ops(unpacked_src: str) -> list[tuple[str, int | None]]:
    ops: list[tuple[str, int | None]] = []
    for m in _OP_RE.finditer(unpacked_src):
        text = m.group(0)
        if m.group(1) is not None:
            ops.append(("rot", int(m.group(1))))
        elif "atob" in text:
            ops.append(("atob", None))
        else:
            ops.append(("reverse", None))
    return ops


def _apply_ops(text: str, ops: list[tuple[str, int | None]]) -> str:
    for kind, param in ops:
        if kind == "reverse":
            text = text[::-1]
        elif kind == "rot":
            out = []
            for ch in text:
                if "A" <= ch <= "Z":
                    out.append(chr((ord(ch) - 65 + param) % 26 + 65))
                elif "a" <= ch <= "z":
                    out.append(chr((ord(ch) - 97 + param) % 26 + 97))
                else:
                    out.append(ch)
            text = "".join(out)
        elif kind == "atob":
            text = base64.b64decode(text.encode("latin-1")).decode("latin-1")
    return text


def _unmix(parts: list[str], unpacked_src: str) -> str:
    magic_m = _MAGIC_RE.search(unpacked_src)
    ops = _parse_ops(unpacked_src)
    if not magic_m or not ops:
        raise ExtractError("hdfilmcehennemi: unmix parametreleri çözülemedi (site güncellenmiş olabilir).")
    magic, offset = int(magic_m.group(1)), int(magic_m.group(2))

    text = _apply_ops("".join(parts), ops)
    out = bytearray((ord(ch) - (magic % (i + offset))) % 256
                    for i, ch in enumerate(text))
    return out.decode("utf-8")


def _extract_master_url(embed_html: str) -> str:
    m = _PACKER_RE.search(embed_html)
    if not m:
        raise ExtractError("hdfilmcehennemi: paketlenmiş player script'i bulunamadı.")
    p_raw, a_s, c_s, k_raw = m.groups()
    unpacked = _packer_unpack(_unescape_js_single_quoted(p_raw), int(a_s), int(c_s),
                              k_raw.split("|"))
    am = _ARRAY_CALL_RE.search(unpacked)
    if not am:
        raise ExtractError("hdfilmcehennemi: kaynak dizisi (unmix girdisi) bulunamadı.")
    parts = re.findall(r'"([^"]*)"', am.group(1))
    return _unmix(parts, unpacked)


def _extract_turkish_vtt(embed_html: str, origin: str) -> str | None:
    for block_m in re.finditer(r'\{[^{}]*"kind"\s*:\s*"captions"[^{}]*\}', embed_html):
        block = block_m.group(0)
        if '"language":"tr"' not in block.replace(" ", ""):
            continue
        file_m = re.search(r'"file"\s*:\s*"([^"]+\.vtt)"', block)
        if file_m:
            return urljoin(origin, file_m.group(1).replace("\\/", "/"))
    return None


_SERIES_TYPES = {"dizi", "series"}


class HDFCAdapter:
    name = "hdfilmcehennemi"
    known_domain = "https://www.hdfilmcehennemi.nl"

    def resolve_domain(self, net: Network, session: SessionState,
                       override: str | None = None, use_cache: bool = True) -> str:
        # Tek ve sabit domain — Dizipal'in artan-sayı taraması burada gerekmiyor.
        domain = (override or self.known_domain).rstrip("/")
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
        resp = net.get(session.base_url + "/search", params={"q": query},
                       referer=session.base_url,
                       headers={"Content-Type": "application/json",
                                "X-Requested-With": "fetch"})
        try:
            data = resp.json()
        except Exception:
            data = json.loads(resp.text)

        out: list[Series] = []
        for frag in data.get("results") or []:
            href_m = re.search(r'<a href="([^"]+)"', frag)
            title_m = re.search(r'<h4 class="title">([^<]+)</h4>', frag)
            if not href_m or not title_m:
                continue
            type_m = re.search(r'<span class="type">([^<]+)</span>', frag)
            typ = type_m.group(1).strip() if type_m else "Film"
            if typ.lower() in _SERIES_TYPES:
                # İş bölümü: diziler dizipal'in işi. Ayrıca bu adapter'ın
                # get_episodes/build_stream'i yalnızca film sayfası yapısını
                # çözebiliyor — bir diziye uygulanırsa zaten patlardı.
                continue
            slug = urlparse(href_m.group(1)).path.strip("/")
            if not slug:
                continue
            out.append(Series(name=html.unescape(title_m.group(1)).strip(), slug=slug,
                              type=typ, site=self.name))
        return out

    def suggest(self, net: Network, session: SessionState, query: str) -> list[Series]:
        # Sitenin kendi /search'ü zaten gevşek/kısmi eşleşme döndürüyor (canlı
        # doğrulandı) — Dizipal'deki kelime-kelime+difflib önerisine gerek yok.
        return self.search(net, session, query)

    def get_episodes(self, net: Network, session: SessionState,
                     series: Series) -> list[Episode]:
        # Site salt film odaklı — sezon/bölüm yok, sentetik tek "bölüm".
        return [Episode(season=1, number=1, url=series.url(session.base_url),
                        title=series.name)]

    def build_stream(self, net: Network, session: SessionState,
                     episode: Episode, series: Series) -> MergedStream:
        page = net.get(episode.url, referer=session.base_url).text
        video_ids = re.findall(r'data-video="(\d+)"', page)
        if not video_ids:
            raise ExtractError("hdfilmcehennemi: kaynak butonu (data-video) bulunamadı.")

        embed_url = None
        for vid in video_ids:
            resp = net.get(f"{session.base_url}/video/{vid}/", referer=episode.url,
                           headers={"Content-Type": "application/json",
                                    "X-Requested-With": "fetch"})
            try:
                data = resp.json()
            except Exception:
                continue
            html = (data.get("data") or {}).get("html") or ""
            src_m = re.search(r'data-src="([^"]+)"', html)
            if not src_m:
                continue
            url = src_m.group(1)
            if urlparse(url).netloc == urlparse(session.base_url).netloc:
                embed_url = url
                break                     # site-içi bulundu, harici alternatifleri dene(me)
            if embed_url is None:
                embed_url = url           # yalnızca site-içi hiç yoksa harici (.mobi) kullan

        if not embed_url:
            raise ExtractError("hdfilmcehennemi: oynatıcı embed URL'si bulunamadı.")

        embed_html = net.get(embed_url, referer=episode.url).text
        m3u8_url = _extract_master_url(embed_html)
        subtitle_url = _extract_turkish_vtt(embed_html, embed_url)

        _, tracks = get_av_urls(net, session, m3u8_url, embed_url, "best")
        audios = [AudioSource(url=t.url, referer=embed_url, lang=(t.lang or "und"),
                              name=(t.name or "Ses"), is_turkish=t.is_turkish)
                 for t in tracks]
        if not audios:
            # Ayrı ses rendition'ı yoksa (nadiren) videonun kendisi tek ses kabul edilir.
            audios = [AudioSource(url=m3u8_url, referer=embed_url, lang="und",
                                  name="Ses", is_turkish=False)]
        for a in audios:
            a.is_default = False
        audios[0].is_default = True      # get_av_urls Türkçe'yi zaten başa sıralar

        return MergedStream(video_master_url=m3u8_url, video_referer=embed_url,
                            audios=audios, subtitle_url=subtitle_url,
                            subtitle_referer=embed_url)


register(HDFCAdapter())
