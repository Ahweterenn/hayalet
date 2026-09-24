"""hdfilmcehennemi.nl site adapter'ı — film VE dizi, dublaj/orijinal ayrımı yok.

Diziler bir dönem bilerek elenirdi ("diziler dizipal'in işi"); Dizipal'in tüm
domain ailesi erişilemez hale gelince bu iş bölümü anlamsızlaştı. Sitenin dizi
sayfaları film sayfalarıyla AYNI oynatıcı zincirini kullandığı için (canlı
doğrulandı: bir bölüm URL'sine build_stream hiç değiştirilmeden uygulandı ve
master m3u8 + Türkçe altyazı çözüldü) tek eklenen şey bölüm listeleme oldu.

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
from hayalet.core.utils import poster_url_from_html
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



# --- unmix "2. nesil" (2026-09) --------------------------------------------
# Site paketlenmiş script içine artık İKİ decode fonksiyonu koyuyor: biri
# TUZAK (eski şemaya kasıtlı benzer — üstteki _OP_RE/_ARRAY_CALL_RE'nin
# aradığı şekle uyar ama çıktısı hiçbir yerde kullanılmaz), diğeri GERÇEK.
# Hangisinin gerçek olduğu fonksiyonun YAPISINDAN değil, embed sayfasındaki
# `sources:[{file:X}]` ifadesinin hangi değişkeni (X) referans aldığından
# anlaşılıyor — bu sinyal tuzağın şekli değişse de sağlam kalır (canlı iki
# ayrı film/site yüklemesiyle doğrulandı: aynı yapı, farklı değişken adları).
#
# Gerçek fonksiyonun algoritması (iki bağımsız örnekte SAYISAL SABİTLER de
# birebir aynı çıktı — 7/5/8, 37/241, 3/11/5/251/65519, işaret karakterleri
# '7'/'3', ROT tabanı 96, LCG 97/41, akış çarpanı 5). Yine de dosyanın genel
# felsefesiyle tutarlı olsun diye bunları HARDCODE ETMİYORUZ — hepsi kaynak
# metninden regex ile çıkarılıyor; sabitler bir gün değişirse (yapı aynı
# kaldığı sürece) kod otomatik uyum sağlar:
#   1. ARR.length-2'den iki indeks türetilir (mod N1, ve N2+ekli mod ile);
#      ARR'dan bu iki eleman splice() ile SIRAYLA çıkarılır — biri "OP"
#      (ters sırada gezilen atob/reverse/ROT işaretleri), diğeri "KEY".
#   2. Kalan dizi join edilip PAYLOAD olur; KEY üzerinden özel bir hash
#      (mod-çarpım + XOR) çalıştırılıp 3 türetilmiş sabit üretilir.
#   3. OP karakterleri TERS sırada PAYLOAD'a uygulanır (atob / reverse / ROT).
#   4. LCG (lineer eşlenik üreteç) ile bir permütasyon üretilip PAYLOAD
#      karıştırılır (Fisher-Yates benzeri ama üretici indeks listesiyle).
#   5. Son adım: akan bir XOR şifre çözme (her baytta çarpım+toplama ile
#      güncellenen bir sayaçla XOR'lanır).
_GEN2_REAL_VAR_RE = re.compile(r'sources:\s*\[\{file:(\w+)\}')
_GEN2_SPLICE_POS_RE = re.compile(
    r'var (\w+)=(\w+)\.length-2,(\w+)=\1%(\d+),(\w+)=(\d+)\+\(\1%(\d+)\)')
_GEN2_HASH_RE = re.compile(
    r'(\w+)=\((\w+)\*(\d+)\+(\w+)\)%(\d+);(\w+)=\((\w+)\+\(\((\w+)<<1\)\^(\w+)\)\)&255')
_GEN2_DERIVED_RE = re.compile(
    r'(\w+)=\((\w+)\*(\d+)\+(\w+)\)%256,(\w+)=\((\w+)%(\d+)\)\+(\d+),'
    r'(\w+)=\(\((\w+)\*(\d+)\+(\w+)\)%(\d+)\)\+1')
_GEN2_OPS_MARK_RE = re.compile(
    r"if\((\w+)==='(.)'\)\{(\w+)=atob\(\3\)\}else if\(\1==='(.)'\)\{"
    r"\3=\3\.split\(''\)\.reverse\(\)\.join\(''\)\}else\{\w+=\(26-\(\(\1\."
    r"charCodeAt\(0\)-(\d+)\)%26\)\)%26;")
_GEN2_LCG_RE = re.compile(
    r'(\w+)=\((\w+)\*(\d+)\+(\d+)\)%(\d+);\w+\[\w+\]=\w+%\(\w+\+1\)')
_GEN2_STREAM_RE = re.compile(
    r"(\w+)=(\w+),(\w+)=''.*?for\(.*?\)\{\w+=\w+\.charCodeAt\(\w+\);"
    r"\1=\(\1\*(\d+)\+(\w+)\)%256;\3\+=String\.fromCharCode\(\w+\^\1\);"
    r"\1=\(\1\+\w+\)%256\}")


def _find_function_body(src: str, name: str) -> str | None:
    """`function NAME(P){...}` ya da `var NAME=function(P){...}` gövdesini
    dengeli süslü parantezle (regex balanced-brace yakalayamadığı için elle)
    ayıklar."""
    m = re.search(r'(?:var\s+' + re.escape(name) + r'\s*=\s*function'
                 r'|function\s+' + re.escape(name) + r')\s*\(\w+\)\s*\{', src)
    if not m:
        return None
    depth, start = 0, m.end() - 1
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start + 1:i]
    return None


def _gen2_decode(arr: list[str], body: str) -> str:
    pos_m = _GEN2_SPLICE_POS_RE.search(body)
    hash_m = _GEN2_HASH_RE.search(body)
    der_m = _GEN2_DERIVED_RE.search(body)
    ops_m = _GEN2_OPS_MARK_RE.search(body)
    lcg_m = _GEN2_LCG_RE.search(body)
    stream_m = _GEN2_STREAM_RE.search(body, pos=0)
    if not (pos_m and hash_m and der_m and ops_m and lcg_m and stream_m):
        raise ExtractError("hdfilmcehennemi: 2. nesil unmix parametreleri çözülemedi "
                          "(site güncellenmiş olabilir).")

    _, _, _, n1, addvar, add_base, n2 = pos_m.groups()
    n1, add_base, n2 = int(n1), int(add_base), int(n2)
    h1_mul, h1_mod = int(hash_m.group(3)), int(hash_m.group(5))
    dg = der_m.groups()
    d1_mul, d2_mod, d2_add = int(dg[2]), int(dg[6]), int(dg[7])
    d3_mul, d3_mod = int(dg[10]), int(dg[12])
    atob_mark, rev_mark, rot_base = ops_m.group(2), ops_m.group(4), int(ops_m.group(5))
    lcg_mul, lcg_add, lcg_mod = int(lcg_m.group(3)), int(lcg_m.group(4)), int(lcg_m.group(5))
    stream_mul = int(stream_m.group(4))

    arr = list(arr)
    nh1 = len(arr) - 2
    modpos = nh1 % n1
    addpos = add_base + (nh1 % n2)
    # splice() SIRAYLA aynı diziyi mutasyona uğratır — JS'teki gibi ADD
    # pozisyonu ÖNCE, MOD pozisyonu SONRA çıkarılmalı (aksi halde ikinci
    # indeks yanlış elemanı işaret eder).
    op = arr.pop(addpos)
    key = arr.pop(modpos)
    payload = "".join(arr)

    if len(key) > 4096:
        payload = base64.b64decode(payload).decode("latin-1")

    h1 = h2 = 0
    for i, ch in enumerate(key):
        c = ord(ch)
        h1 = (h1 * h1_mul + c) % h1_mod
        h2 = (h2 + ((c << 1) ^ i)) & 255

    d1 = (h1 * d1_mul + h2) % 256
    d2 = (h2 % d2_mod) + d2_add
    d3 = ((h2 * d3_mul + h1) % d3_mod) + 1

    for ch in reversed(op):
        if ch == atob_mark:
            payload = base64.b64decode(payload).decode("latin-1")
        elif ch == rev_mark:
            payload = payload[::-1]
        else:
            shift = (26 - ((ord(ch) - rot_base) % 26)) % 26
            out = []
            for c in payload:
                if "A" <= c <= "Z":
                    out.append(chr((ord(c) - 65 + shift) % 26 + 65))
                elif "a" <= c <= "z":
                    out.append(chr((ord(c) - 97 + shift) % 26 + 97))
                else:
                    out.append(c)
            payload = "".join(out)
    if len(op) > 2048:
        payload = payload[::-1]

    n = len(payload)
    idx = [0] * n
    seed = d3
    for i in range(n - 1, 0, -1):
        seed = (seed * lcg_mul + lcg_add) % lcg_mod
        idx[i] = seed % (i + 1)
    chars = list(payload)
    for i in range(1, n):
        j = idx[i]
        chars[i], chars[j] = chars[j], chars[i]
    payload = "".join(chars)

    stream = d1
    out = []
    for ch in payload:
        c = ord(ch)
        stream = (stream * stream_mul + d2) % 256
        out.append(chr(c ^ stream))
        stream = (stream + c) % 256
    return "".join(out)


def _extract_master_url_v2(embed_html: str, unpacked: str) -> str:
    real_m = _GEN2_REAL_VAR_RE.search(embed_html)
    if not real_m:
        raise ExtractError("hdfilmcehennemi: gerçek kaynak değişkeni bulunamadı.")
    real_var = real_m.group(1)

    call_m = re.search(r"var\s+" + re.escape(real_var) + r"\s*=\s*(\w+)\((.*?)\);", unpacked)
    if not call_m:
        raise ExtractError("hdfilmcehennemi: gerçek çözücü çağrısı bulunamadı.")
    func_name, args_text = call_m.groups()

    m_str = re.match(r'"(.*)"\.split\("(.*)"\)\s*$', args_text)
    if m_str:
        big, sep = m_str.groups()
        arr = big.split(sep)
    else:
        arr = re.findall(r'"([^"]*)"', args_text)
    if not arr:
        raise ExtractError("hdfilmcehennemi: kaynak dizisi (2. nesil) bulunamadı.")

    body = _find_function_body(unpacked, func_name)
    if not body:
        raise ExtractError("hdfilmcehennemi: çözücü fonksiyon gövdesi bulunamadı.")
    return _gen2_decode(arr, body)


def _extract_master_url(embed_html: str) -> str:
    m = _PACKER_RE.search(embed_html)
    if not m:
        raise ExtractError("hdfilmcehennemi: paketlenmiş player script'i bulunamadı.")
    p_raw, a_s, c_s, k_raw = m.groups()
    unpacked = _packer_unpack(_unescape_js_single_quoted(p_raw), int(a_s), int(c_s),
                              k_raw.split("|"))

    # 2026-09: site "2. nesil" şemaya geçti (bkz. yukarıdaki blok) — önce onu
    # dene. Eski tek-fonksiyonlu şema hâlâ bir mirror/eski önbellekte
    # karşımıza çıkarsa (embed_html'de sources:[{file:X}] hiç yoksa ya da
    # 2. nesil ayrıştırma başarısız olursa) eski yola düş.
    try:
        return _extract_master_url_v2(embed_html, unpacked)
    except ExtractError:
        pass

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

# --- katalog gezinme (ana sayfa rafları + Diziler/Filmler listeleri) --------
# Listeleme sayfalarındaki kart işaretlemesi; ana sayfa, tür sayfaları ve
# "Filmler"/"Diziler" listeleri AYNI kalıbı kullanıyor (canlı doğrulandı), o
# yüzden tek ayrıştırıcı hepsine yetiyor. `title` niteliği tam (iki dilli) adı
# taşır — kart içindeki <strong class="poster-title"> yalnız kısa adı verir.
_POSTER_RE = re.compile(
    r'<a\s+href="([^"]+)"[^>]*?title="([^"]*)"[^>]*?class="poster[^"]*"(.*?)</a>',
    re.S)
_ANY_POSTER_RE = re.compile(
    r'<a\b([^>]*\bclass\s*=\s*["\'][^"\']*poster[^"\']*["\'][^>]*)>(.*?)</a>',
    re.S | re.I)
# Bazı raflar (ör. "Nette İlk", "Popüler Diziler") büyük kart yerine küçük
# "mini-poster" kartı kullanıyor: yıl/puan yok, ad <h4> içinde.
_MINI_RE = re.compile(
    r'<a\s+href="([^"]+)"[^>]*?class="mini-poster"(.*?)</a>', re.S)
_MINI_TITLE_RE = re.compile(r'class="mini-poster-title"[^>]*>([^<]+)<')
_YEAR_RE = re.compile(r"<span>\s*(\d{4})\s*</span>")
_IMDB_RE = re.compile(r'class="imdb"[^>]*>\s*([\d.]+)')
_IMG_RE = re.compile(r'<img[^>]+(?:data-src|src)="([^"]+)"', re.I)
_SVG_RE = re.compile(r"<svg.*?</svg>", re.S)
# Raf başlığı: bölüm başlığı ya da (sekmeli bölümde) etkin sekmenin adı.
_LABEL_RE = re.compile(
    r'class="section-title"[^>]*>(.*?)</h[0-9]>'
    r'|class="section-tab active"[^>]*>([^<]{2,60})', re.S)
# Vizyona girmemiş yapımların arkasında video yok — rafta gösterip kullanıcıyı
# ExtractError'a düşürmenin anlamı yok (canlı: "vizyona girmemiş film" hatası).
_SKIP_ROWS = ("yakında",)
# Gezinme menüsündeki liste sayfaları. Slug'lardaki sayı ekleri (-2, -5) zaman
# içinde değişebildiği için önce ana sayfanın menüsünden okunur; bunlar yedek.
_FALLBACK_LISTS = {"film": "/category/film-izle-2/", "dizi": "/yabancidiziizle-5/"}
_NAV_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>\s*(Filmler|Diziler)\s*</a>')
# Tür sayfaları: ana sayfadaki "Türlerine Göre Filmler" bölümünden okunur —
# slug'lardaki sayı ekleri (-7, -844) zaman içinde değiştiği için elle liste
# tutmak bakım yükü olurdu. Adı "... Filmleri" ekinden temizleyip gösteriyoruz.
_GENRE_RE = re.compile(r'<a[^>]+href="([^"]*/tur/[^"]+)"[^>]*>([^<]{2,40}?)\s*Filmleri\s*</a>')


def _poster(fragment: str, base_url: str) -> str:
    """HDFC kartlarında büyük poster/lazy ve background alanlarını seçer."""
    return poster_url_from_html(fragment, base_url)


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
            slug = urlparse(href_m.group(1)).path.strip("/")
            if not slug:
                continue
            poster = _poster(frag, session.base_url)
            out.append(Series(name=html.unescape(title_m.group(1)).strip(), slug=slug,
                              type=typ, site=self.name, poster_url=poster))
        return out

    def suggest(self, net: Network, session: SessionState, query: str) -> list[Series]:
        """Son çare: sorguyu kelimelerine bölüp tek tek arar.

        Sitenin /search'ü kısmi eşleşme döndürür ama **birebir alt dizi** arar;
        "spiderman" hiçbir şey bulmazken "spider-man" buluyor (canlı doğrulandı).
        `sites.search_site` zaten yazım varyantlarını deniyor; burası ondan da
        sonra gelen adım, o yüzden sorguyu parçalayıp en uzun kelimeden başlayarak
        ayrı ayrı aramak dışında yapacak bir şey kalmıyor.
        """
        from hayalet.core import query as q

        words = sorted(set(q.tokens(query)), key=len, reverse=True)
        found: dict[str, Series] = {}
        for w in words[:3]:
            if len(w) < 3:
                continue
            try:
                for r in self.search(net, session, w):
                    found.setdefault(r.slug, r)
            except Exception:
                continue
            if found:
                break
        return q.rank(query, list(found.values()), min_score=0.4)

    # --- katalog gezinme --------------------------------------------------

    def _one(self, href: str, name: str, body: str) -> "Series | None":
        slug = urlparse(href).path.strip("/")
        name = html.unescape(name or "").strip()
        if not slug or not name:
            return None
        y = _YEAR_RE.search(body)
        r = _IMDB_RE.search(body)
        poster = _poster(body, self.known_domain)
        return Series(
            name=name, slug=slug,
            # Dizi sayfalari /dizi/<slug> altinda; tur bundan kesin belli.
            type="Dizi" if slug.startswith("dizi/") else "Film",
            site=self.name,
            year=y.group(1) if y else "",
            rating=r.group(1) if r else "", poster_url=poster)

    def _cards(self, html_text: str) -> list[tuple[int, Series]]:
        """Sayfadaki tum kartlar, GORUNME SIRASINA gore (konum, Series).

        Iki kart bicimi var (buyuk `poster`, kucuk `mini-poster`); ikisi de ayni
        sayfada karisik duruyor, o yuzden konuma gore birlestiriliyor — raflara
        bolerken sira onemli (bkz. home_rows).
        """
        found: list[tuple[int, Series]] = []
        for m in _POSTER_RE.finditer(html_text):
            s = self._one(m.group(1), m.group(2), m.group(3))
            if s:
                found.append((m.start(), s))
        # Attribute sırası site tarafından değişebiliyor; title/class sırası
        # değiştiğinde kartı yine de başlık ve görselle birlikte yakala.
        known = {s.slug for _, s in found}
        for m in _ANY_POSTER_RE.finditer(html_text):
            attrs, body = m.group(1), m.group(2)
            href = re.search(r'\bhref\s*=\s*["\']([^"\']+)', attrs, re.I)
            title = re.search(r'\btitle\s*=\s*["\']([^"\']*)', attrs, re.I)
            if not href or not title:
                continue
            slug = urlparse(href.group(1)).path.strip("/")
            if not slug or slug in known:
                continue
            s = self._one(href.group(1), html.unescape(title.group(1)), body)
            if s:
                known.add(slug)
                found.append((m.start(), s))
        for m in _MINI_RE.finditer(html_text):
            tm = _MINI_TITLE_RE.search(m.group(2))
            s = self._one(m.group(1), tm.group(1) if tm else "", m.group(2))
            if s:
                found.append((m.start(), s))
        found.sort(key=lambda p: p[0])
        return found

    def _posters(self, html_text: str) -> list[Series]:
        out: list[Series] = []
        seen: set[str] = set()
        for _, s in self._cards(html_text):
            if s.slug in seen:
                continue
            seen.add(s.slug)
            out.append(s)
        return out

    def home_rows(self, net: Network, session: SessionState) -> list[dict]:
        """Ana sayfa rafları: [{"title": ..., "items": [Series, ...]}, ...].

        Bölüm başlıkları ve kartlar aynı akışta duruyor; her kartı KENDİNDEN
        ÖNCEKİ en yakın başlığa bağlıyoruz. Sayfanın iç içe kutu yapısını
        ayrıştırmaktan daha dayanıklı: site sarmalayıcı div'lerini değiştirse
        bile başlık→kart sırası bozulmuyor.
        """
        page = _SVG_RE.sub("", net.get(session.base_url, referer=session.base_url).text)

        labels = []
        for m in _LABEL_RE.finditer(page):
            raw = m.group(1) if m.group(1) is not None else m.group(2)
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
            if text:
                labels.append((m.start(), html.unescape(text)))

        rows: dict[str, list[Series]] = {}
        order: list[str] = []
        for pos, item in self._cards(page):
            title = "Öne çıkanlar"
            for lpos, text in labels:
                if lpos < pos:
                    title = text
                else:
                    break
            if any(s in title.lower() for s in _SKIP_ROWS):
                continue
            if title not in rows:
                rows[title] = []
                order.append(title)
            # Ayni yapim birden cok rafta cikabiliyor; raf ICINDE tekrar olmasin.
            if any(s.slug == item.slug for s in rows[title]):
                continue
            rows[title].append(item)

        return [{"title": t, "items": rows[t]} for t in order if rows[t]]

    def _list_url(self, net: Network, session: SessionState, kind: str) -> str:
        page = net.get(session.base_url, referer=session.base_url).text
        want = "Diziler" if kind == "dizi" else "Filmler"
        for href, label in _NAV_RE.findall(page):
            if label == want:
                return href
        return session.base_url + _FALLBACK_LISTS[kind]

    def browse(self, net: Network, session: SessionState, kind: str) -> list[Series]:
        """Diziler / Filmler listeleme sayfası — arama olmadan katalog."""
        if kind not in ("dizi", "film"):
            return []
        url = self._list_url(net, session, kind)
        page = _SVG_RE.sub("", net.get(url, referer=session.base_url).text)
        return self._posters(page)

    def genres(self, net: Network, session: SessionState) -> list[dict]:
        """Tür listesi: [{"name": "Aksiyon", "url": "..."}, ...]."""
        page = net.get(session.base_url, referer=session.base_url).text
        out: list[dict] = []
        seen: set[str] = set()
        for href, name in _GENRE_RE.findall(page):
            name = html.unescape(name).strip()
            key = name.lower()
            # Site aynı türü birden çok slug'la listeleyebiliyor (ör. iki ayrı
            # "Müzik Filmleri"); ilki yeter.
            if not name or key in seen:
                continue
            seen.add(key)
            out.append({"name": name, "url": urljoin(session.base_url, href)})
        return out

    def by_genre(self, net: Network, session: SessionState, url: str) -> list[Series]:
        page = _SVG_RE.sub("", net.get(url, referer=session.base_url).text)
        return self._posters(page)

    def get_episodes(self, net: Network, session: SessionState,
                     series: Series) -> list[Episode]:
        if series.type.lower() not in _SERIES_TYPES:
            # Film: sezon/bölüm yok, sentetik tek "bölüm".
            return [Episode(season=1, number=1, url=series.url(session.base_url),
                            title=series.name)]

        page = net.get(series.url(session.base_url), referer=session.base_url).text

        # Dizi sayfası bölüm listesini schema.org JSON-LD olarak da yayınlıyor
        # (containsSeason -> TVSeason -> TVEpisode: seasonNumber/episodeNumber/url).
        # Bunu tercih ediyoruz: HTML sınıf adlarından çok daha stabil, sıralı ve
        # sezon numarasını doğrudan veriyor. JSON-LD'yi tam olarak parse etmek yerine
        # TVEpisode bloklarını tek tek yakalıyoruz — sayfada birden çok, kimi zaman
        # bozuk kaçışlı JSON-LD bloğu bulunabiliyor ve tek bir json.loads hepsini
        # birden kaybettirirdi.
        episodes: list[Episode] = []
        seen: set[str] = set()
        for m in re.finditer(
                r'"@type"\s*:\s*"TVEpisode".*?"episodeNumber"\s*:\s*"?(\d+)"?'
                r'.*?"url"\s*:\s*"([^"]+)"', page, re.S):
            url = html.unescape(m.group(2))
            sm = re.search(r"/sezon-(\d+)/", url)
            if not sm or url in seen:
                continue
            seen.add(url)
            episodes.append(Episode(season=int(sm.group(1)),
                                    number=int(m.group(1)), url=url))

        if not episodes:
            # JSON-LD yoksa/değişmişse düz linklere düş: /sezon-N/bolum-M/
            for url in re.findall(r'href="([^"]*/sezon-\d+/bolum-\d+/?)"', page):
                url = urljoin(session.base_url, html.unescape(url))
                if url in seen:
                    continue
                seen.add(url)
                sm = re.search(r"/sezon-(\d+)/bolum-(\d+)", url)
                episodes.append(Episode(season=int(sm.group(1)),
                                        number=int(sm.group(2)), url=url))

        episodes.sort(key=lambda e: (e.season, e.number))
        return episodes

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
        for a in audios:
            a.is_default = False
        if audios:
            audios[0].is_default = True  # get_av_urls Türkçe'yi zaten başa sıralar

        return MergedStream(video_master_url=m3u8_url, video_referer=embed_url,
                            audios=audios, subtitle_url=subtitle_url,
                            subtitle_referer=embed_url)


register(HDFCAdapter())
