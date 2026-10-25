"""Altyazının videonun zaman çizgisine eşlenmesi (X-TIMESTAMP-MAP).

Eşleme olmadan oynatıcı tahmin ediyor ve altyazı kayıyordu (ölçüldü: hls.js'te
Dizipal 1,4 sn, hdfilmcehennemi 0,101 sn erken; Media3'te ~0,7 sn). Buradaki
testler ağsız: sahte MPEG-TS paketleri ve elle yazılmış WebVTT.
"""
from hayalet.core.proxy import add_timestamp_map, first_pts


def _pes_paketi(pid: int, stream_id: int, pts: float) -> bytes:
    """Tek bir 188 baytlık TS paketi: PES başı + PTS (90 kHz)."""
    t = round(pts * 90000)
    pts_b = bytes([
        0x21 | ((t >> 29) & 0x0E),
        (t >> 22) & 0xFF,
        0x01 | ((t >> 14) & 0xFE),
        (t >> 7) & 0xFF,
        0x01 | ((t << 1) & 0xFE),
    ])
    pes = b"\x00\x00\x01" + bytes([stream_id, 0, 0, 0x80, 0x80, 5]) + pts_b
    bas = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10])   # yük başlangıcı
    return (bas + pes).ljust(188, b"\xff")


def _dolgu(pid: int = 0x1FFF) -> bytes:
    return bytes([0x47, pid >> 8, pid & 0xFF, 0x10]).ljust(188, b"\xff")


def test_first_pts_reads_single_stream():
    veri = _pes_paketi(256, 0xE0, 1.4) + _dolgu() * 5
    assert abs(first_pts(veri) - 1.4) < 1e-6


def test_first_pts_takes_earliest_stream_not_video():
    # hdfilmcehennemi şekli: görüntü 0,184'te, gömülü ses 0,101'de. Oynatıcı
    # zaman çizgisini en erkene kuruyor; görüntünün değeri fazla düzeltiyordu.
    veri = (_pes_paketi(256, 0xE0, 0.1844) + _pes_paketi(257, 0xC0, 0.101)
            + _dolgu() * 5)
    assert abs(first_pts(veri) - 0.101) < 1e-6


def test_first_pts_ignores_later_packets_of_same_stream():
    veri = (_pes_paketi(256, 0xE0, 1.4) + _pes_paketi(256, 0xE0, 0.2)
            + _dolgu() * 5)
    assert abs(first_pts(veri) - 1.4) < 1e-6


def test_first_pts_skips_fake_image_header():
    # Bazı CDN'ler parçanın başına sahte resim başlığı koyuyor.
    veri = b"\x89PNG\r\n\x1a\n" + b"\x00" * 300 + _pes_paketi(256, 0xE0, 2.5) \
        + _dolgu() * 5
    assert abs(first_pts(veri) - 2.5) < 1e-6


def test_first_pts_not_ts_returns_none():
    assert first_pts(b"<html>hata</html>" * 50) is None
    assert first_pts(b"") is None


VTT = b"WEBVTT\n\n00:01.000 --> 00:02.000\nmerhaba\n"


def test_map_inserted_after_header():
    out = add_timestamp_map(VTT, 1.4).decode()
    satirlar = out.splitlines()
    assert satirlar[0] == "WEBVTT"
    assert satirlar[1] == "X-TIMESTAMP-MAP=MPEGTS:126000,LOCAL:00:00:00.000"
    assert "00:01.000 --> 00:02.000" in out          # cue'lara dokunulmaz


def test_map_handles_bom_and_crlf():
    out = add_timestamp_map(b"\xef\xbb\xbfWEBVTT\r\n\r\n00:01.000 --> 00:02.000\r\nx\r\n",
                            0.101)
    assert out.startswith(b"WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:9090,")


def test_map_not_added_without_pts_or_twice():
    assert add_timestamp_map(VTT, None) == VTT
    bir = add_timestamp_map(VTT, 1.4)
    assert add_timestamp_map(bir, 2.0) == bir        # var olan eşleme korunur


def test_map_leaves_non_vtt_alone():
    assert add_timestamp_map(b"<html>404</html>", 1.4) == b"<html>404</html>"
