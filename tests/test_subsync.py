"""Altyazı-ses hizalamanın saf-mantık testleri (ağsız, ffmpeg'siz).

Canlı ölçümün (bkz. core/subsync.py başlığı) kodlanmış hâli: bilinen bir kayma
verilip geri bulunması beklenir. Gerçek veride altı kaymanın altısı da 0,1 sn
hatayla bulunmuştu; buradaki sentetik veri aynı davranışı regresyona karşı
kilitler.
"""
from __future__ import annotations

from hayalet.core import subsync


def _stderr(silences):
    """silencedetect çıktısını taklit eder."""
    out = []
    for a, b in silences:
        out.append(f"[silencedetect @ 000] silence_start: {a}")
        out.append(f"[silencedetect @ 000] silence_end: {b} | silence_duration: {b - a}")
    return "\n".join(out)


def _vtt(spans):
    """(başlangıç, bitiş) listesinden WEBVTT üretir (MM:SS.mmm biçimi)."""
    blocks = ["WEBVTT", ""]
    for i, (a, b) in enumerate(spans, 1):
        blocks.append(f"{int(a // 60):02d}:{a % 60:06.3f} --> "
                      f"{int(b // 60):02d}:{b % 60:06.3f}")
        blocks.append(f"satir {i}")
        blocks.append("")
    return "\n".join(blocks)


# --- Ayrıştırma -----------------------------------------------------------
def test_speech_spans_is_inverse_of_silence():
    spans = subsync.speech_spans(_stderr([(2.0, 5.0), (8.0, 9.0)]), window=10)
    assert spans == [(0.0, 2.0), (5.0, 8.0), (9.0, 10.0)]


def test_speech_spans_closes_open_silence_at_window_end():
    """Son sessizlik kapanmamışsa pencere sonuna kadar sürer."""
    assert subsync.speech_spans("silence_start: 7.5", window=10) == [(0.0, 7.5)]


def test_cue_spans_reads_mm_ss_form():
    """Altyazılar saat hanesi OLMADAN geliyor — bu biçim okunamazsa her şey 0 döner."""
    assert subsync.cue_spans(_vtt([(61.5, 63.0)]), window=600) == [(61.5, 63.0)]


def test_cue_spans_reads_hh_mm_ss_form():
    vtt = "WEBVTT\n\n01:02:03.500 --> 01:02:05.000\nmerhaba\n"
    assert subsync.cue_spans(vtt, window=7200) == [(3723.5, 3725.0)]


def test_cue_spans_drops_nonspeech_lines():
    """[kapı çalar] gibi satırların sesle hizalanacak konuşması yok."""
    vtt = ("WEBVTT\n\n00:01.000 --> 00:02.000\n[kapı çalar]\n\n"
           "00:05.000 --> 00:06.000\nkonuşma\n")
    assert subsync.cue_spans(vtt, window=60) == [(5.0, 6.0)]


# --- Hizalama -------------------------------------------------------------
def _speech_line(window: float = 600.0):
    """Düzensiz konuşma aralıkları.

    DÜZENSİZ olması şart: tam periyodik bir ses çizgisinde korelasyon da
    periyodik olur, periyodun katları birbirinin aynı puanı alır ve "doğru
    kayma" diye bir şey kalmaz. Gerçek diyalog böyle değil.
    """
    import random
    rnd = random.Random(42)
    spans, t = [], 0.0
    while t < window - 20:
        t += rnd.uniform(0.8, 5.0)
        spans.append((t, t + rnd.uniform(0.6, 3.5)))
        t = spans[-1][1]
    return spans


def _world(offset: float, window: float = 600.0):
    """Konuşma çizgisi + ondan `offset` kadar kaydırılmış altyazı."""
    speech = _speech_line(window)
    cues = [(a + offset, b + offset) for a, b in speech]
    return (subsync.mask(speech, window),
            subsync.mask(cues, window))


def test_best_offset_recovers_known_shift():
    """Canlı deneyin özü: bilinen kayma, ters işaretiyle geri bulunmalı."""
    for truth in (0.5, 1.5, -2.0, 4.0, -7.5, 15.0):
        speech, cues = _world(truth)
        found, score, _, z = subsync.best_offset(speech, cues)
        assert z >= subsync.MIN_Z, f"{truth:+} sn için tepe sivrilmedi (z={z:.1f})"
        assert abs(found + truth) <= subsync.BIN_MS / 1000, \
            f"{truth:+} sn için {found:+} bulundu"
        assert score > 0.9


def test_analyze_applies_real_shift():
    window = 600.0
    speech = _speech_line(window)
    cues = [(a + 4.0, b + 4.0) for a, b in speech]
    r = subsync.analyze(_stderr(_gaps(speech, window)), _vtt(cues), window)
    assert r.apply
    assert abs(r.offset + 4.0) <= 0.1


