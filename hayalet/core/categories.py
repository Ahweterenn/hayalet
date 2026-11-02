"""Tür ('Aksiyon', 'Komedi'...) kategorisi rafları — saf/ağsız kovalama.

IMDb'den gelen, türleriyle birlikte sitelerimize EŞLEŞMİŞ yapımları türe göre
raflara ayırır. IMDb çekimi ve site-eşleştirmesi çağırana aittir
(bkz. hayalet_app._refresh_imdb_rows); burada yalnız saf gruplama var → test
edilebilir, ağ yok. Tür adları Türkçe'dir (imdb.GENRES_TR çıktısı).
"""
from __future__ import annotations

# Rafların görünüm sırası (öne çıkan türler üstte). Listede olmayan türler,
# yeterli yapım varsa, sona eklenir.
GENRE_ORDER = [
    "Aksiyon", "Komedi", "Dram", "Bilim kurgu", "Korku", "Gerilim", "Suç",
    "Macera", "Fantastik", "Romantik", "Gizem", "Animasyon", "Belgesel",
    "Aile", "Tarih", "Savaş", "Biyografi", "Müzik", "Western",
]
_MIN_ITEMS = 4      # bu kadar yapım bulunmayan tür rafı gösterilmez (seyrek görünmesin)
_MAX_ITEMS = 20


def build_genre_rows(pairs, *, order=None, min_items=_MIN_ITEMS,
                     max_items=_MAX_ITEMS, key=lambda s: s):
    """pairs: [(genres: list[str], item)]. Dönüş: [(tür_adı, [item, ...])] sıralı.

    - Her yapımı taşıdığı TÜM türlerin kovasına ekler (bir film hem Aksiyon hem
      Gerilim rafında görünebilir — IMDb'de zaten öyle).
    - `key(item)` ile aynı tür içinde yinelenmeyi eler (aynı site+slug iki kez
      gelmesin).
    - `min_items` altındaki türleri atar, `order` sırasında döndürür.
    """
    buckets: dict[str, list] = {}
    seen: dict[str, set] = {}
    for genres, item in pairs:
        if item is None:
            continue
        for g in genres or []:
            b = buckets.setdefault(g, [])
            s = seen.setdefault(g, set())
            k = key(item)
            if k in s:
                continue
            s.add(k)
            b.append(item)
    names = order or GENRE_ORDER
    extra = [g for g in buckets if g not in names]
    rows = []
    for g in list(names) + extra:
        items = buckets.get(g) or []
        if len(items) >= min_items:
            rows.append((g, items[:max_items]))
    return rows
