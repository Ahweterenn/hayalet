"""video-cli yapılandırma sabitleri.

Tek merkez: domain, anti-bot ayarları, dizinler, HTML seçiciler ve reklam kara listesi.
Site değişince (seçiciler / domain) buradan güncellenir.
"""
from pathlib import Path

# --- Domain ---------------------------------------------------------------
# Son bilinen domain; resolver bunu doğrular/override eder.
KNOWN_DOMAIN = "https://dizipal1560.com"

# Güncel domaini veren yönlendirme kaynakları (resolver adımında doldurulacak).
RESOLVER_SOURCES: list[str] = []

# --- Ağ / anti-bot --------------------------------------------------------
# curl-cffi TLS/JA3 taklidi profili.
IMPERSONATE = "chrome"

# curl-cffi, mpv ve yt-dlp'nin AYNI User-Agent'ı kullanması kritik.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 20
MAX_RETRIES = 3

# İzleme oynatıcısı: "auto" (PotPlayer > VLC > mpv) | "potplayer" | "vlc" | "mpv"
PLAYER = "auto"

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# --- Dizinler -------------------------------------------------------------
PACKAGE_DIR = Path(__file__).resolve().parent      # .../hayalet/hayalet
PROJECT_ROOT = PACKAGE_DIR.parent                  # .../hayalet
DOWNLOAD_DIR = PROJECT_ROOT / "downloads"
CACHE_DIR = PROJECT_ROOT / ".cache"

# --- Site endpoint'leri (canlı incelemeyle doğrulandı) --------------------
SEARCH_ENDPOINT = "/bg/searchcontent"   # POST: searchterm + cValue -> JSON

# --- Player şifre çözme (data-rm-k) ---------------------------------------
# Bölüm sayfasındaki oynatıcı kaynağı CryptoJS AES + PBKDF2-SHA512 ile
# şifrelenir. Parametreler app-dizipals.js'ten çıkarıldı. Site bundle'ı
# yenilenirse (passphrase rotasyonu) buradan güncellenir.
RMK_PASSPHRASE = (
    "3hPn4uCjTVtfYWcjIcoJQ4cL1WWk1qxXI39egLYOmNv6IblA7eKJz68uU3eLzux1biZ"
    "LCms0quEjTYniGv5z1JcKbNIsDQFSeIZOBZJz4is6pD7UyWDggWWzTLBQbHcQFpBQdC"
    "lnuQaMNUHtLHTpzCvZy33p6I7wFBvL4fnXBYH84aUIyWGTRvM2G5cfoNf4705tO2kv"
)
RMK_ITERATIONS = 999
RMK_KEYSIZE = 32   # 256-bit
# hasher = SHA512 (utils içinde sabit)

# --- HTML seçiciler / kalıplar (canlı incelemede doğrulandı) --------------
SELECTORS = {
    "rmk_block": r'data-rm-k="true">(\{.*?\})</div>',   # şifreli kaynak JSON
    "bolum_link": r'/bolum/[a-z0-9\-]+',                 # bölüm URL kalıbı
    "sxe_from_slug": r'-(\d+)x(\d+)(?:-c\d+)?$',         # slug -> (sezon, bölüm)
}

# --- Reklam kara listesi (örnek eklentiden birebir) -----------------------
AD_INTERCEPT_DOMAINS = [
    "grandpashabet", "betasus", "roketbet", "romabet", "bahibom", "dedebet",
    "betcio", "betnano", "betmatik", "casibom", "cratosslot", "pusulabet",
    "doubleclick", "googlesyndication", "adservice", "adnxs", "exoclick",
    "propellerads", "popcash", "popads", "adsterra", "trafficjunky",
    "shortsteven", "shorteven", "outbrain", "taboola",
]
