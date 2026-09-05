"""Yardımcılar: data-rm-k çözme, TR-altyazı/reklam sezgileri, dosya adı.

TR-altyazı ve reklam sezgileri `örnek eklenti`'den (content.js / page-inject.js)
Python'a port edilmiştir.
"""
from __future__ import annotations

import base64
import html as _html
import json
import re
from urllib.parse import urlparse

from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import SHA512

from hayalet import config


# --- data-rm-k çözme (CryptoJS AES + PBKDF2-SHA512, 999 iter) -------------
def decrypt_rmk(raw_json: str, passphrase: str | None = None) -> str:
    """Bölüm sayfasındaki `data-rm-k` şifreli JSON'unu çözer -> iframe URL'si."""
    passphrase = passphrase or config.RMK_PASSPHRASE
    obj = json.loads(_html.unescape(raw_json))
    ciphertext = base64.b64decode(obj["ciphertext"])
    iv = bytes.fromhex(obj["iv"])
    salt = bytes.fromhex(obj["salt"])
    key = PBKDF2(
        passphrase, salt,
        dkLen=config.RMK_KEYSIZE,
        count=config.RMK_ITERATIONS,
        hmac_hash_module=SHA512,
    )
    padded = AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext)
    plaintext = padded[: -padded[-1]]          # PKCS7 unpad
    return plaintext.decode("utf-8").strip()


# --- Medya / reklam / TR-altyazı sezgileri (eklentiden) ------------------
def is_ad_url(url: str) -> bool:
    low = (url or "").lower()
    return any(d in low for d in config.AD_INTERCEPT_DOMAINS)


def is_media_url(url: str) -> str | None:
    """'VIDEO' | 'SUB' | None (örnek eklenti isMediaUrl port'u)."""
    low = (url or "").lower()
    if not low:
        return None
    if ".m3u8" in low or ".mp4" in low:
        return "VIDEO"
    if any(x in low for x in (".vtt", ".srt", ".ass")):
        return "SUB"
    if any(x in low for x in ("/subtitle", "/caption", "/sub/", "/subs/",
                              "/tracks/", "webvtt", "text/vtt")):
        return "SUB"
    return None


def is_turkish_sub(url: str) -> bool:
    """Altyazı URL'si Türkçe mi? (örnek eklenti isTurkishSubtitleUrl port'u)."""
    low = (url or "").lower()
    if not low:
        return False
    if "forced" in low or "zorunlu" in low:
        return False
    if "turkish" in low or "turkce" in low or "türkçe" in low:
        return True
    try:
        parsed = urlparse(url)
        if re.search(r"(^|[/_.\-])(tr|tur)([/_.\-]|$)", parsed.path, re.I):
            return True
        from urllib.parse import parse_qsl
        for k, v in parse_qsl(parsed.query):
            if any(t in k.lower() for t in ("lang", "sub", "file")) and \
               re.search(r"(^|[-_])(tr|tur)([-_]|$)", v, re.I):
                return True
    except Exception:
        pass
    return False


def looks_turkish(text: str) -> bool:
    t = (text or "").lower()
    return ("turkce" in t or "türkçe" in t or "turkish" in t
            or t.strip() == "tr" or " tr " in t)


# --- Dosya adı ------------------------------------------------------------
def safe_filename(name: str) -> str:
    """Geçersiz dosya adı karakterlerini temizle (eski server.js mantığı)."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "")
    name = re.sub(r"\s+", " ", name).strip()
    return name or "video"


# --- Poster yardımcıları --------------------------------------------------
def normalize_poster_url(url: str, base_url: str = "") -> str:
    """Poster URL'sini temizler, /artist/ 404 adreslerini eler, protokol ve base_url tamamlar."""
    if not url:
        return ""
    raw = str(url).replace(r"\/", "/").strip().strip("\"'")
    if not raw or raw.startswith("data:"):
        return ""
    if "/artist/" in raw.lower():
        return ""
    if raw.startswith("//"):
        return "https:" + raw
    if raw.startswith("/"):
        if base_url:
            from urllib.parse import urljoin
            return urljoin(base_url, raw)
        return raw
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return ""


def poster_url_from_html(fragment: str, base_url: str = "") -> str:
    """HTML parçasından geçerli poster resim URL'sini çıkarır."""
    if not fragment:
        return ""
    patterns = [
        r'data-(?:src|lazy-src|original)\s*=\s*["\']([^"\']+)["\']',
        r'data-background\s*=\s*["\']([^"\']+)["\']',
        r'background(?:-image)?\s*:\s*url\(["\']?([^"\'\)]+)["\']?\)',
        r'src\s*=\s*["\']([^"\']+)["\']',
    ]
    ignored = (".svg", ".gif", "logo", "avatar", "blank", "placeholder", "icon", "/artist/", "data:image")
    for pat in patterns:
        for m in re.finditer(pat, fragment, re.I):
            val = m.group(1).replace(r"\/", "/").strip()
            low = val.lower()
            if any(bad in low for bad in ignored):
                continue
            norm = normalize_poster_url(val, base_url)
            if norm:
                return norm
    return ""
