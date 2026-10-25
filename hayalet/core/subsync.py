"""Altyazı-ses hizalama: altyazının sesle kaymasını ÖLÇER, gerekiyorsa düzeltir.

Elle müdahalesiz çalışır. Yöntem canlı veride doğrulandı (The Last of Us S1B1,
Dizipal, ilk 10 dk): altyazı bilerek 0,5 / 1,5 / −2 / 4 / −7,5 / 15 sn
kaydırıldığında altısında da doğru kayma geri bulundu, hata hep 0,1 sn (tek
kova, yani yuvarlama). Aynı bölümde gerçek kayma ölçüldü: yok (tepe 0,0 sn).

Nasıl çalışıyor:

1. **Konuşmanın NEREDE olduğu** yeter, ne dendiği gerekmez. ffmpeg'in
   `silencedetect` filtresi sessizlik başlangıç/bitişlerini yazdırır; tersi
   konuşma aralıklarıdır. Yeni bağımlılık yok — ffmpeg zaten indirme için
   gerekli.
2. Konuşma ve altyazı zaman çizgileri `BIN_MS`'lik kovalara indirilip birer
   **bit maskesine** çevrilir; kaydırma `maske << k` olur, örtüşme
   `(a & b).bit_count()`. ±30 sn'lik tarama milisaniyeler sürer, numpy/FFT
   gerekmez.
3. **Puan asimetriktir**: "altyazı varken ses var mı" diye bakılır, tersi
   değil. Kornanın, müziğin, patlamanın ürettiği FAZLA ses cezalandırılmaz —
   çünkü onların altyazısı yoktur. Cezalandırılan tek şey altyazı varken
   ortalığın sessiz olmasıdır. Simetrik bir puan, konuşmasız gürültüyü
   altyazıyla eşleştirmeye çalışıp tepeyi kaydırırdı.
4. **Güven kapısı**: tepe puanı ve tepenin rakibinden farkı eşiği geçmezse
   HİÇBİR ŞEY YAPILMAZ. Otomatik yanlış kaydırma, kaymış altyazıdan beterdir.

Sesi diyaloga daraltmak için bant geçiren filtre (300–3000 Hz) kullanılır;
5.1 kaynaklarda konuşma merkez kanaldadır ama HLS sesleri çoğunlukla stereo
geldiği için `pan` ile merkez ayıklamaya güvenilmiyor.

Modül SAFTIR: ağ yok, dosya yok. Girdi olarak ffmpeg'in stderr metnini ve
altyazı metnini alır — bu yüzden `tests/test_subsync.py` ile ağsız test edilir.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# --- Ayarlar (hepsi ölçümden geldi, bkz. modül başlığı) -------------------
BIN_MS = 100          # kova genişliği = çözünürlük; 0,1 sn fark edilmez
MAX_LAG_S = 30        # ±bu kadar saniye taranır
WINDOW_S = 600        # ölçüm için kullanılan ilk N saniye
MIN_CUE_BINS = 50     # bu kadar altyazı kovası yoksa ölçüm anlamsız

# --- Güven eşikleri ------------------------------------------------------
# Hepsi ÖLÇÜLDÜ (Rick and Morty S1B1, yerel dosya; doğru/yanlış cevaplar ayrı
# ayrı). "Tepe eksi en iyi rakip" tek başına YETMİYOR: ses %83 dolu olduğunda
# her kaymada puan yükseliyor, tepe de rakip de tavana yapışıyor ve mutlak fark
# anlamını yitiriyor — pencereyi büyütünce fark 0,031'den 0,016'ya DÜŞÜYORDU,
# yani doğru cevaplar reddediliyordu. Asıl ölçüt tepenin eğrinin GENELİNE göre
# ne kadar sivrildiği:  z = (tepe − medyan) / MAD.
#
# Eşikler, üretim ayarında (−25 dB) OLUMSUZ KONTROLLE belirlendi: gerçek
# altyazının cue sayısı ve süreleri korunup yerleri bozularak (karıştırma, ters
# çevirme, aralık bozma) "doğru cevabı olmayan" altyazılar üretildi. Gate
# bunların hepsini reddetmeli:
#
#                         rakip farkı(>5sn)      z
#   doğru cevaplar          0,031 – 0,042     5,7 – 13,9
#   karşılığı olmayanlar   0,0003 – 0,0074    2,1 –  3,5
#
# Mutlak puan AYIRMIYOR: olumsuz örnekler de 0,717–0,730 alıyor (doğru 0,742).
# Doyuma ulaşmış seste her kayma yüksek puan verir; ayıran tek şey tepenin
# sivriliği. Bu yüzden MIN_SCORE yalnızca akıl sağlığı tabanı.
# Eşik −25 dB seçildi: her ölçümde −30 dB'den daha belirgin tepe veriyor.
MIN_SCORE = 0.45      # akıl sağlığı tabanı (ayırt edici DEĞİL, yukarıya bak)
MIN_Z = 4.5           # tepe, eğrinin genelinden bu kadar sivrilmeli
MIN_MARGIN = 0.015    # tepe ile UZAKTAKİ en iyi rakip arasındaki en küçük fark
RIVAL_GUARD_S = 5.0   # "rakip" sayılmak için tepeden en az bu kadar uzak;
                      # cue'lar 2-3 sn sürdüğü için 1 sn ötesi hâlâ aynı tepenin
                      # eteğiydi ve kendi tepesini rakip sanıyordu
MIN_SHIFT_S = 0.35    # bundan küçük kaymaya dokunma (kova gürültüsü)
MIN_FILL = 0.15       # ses çizgisi bundan seyrekse desen yok (ölçüldü: %9,6 → yanlış)
MAX_FILL = 0.95       # bundan doluysa her şey "ses var", ayrım kalmaz

AUDIO_FILTER = "highpass=f=200,lowpass=f=3000,silencedetect=noise=-25dB:d=0.25"

# --- Kare hızı uyuşmazlığı (zamanla büyüyen kayma) ------------------------
# Altyazı başka kare hızındaki bir sürüme göre zamanlanmışsa kayma sabit değil,
# büyür: 25 ↔ 23,976'da dakikada ~2,6 sn. Serbest bir eğim aranmaz — fiziksel
# olarak mümkün oranlar birkaç tane ve her ek deneme yanlış tepe bulma şansını
# artırır. Altyazı zamanı oranla çarpılır, sonra olağan sabit kayma araması.
RATES = (25 / 23.976, 23.976 / 25, 24 / 23.976, 23.976 / 24, 25 / 24, 24 / 25)
# Eşikler ÖLÇÜLDÜ (Rick and Morty S1B1 gerçek sesi):
#                            z (oranlı)    tepe farkı
#   bilerek gerilmiş           8,6 – 11,1   0,036 – 0,042
#   sahte (20 karıştırma)      ≤ 5,5        ≤ 0,019
# Ve asıl tehlike: DOĞRU altyazılarda (6 yapım) başka bir oran 1,0'ı en fazla
# +0,6 z geçti — The Last of Us'ta 1,001 oranı z=7,5 / fark 0,075 ile ilk iki
# eşiği geçti, sabit kaymaya göre kazancı yalnız +0,1'di. Kazanç şartı
# olmasa doğru altyazı zamanla bozulurdu. Gerilmişlerde kazanç +0,9 ile +8,8.
RATE_MIN_Z = 7.0
RATE_MIN_MARGIN = 0.025
RATE_MIN_GAIN = 1.0   # oranlı aday sabit kaymanın z'sini en az bu kadar geçmeli

_SILENCE_RE = re.compile(r"silence_(start|end):\s*(-?[\d.]+)")
# Saat hanesi opsiyonel: altyazılar MM:SS.mmm biçiminde de geliyor (ölçüldü).
_TS_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_CUE_RE = re.compile(
    r"((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*(-->)\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})")
# Konuşma olmayan satırlar: [kapı çalar], (müzik), ♪ ... ♪
_NONSPEECH_RE = re.compile(r"^\s*[\[(♪#].*[\])♪#]\s*$")


@dataclass
class SyncResult:
    """Ölçümün sonucu. `apply` False ise altyazıya DOKUNULMAZ."""
    offset: float      # altyazıya eklenecek saniye (+ ileri, − geri)
    score: float       # tepe örtüşme oranı
    rival: float       # tepeden uzaktaki en iyi rakip
    apply: bool
    reason: str        # neden uygulandı/uygulanmadı (log ve --doctor için)
    z: float = 0.0     # tepenin eğrinin genelinden sivrilmesi (asıl ölçüt)
    fill: float = 0.0  # ses çizgisinin doluluk oranı
    rate: float = 1.0  # altyazı zamanı bu oranla çarpılır (kare hızı uyuşmazlığı)

    @property
    def margin(self) -> float:
        return self.score - self.rival


# --- Zaman çizgileri ------------------------------------------------------
def _secs(h, m, s, ms) -> float:
    """(saat?, dakika, saniye, salise) -> saniye. Saat yoksa 0."""
    ms = (ms or "0").ljust(3, "0")[:3]
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def speech_spans(ff_stderr: str, window: float) -> list[tuple[float, float]]:
    """silencedetect çıktısından KONUŞMA (ses var) aralıkları.

    Filtre sessizliği bildirir; konuşma onun tersidir. Kapanmamış son
    sessizlik pencerenin sonuna kadar sürüyor sayılır.
    """
    silences: list[tuple[float, float]] = []
    start: float | None = None
    for kind, val in _SILENCE_RE.findall(ff_stderr):
        if kind == "start":
            start = float(val)
        elif start is not None:
            silences.append((start, float(val)))
            start = None
    if start is not None:
        silences.append((start, window))

    spans: list[tuple[float, float]] = []
    prev = 0.0
    for a, b in silences:
        if a > prev:
            spans.append((prev, min(a, window)))
        prev = max(prev, b)
        if prev >= window:
            break
    if prev < window:
        spans.append((prev, window))
    return [(a, b) for a, b in spans if b > a]


def cue_spans(vtt: str, window: float) -> list[tuple[float, float]]:
    """Altyazı cue aralıkları. Konuşma olmayan satırlar ([kapı çalar]) elenir."""
    spans: list[tuple[float, float]] = []
    for block in re.split(r"\n\s*\n", vtt):
        m = _CUE_RE.search(block)
        if not m:
            continue
        text = block[m.end():].strip()
        lines = [l for l in text.splitlines() if l.strip()]
        if not lines or all(_NONSPEECH_RE.match(l) for l in lines):
            continue
        a = _secs(*_TS_RE.match(m.group(1)).groups())
        b = _secs(*_TS_RE.match(m.group(3)).groups())
        if b > a and a < window:
            spans.append((a, min(b, window)))
    return spans


def mask(spans: list[tuple[float, float]], window: float) -> int:
    """Aralıkları bit maskesine çevirir (bit i = i. kovada olay var)."""
    nbins = int(window * 1000 // BIN_MS)
    out = 0
    for a, b in spans:
        i = max(0, int(a * 1000 // BIN_MS))
        j = min(nbins, int(b * 1000 // BIN_MS))
        if j > i:
            out |= ((1 << (j - i)) - 1) << i
    return out


# --- Hizalama -------------------------------------------------------------
def _score(speech: int, cues: int) -> float:
    """Altyazı kovalarının kaçı sesin üstüne düşüyor (asimetrik, bkz. başlık)."""
    total = cues.bit_count()
    return (speech & cues).bit_count() / total if total else 0.0


def best_offset(speech: int, cues: int,
                max_lag: float = MAX_LAG_S) -> tuple[float, float, float, float]:
    """(kayma, tepe puanı, rakip puanı, z). Kayma, altyazıya EKLENECEK saniye.

    `z`, tepenin eğrinin genelinden kaç MAD yukarıda olduğu — doyuma ulaşmış
    seslerde mutlak farklar anlamını yitirdiği için asıl güven ölçütü budur
    (bkz. modül başındaki ölçüm tablosu).
    """
    steps = int(max_lag * 1000 // BIN_MS)
    scored: list[tuple[float, float]] = []
    for k in range(-steps, steps + 1):
        moved = cues << k if k >= 0 else cues >> -k
        if moved.bit_count() < MIN_CUE_BINS:
            continue
        scored.append((_score(speech, moved), k * BIN_MS / 1000))
    if not scored:
        return 0.0, 0.0, 0.0, 0.0
    # Eşitlikte EN KÜÇÜK kayma kazanır: aynı puanı veren iki aday varsa
    # altyazıyı daha az oynatan doğru seçimdir (periyodik/tekrarlı seslerde
    # birbirinin aynı tepeler çıkabiliyor).
    top_score, _, top_lag = max((sc, -abs(lg), lg) for sc, lg in scored)
    rival = max((sc for sc, lg in scored
                 if abs(lg - top_lag) > RIVAL_GUARD_S), default=0.0)

    puanlar = sorted(sc for sc, _ in scored)
    med = puanlar[len(puanlar) // 2]
    sapmalar = sorted(abs(p - med) for p in puanlar)
    mad = sapmalar[len(sapmalar) // 2] or 1e-9
    return top_lag, top_score, rival, (top_score - med) / mad


def analyze_spans(speech_list: list[tuple[float, float]], vtt: str,
                  window: float = WINDOW_S) -> SyncResult:
    """Kararın tamamı: KONUŞMA ARALIKLARI + altyazı -> kayma (ya da dokunma).

    Konuşma aralıklarının nereden geldiği önemsizdir. Masaüstünde ffmpeg
    `silencedetect` üretir (bkz. `analyze`), ama Android'de ffmpeg yok ve sesi
    işletim sisteminin kendi çözücüsü çözüp aralıkları buradan verebilir —
    eşikler, puanlama ve güven kapısı ikisinde de AYNI kod.

    (Denenip ELENEN ucuz yol: sesi hiç çözmeden AAC kare boyutlarından
    "dolu/boş" haritası çıkarmak. Ölçüldü, dört farklı eşikte de çuvalladı —
    gerçek altyazıyı rastgele üretilmiş sahte bir altyazıdan ayıramadı
    (z 2,7–4,1'e karşı sahtede 3,1–3,2). Sebebi, sıkıştırılmış seste müzik ve
    efektin de kareleri doldurması: "boş" diye bir şey kalmıyor. Bir daha
    denenmesin.)
    """
    cues = mask(cue_spans(vtt, window), window)
    if cues.bit_count() < MIN_CUE_BINS:
        return SyncResult(0.0, 0.0, 0.0, False, "ölçüme yetecek altyazı yok")
    speech = mask(speech_list, window)
    if not speech:
        return SyncResult(0.0, 0.0, 0.0, False, "seste konuşma aralığı bulunamadı")

    fill = speech.bit_count() / max(1, int(window * 1000 // BIN_MS))
    offset, score, rival, z = best_offset(speech, cues)
    out = lambda ok, why: SyncResult(offset, score, rival, ok, why, z, fill)

    if not MIN_FILL <= fill <= MAX_FILL:
        return out(False, f"ses çizgisi ölçüme uygun değil (doluluk "
                          f"%{fill * 100:.0f})")

    # Kare hızı oranları (bkz. RATES). Pencere dışındaki cue'lar oranla içeri
    # girebileceği için ham liste geniş alınır; mask() pencereye kırpar.
    ham = cue_spans(vtt, window / min(RATES))
    aday = None
    for r in RATES:
        cm = mask([(a * r, b * r) for a, b in ham], window)
        if cm.bit_count() < MIN_CUE_BINS:
            continue
        lag, sc, rv, zz = best_offset(speech, cm)
        if aday is None or zz > aday[3]:
            aday = (lag, sc, rv, zz, r)
    if aday:
        lag, sc, rv, zz, r = aday
        if (zz >= RATE_MIN_Z and zz >= z + RATE_MIN_GAIN and sc >= MIN_SCORE
                and sc - rv >= RATE_MIN_MARGIN):
            return SyncResult(lag, sc, rv, True,
                              f"kare hızı uyuşmazlığı: oran {r:.5f}, kayma "
                              f"{lag:+.1f} sn (z={zz:.1f}, sabit kaymada {z:.1f})",
                              zz, fill, r)
    if score < MIN_SCORE:
        return out(False, f"örtüşme zayıf ({score:.2f} < {MIN_SCORE})")
    if z < MIN_Z:
        return out(False, f"tepe sivrilmiyor (z={z:.1f} < {MIN_Z})")
    if score - rival < MIN_MARGIN:
        return out(False, f"tepe belirsiz (fark {score - rival:.3f})")
    if abs(offset) < MIN_SHIFT_S:
        return out(False, f"kayma önemsiz ({offset:+.1f} sn)")
    return out(True, f"kayma {offset:+.1f} sn (puan {score:.2f}, z={z:.1f}, "
                     f"fark {score - rival:.3f})")


def analyze(ff_stderr: str, vtt: str, window: float = WINDOW_S) -> SyncResult:
    """Masaüstü yolu: ffmpeg `silencedetect` çıktısı + altyazı -> karar."""
    return analyze_spans(speech_spans(ff_stderr, window), vtt, window)


# --- Uygulama -------------------------------------------------------------
def _fmt(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return "%02d:%02d:%06.3f" % (int(h), int(m), s)


def shift_vtt(vtt: str, offset: float, rate: float = 1.0) -> str:
    """Altyazıdaki tüm zaman damgalarını `t → t·rate + offset` ile taşır.

    `rate` 1 değilse kare hızı uyuşmazlığı düzeltilir (bkz. RATES). Yalnız
    zaman satırlarına dokunur; metin, konum ayarları (align/line) ve WEBVTT
    başlığı olduğu gibi kalır.
    """
    if not offset and rate == 1.0:
        return vtt

    def sub(m: re.Match) -> str:
        a = _secs(*_TS_RE.match(m.group(1)).groups()) * rate + offset
        b = _secs(*_TS_RE.match(m.group(3)).groups()) * rate + offset
        return f"{_fmt(a)} {m.group(2)} {_fmt(b)}"

    return _CUE_RE.sub(sub, vtt)


# --- ffmpeg tarafı (tek ağ/dış süreç dokunuşu) ----------------------------
def probe_cmd(ffmpeg: str, audio_url: str, window: float = WINDOW_S) -> list[str]:
    """Sesi ÇÖZÜP sessizlikleri yazdıran ffmpeg komutu.

    `-f null -` çıktıyı atar: ses kartına hiçbir şey gitmez, disk yazılmaz.
    """
    return [ffmpeg, "-nostdin", "-allowed_extensions", "ALL",
            "-i", audio_url, "-t", str(int(window)), "-vn",
            "-af", AUDIO_FILTER, "-f", "null", "-"]