def test_analyze_leaves_aligned_subtitle_alone():
    """Kayma yoksa DOKUNMA — ölçülen gerçek bölümde durum buydu."""
    window = 600.0
    speech = _speech_line(window)
    r = subsync.analyze(_stderr(_gaps(speech, window)), _vtt(speech), window)
    assert not r.apply
    assert "önemsiz" in r.reason


def test_analyze_refuses_when_nothing_matches():
    """Ses ile altyazı örtüşmüyorsa tahmin yürütme, geri çekil."""
    window = 600.0
    speech = [(t, t + 0.4) for t in range(0, 590, 30)]      # seyrek konuşma
    cues = [(t, t + 2.0) for t in range(2, 590, 3)]          # yoğun altyazı
    r = subsync.analyze(_stderr(_gaps(speech, window)), _vtt(cues), window)
    assert not r.apply


def test_analyze_without_subtitle_is_a_noop():
    r = subsync.analyze(_stderr([(1, 2)]), "WEBVTT\n", 600.0)
    assert not r.apply and r.offset == 0.0


def _gaps(spans, window):
    """Konuşma aralıklarının tersi: silencedetect'in bildireceği sessizlikler."""
    out = []
    prev = 0.0
    for a, b in spans:
        if a > prev:
            out.append((prev, a))
        prev = b
    if prev < window:
        out.append((prev, window))
    return out


# --- Uygulama -------------------------------------------------------------
def test_shift_vtt_moves_times_and_keeps_text():
    vtt = "WEBVTT\n\n00:10.000 --> 00:12.500\nilk satir\n"
    out = subsync.shift_vtt(vtt, -2.5)
    assert "00:00:07.500 --> 00:00:10.000" in out
    assert "ilk satir" in out
    assert out.startswith("WEBVTT")


def test_shift_vtt_clamps_at_zero():
    """Negatif zamana taşan cue 0'a sabitlenir — oynatıcılar eksi kabul etmez."""
    out = subsync.shift_vtt("WEBVTT\n\n00:01.000 --> 00:03.000\nx\n", -5.0)
    assert "00:00:00.000 --> 00:00:00.000" in out


def test_shift_vtt_roundtrip_is_identity():
    vtt = _vtt([(5.0, 7.0), (20.25, 22.75)])
    there = subsync.shift_vtt(vtt, 3.0)
    back = subsync.shift_vtt(there, -3.0)
    assert subsync.cue_spans(back, 600) == subsync.cue_spans(vtt, 600)


def test_shift_vtt_zero_offset_changes_nothing():
    vtt = _vtt([(1.0, 2.0)])
    assert subsync.shift_vtt(vtt, 0) == vtt


# --- Kare hızı uyuşmazlığı (zamanla büyüyen kayma) ------------------------
def test_frame_rate_mismatch_is_found_and_undone():
    """Altyazı 25/23,976 oranında gerilmiş: sabit kayma bunu düzeltemez
    (bölüm sonunda dakikalar kayar); oran bulunup tamamen geri alınmalı."""
    window = 600.0
    speech = _speech_line(window)
    for oran in (25 / 23.976, 23.976 / 25):
        cues = [(a * oran + 2.0, b * oran + 2.0) for a, b in speech]
        r = subsync.analyze(_stderr(_gaps(speech, window)), _vtt(cues), window)
        assert r.apply and r.rate != 1.0, r.reason
        geri = subsync.cue_spans(subsync.shift_vtt(_vtt(cues), r.offset, r.rate), 10 ** 6)
        assert max(abs(x[0] - y[0]) for x, y in zip(geri, speech)) <= 0.15


def test_aligned_subtitle_gets_no_rate():
    """Doğru altyazıya oran UYGULANMAMALI — yanlış oran onu zamanla bozardı."""
    window = 600.0
    speech = _speech_line(window)
    r = subsync.analyze(_stderr(_gaps(speech, window)), _vtt(speech), window)
    assert not r.apply and r.rate == 1.0


def test_shift_vtt_rate_scales_times():
    out = subsync.shift_vtt("WEBVTT\n\n01:40.000 --> 01:42.000\nx\n", 1.0, rate=1.5)
    assert "00:02:31.000 --> 00:02:34.000" in out


def test_shift_vtt_rate_roundtrip():
    vtt = _vtt([(5.0, 7.0), (300.0, 302.5)])
    r = 25 / 23.976
    there = subsync.shift_vtt(vtt, 0.0, r)
    back = subsync.shift_vtt(there, 0.0, 1 / r)
    for (a, b), (c, d) in zip(subsync.cue_spans(back, 600), subsync.cue_spans(vtt, 600)):
        assert abs(a - c) < 0.002 and abs(b - d) < 0.002
