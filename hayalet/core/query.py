"""Site-bağımsız arama sorgusu normalleştirme, varyant üretimi ve puanlama.

Neden gerek var: sitelerin kendi arama backend'i **harfi harfine** eşleşme
arıyor. Canlı gözlem (hdfilmcehennemi): `spider-man` sonuç veriyor ama
`spiderman` HİÇBİR şey döndürmüyor — tire, Türkçe karakter, boşluk gibi tek bir
fark tüm sonucu siliyor. Bunu sitede düzeltemeyiz (index bizim değil), o yüzden
istemci tarafında iki şey yapılır:

  1. **Varyant üretimi** (`variants`) — asıl sorgu boş dönerse (ya da hiçbir
     sonuç yeterince benzemiyorsa) bir avuç makul yazım denenir. "Dozunda"
     olması önemli: en fazla `MAX_VARIANTS` ek istek, hepsi paralel.
  2. **Puanlama** (`score`) — geniş ağ atınca gelen alakasız sonuçlar dışarıda
     kalsın diye her sonuç sorguya benzerliğine göre sıralanır ve eşiğin altı
     atılır. Böylece "geniş arama" listeyi çöple doldurmuyor.

Saf fonksiyonlar — ağ yok, site bilgisi yok; tests/ altından doğrudan test edilir.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

# Türkçe harfler için özel katlama: unicodedata NFKD 'ı' ve 'ğ' için beklendiği
# gibi çalışmıyor ve 'I'.lower() Türkçe'de 'ı' olmalı. Karşılaştırma amaçlı
# olduğu için hepsini ASCII karşılığına indiriyoruz.
_FOLD = str.maketrans({
    "ı": "i", "İ": "i", "I": "i", "i": "i",
    "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ü": "u", "Ü": "u", "ö": "o", "Ö": "o",
    "ç": "c", "Ç": "c", "â": "a", "Â": "a",
    "î": "i", "Î": "i", "û": "u", "Û": "u",
})

_PUNCT_RE = re.compile(r"[^0-9a-z]+")
_WS_RE = re.compile(r"\s+")

# Başlıklarda anlam taşımayan, aramayı daraltmaktan başka işe yaramayan ekler.
_NOISE = {"turkce", "dublaj", "altyazili", "izle", "full", "hd", "1080p", "720p"}
# Dilbilgisi kelimeleri: eşleşmeye katkısı yok, kapsama hesabını sulandırır
# ("game of thrones"te ayırt edici olan "thrones", "of" değil).
_STOP = {"the", "of", "a", "an", "and", "bir", "ve", "ile", "da", "de"}


def fold(text: str) -> str:
    """Türkçe/aksanlı harfleri ASCII'ye indirir, küçük harfe çevirir."""
    text = text.translate(_FOLD).lower()
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def normalize(text: str) -> str:
    """Karşılaştırma biçimi: katlanmış, noktalamasız, tek boşluklu.

    'Spider-Man: No Way Home' -> 'spider man no way home'
    """
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", fold(text))).strip()


def squash(text: str) -> str:
    """Boşluk/tire farkını tamamen yok sayan biçim: 'spider-man' -> 'spiderman'."""
    return normalize(text).replace(" ", "")


def tokens(text: str) -> list[str]:
    return [t for t in normalize(text).split() if t]


def _meaningful(text: str) -> list[str]:
    return [t for t in tokens(text) if t not in _NOISE and t not in _STOP]


def _word_hit(word: str, title_words: set[str]) -> bool:
    """Kelime başlıkta geçiyor mu — Türkçe ekleri affederek.

    'yuzukler' ile 'yuzuklerin' aynı kelimedir; birinin diğeriyle başlaması
    yeterli sayılır — ama yalnızca uzunlukları yakınsa. Bu sınır olmadan
    'break', 'breaking'in eki sayılıp "breaking bad" aramasına 'Break',
    'Break In' gibi alakasız filmler karışıyordu.
    """
    if word in title_words:
        return True
    if len(word) < 4:
        return False
    return any(len(t) >= 4 and (t.startswith(word) or word.startswith(t))
               and min(len(t), len(word)) / max(len(t), len(word)) >= 0.75
               for t in title_words)


