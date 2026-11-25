"""NP oynatıcısının (pichive / dplayer / NP_CONFIG ailesi) "gate" iş kanıtı.

2026-11'de oynatıcı (np.js) şunu eklemiş: NP_CONFIG.gate = {n, b} varsa bir
sayaç bulunur ve "&p=<sayaç>" HER kaynağın master'ına ve HER altyazı url'sine
eklenir; eklenmezse sunucu boş 404 döner (ölçüldü: nm.php ve nsub.php, hangi
Referer/Origin verilirse verilsin 404 — gate'li adres 200).

Varsayılan kural (np.js): SHA-256(n + ":" + i) ilk 32-bit kelimesinin üst
`b` biti sıfır olan ilk i. Ama sayfa çoğu zaman kendi `window.NP_GATE`
fonksiyonunu tanımlıyor ve kuralı her yüklemede değiştiriyor: araya gizli bir
tuz (String.fromCharCode(...)) giriyor, bakılan kelime ([2] gibi) ve kaydırma
(>>>20 gibi) değişiyor. O fonksiyonun parçaları kaynaktan OKUNUR; JS
çalıştırılmaz.
"""
from __future__ import annotations

import hashlib
import re

_MAX_TRIES = 20_000_000  # 12 bit ≈ 4096 deneme; bu tavan yalnız sonsuz döngü sigortası


class GateError(Exception):
    pass


def _words(s: str) -> tuple[int, ...]:
    # np.js'in ee(): charCodeAt & 255 → latin-1 bayt, standart SHA-256, big-endian kelimeler.
    d = hashlib.sha256(bytes(ord(c) & 255 for c in s)).digest()
    return tuple(int.from_bytes(d[i:i + 4], "big") for i in range(0, 32, 4))


def _to_radix(i: int, radix: int) -> str:
    if radix == 10:
        return str(i)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        i, r = divmod(i, radix)
        out = digits[r] + out
        if not i:
            return out


def _js_string_values(src: str) -> dict[str, str]:
    """`var X=String.fromCharCode(..)` ve `var X="..."` atamalarını toplar."""
    vals: dict[str, str] = {}
    for m in re.finditer(r"(\w+)\s*=\s*String\.fromCharCode\(([\d,\s]+)\)", src):
        vals[m.group(1)] = "".join(chr(int(x)) for x in m.group(2).split(","))
    for m in re.finditer(r"(\w+)\s*=\s*(['\"])((?:(?!\2).)*)\2", src):
        vals.setdefault(m.group(1), m.group(3))
    return vals


def _parse_custom(html: str):
    """window.NP_GATE gövdesinden (parçalar, kelime_idx, (işlem, değer), taban) çıkarır.

    parçalar: "N" (gate.n), "I" (sayaç) ya da sabit dizgilerden oluşan liste.
    """
    m = re.search(r"window\.NP_GATE\s*=\s*function\s*\((\w+)\s*,\s*(\w+)\)\s*\{(.*?)\};?\s*(?:window\.|</script>)",
                  html, re.S)
    if not m:
        return None
    p_n, p_hash, body = m.groups()
    # Koşul iki biçimde görüldü: üst bitler `(h(..)[k] >>> s) === 0` ve
    # (2026-11-25, selcukflix/pichive) alt bitler `(h(..)[k] & m) === 0`.
    call = re.search(re.escape(p_hash) + r"\(([^()]*)\)\s*\[(\d+)\]\s*(>>>|&)\s*(\d+)\s*\)\s*===?\s*0", body)
    if not call:
        raise GateError("NP_GATE koşulu tanınmadı.")
    expr, word_idx = call.group(1), int(call.group(2))
    test = (call.group(3), int(call.group(4)))
    counter = re.search(r"(\w+)\s*=\s*\w+\.toString\((\d+)\)", body)
    counter_var, radix = (counter.group(1), int(counter.group(2))) if counter else (None, 10)
    strings = _js_string_values(body)

    parts: list[str] = []
    for tok in (t.strip() for t in expr.split("+")):
        if tok == p_n:
            parts.append("N")
        elif counter_var and tok == counter_var:
            parts.append("I")
        elif len(tok) >= 2 and tok[0] in "'\"" and tok[-1] == tok[0]:
            parts.append("=" + tok[1:-1])
        elif tok in strings:
            parts.append("=" + strings[tok])
        else:
            raise GateError(f"NP_GATE ifadesi tanınmadı: {tok[:30]}")
    if "I" not in parts:
        raise GateError("NP_GATE sayaç parçası bulunamadı.")
    return parts, word_idx, test, radix


def solve(html: str, gate: dict) -> str:
    """Sayfanın gate'ini çözer; eklenecek sayaç dizgisini döndürür."""
    n = str(gate.get("n") or "")
    custom = _parse_custom(html)
    if custom:
        parts, word_idx, (op, val), radix = custom
    else:
        parts, word_idx, (op, val), radix = ["N", "=:", "I"], 0, (">>>", 32 - int(gate.get("b") or 12)), 10
    pre = []
    for p in parts:
        if p == "I":
            break
        pre.append(n if p == "N" else p[1:])
    post = []
    for p in parts[parts.index("I") + 1:]:
        post.append(n if p == "N" else p[1:])
    pre_s, post_s = "".join(pre), "".join(post)
    for i in range(_MAX_TRIES):
        c = _to_radix(i, radix)
        w = _words(pre_s + c + post_s)[word_idx]
        if (w >> val if op == ">>>" else w & val) == 0:
            return c
    raise GateError("gate çözülemedi (deneme sınırı aşıldı).")


def apply(html: str, cfg: dict) -> dict:
    """cfg (NP_CONFIG) gate'liyse master ve altyazı url'lerine &p= ekler (yerinde)."""
    gate = cfg.get("gate")
    if not (isinstance(gate, dict) and gate.get("n")):
        return cfg
    suffix = "&p=" + solve(html, gate)
    for s in cfg.get("sources") or []:
        if s.get("master"):
            s["master"] += suffix
    for s in cfg.get("subtitles") or []:
        if s.get("url"):
            s["url"] += suffix
        s["_gate"] = suffix   # url'yi tok'tan kuran çağıranlar için
    return cfg