def coverage(query: str, title: str) -> float:
    """Sorgunun kaçta kaçı (harf ağırlıklı) başlıkta karşılık buluyor.

    Tek kelimelik sorgularda anlamsız (hep 1.0) — çok kelimeli sorgularda
    "sadece bir kelimesi tutan" sonuçları ayıklamak için var: 'orumcek adam'
    araması 'Adam' filmini getirmemeli.
    """
    q_words = _meaningful(query)
    if len(q_words) < 2:
        return 1.0
    t_words = set(_meaningful(title))
    total = sum(len(w) for w in q_words)
    if not total:
        return 1.0
    return sum(len(w) for w in q_words if _word_hit(w, t_words)) / total


def score(query: str, title: str) -> float:
    """0..1 arası benzerlik. Boşluk/tire/Türkçe karakter farkını cezalandırmaz.

    'spiderman' ile 'Spider-Man' 1.0 verir; 'spiderman' ile 'Spider-Man'in
    devam filmi 'Spider-Man: No Way Home' yüksek ama 1.0'ın altında kalır —
    böylece birebir eşleşme listenin başına çıkar.
    """
    sq, st = squash(query), squash(title)
    if not sq or not st:
        return 0.0
    if sq == st:
        return 1.0

    ratio = difflib.SequenceMatcher(None, sq, st).ratio()

    # Sorgu, başlığın içinde bir bütün olarak geçiyorsa (alt seri / devam filmi)
    # difflib uzunluk farkı yüzünden haksızca düşük puan veriyor; tabanı yükselt.
    if sq in st:
        ratio = max(ratio, 0.95 - 0.15 * (len(st) - len(sq)) / len(st))
    elif st in sq:
        ratio = max(ratio, 0.85)

    # Kelime bazlı örtüşme: 'yuzuklerin efendisi kralin donusu' gibi uzun
    # başlıklarda karakter benzerliği düşerken kelimeler tam tutuyor olabilir.
    q_words, t_words = set(_meaningful(query)), set(_meaningful(title))
    if q_words and q_words <= t_words:
        ratio = max(ratio, 0.9)
    elif q_words and t_words:
        overlap = len(q_words & t_words) / len(q_words)
        ratio = max(ratio, 0.55 * overlap + 0.35 * ratio)

    return min(ratio, 1.0)


# --- Varyant üretimi -------------------------------------------------------
MAX_VARIANTS = 3

# Bileşik başlıklarda sık geçen ikinci yarılar. 'spiderman' -> 'spider man'
# bölünmesini tahmin etmek için genel bir kural yok; ama bu ekler tek başına
# "ön ek + ek" kalıbını yakalamaya yetiyor ve yanlış bölme riski düşük
# (yanlış varyant zaten 0 sonuç döndürür, sadece bir istek harcar).
_SPLIT_TAILS = ("man", "men", "woman", "girl", "boy", "world", "war", "land",
                "star", "wars", "day", "night", "life", "house", "town")

_MIN_PREFIX = 5
_MIN_WORD = 4


def _split_compound(word: str) -> str | None:
    """'spiderman' -> 'spider man' (tanınan bir son ek varsa)."""
    for tail in _SPLIT_TAILS:
        head = word[:-len(tail)]
        if word.endswith(tail) and len(head) >= 4:
            return f"{head} {tail}"
    return None


def variants(query: str, limit: int = MAX_VARIANTS) -> list[str]:
    """Asıl sorgu boş dönerse denenecek alternatif yazımlar (öncelik sırasıyla).

    Asıl sorgunun kendisi listede YOKTUR — çağıran onu zaten aramıştır.
    """
    out: list[str] = []
    # Tekrarı **birebir metin** üzerinden eleriz, squash üzerinden DEĞİL: bizim
    # için "spiderman" ile "spider man" aynı şey ama sitenin araması için
    # tamamen farklı iki sorgu — varyantın bütün amacı zaten bu fark.
    seen = {query.strip().lower()}

    def add(cand: str) -> None:
        cand = cand.strip()
        key = cand.lower()
        if cand and key not in seen:
            seen.add(key)
            out.append(cand)

    norm = normalize(query)
    words = norm.split()

    # 1) Noktalama/Türkçe karakter katlanmış hâli ("Spider-Man" -> "spider man").
    add(norm)

    # 2) Bileşik kelimeyi ayır ("spiderman" -> "spider man"); sitenin tireli
    #    kaydını da yakalar çünkü tire arama motorlarında çoğunlukla boşluk sayılır.
    if len(words) == 1:
        split = _split_compound(words[0])
        if split:
            add(split)

    # 3) Anlamsız ekleri at ("shrek turkce dublaj izle" -> "shrek").
    meaningful = _meaningful(query)
    if meaningful and len(meaningful) < len(words):
        add(" ".join(meaningful))

    # 4) Geniş ağ. Site araması "içinde geçen" mantığıyla çalıştığı için tek bir
    #    kelime bile doğru yapımı getirir; fazlalık sonuçları score() eşiği eler.
    #    Çok kelimelide önce kelimeleri tek tek denemek gerekiyor: sitede kayıtlı
    #    başlık "Yüzüklerin Efendisi" iken kullanıcının yazdığı ASCII cümle hiç
    #    tutmaz ama içindeki aksansız kelime ("efendisi") tutar.
    pool = meaningful or words
    if len(pool) > 1:
        longest = max(pool, key=len)
        # Kısa kelimeyi tek başına aramak anlamsız: "man" yüzlerce alakasız
        # başlık getirir, hepsi de eşiğin altında kalıp atılır — boşa istek.
        for cand in (longest, pool[-1]):
            if len(cand) >= _MIN_WORD:
                add(cand)
    base = max(pool, key=len, default="")
    if len(base) > _MIN_PREFIX + 1:
        add(base[:max(_MIN_PREFIX, len(base) - 3)])

    return out[:limit]


# --- Sonuç sıralama --------------------------------------------------------
# Eşik: bunun altındaki sonuçlar "geniş ağ"dan gelen gürültü sayılıp atılır.
# 0.45, "orumcek" -> "Örümcek Adam" gibi kısmi eşleşmeleri tutup alakasız
# başlıkları eleyecek şekilde seçildi.
MIN_SCORE = 0.45
# Asıl arama bu kadar iyi bir sonuç verdiyse varyantlara hiç gerek yok.
GOOD_SCORE = 0.72
# Çok kelimeli sorgularda başlığın karşılaması gereken en az kapsama. Geniş ağ
# atılan varyantlardan ("adam", "break") gelen tek-kelime eşleşmelerini eler.
MIN_COVERAGE = 0.6


def rank(query: str, results: list, min_score: float = MIN_SCORE,
         key=lambda r: r.name) -> list:
    """Sonuçları sorguya benzerliğe göre sıralar; eşiğin altını atar.

    İki süzgeç var: benzerlik puanı (`score`) ve çok kelimeli sorgularda kapsama
    (`coverage`) — ikincisi olmadan "orumcek adam" araması varyantlar sayesinde
    doğru filmi buluyor ama yanına 'Adam', 'Black Adam' gibi tek kelimesi tutan
    sonuçları da alıyordu.

    Hiçbiri eşiği geçemezse liste boşaltılmaz — sıralanmış hâli döner
    (kullanıcıya "hiç sonuç yok" demektense zayıf eşleşmeleri göstermek yeğdir;
    yalnızca zaten sonuç varken gürültü ayıklanır).
    """
    scored = sorted(((score(query, key(r)), r) for r in results),
                    key=lambda p: p[0], reverse=True)
    kept = [r for s, r in scored
            if s >= min_score and coverage(query, key(r)) >= MIN_COVERAGE]
    return kept or [r for _, r in scored]


def best_score(query: str, results: list, key=lambda r: r.name) -> float:
    return max((score(query, key(r)) for r in results), default=0.0)
