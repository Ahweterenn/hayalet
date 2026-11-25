"""NP oynatıcı gate'i (core/npgate.py) — ağsız.

Örnek NP_GATE gövdesi canlı bir pichive iframe'inden (2026-11) alındı; tuz,
kelime indeksi ve kaydırma her yüklemede değişir, bu yüzden parçalar
kaynaktan okunmalı.
"""
import hashlib

import pytest

from hayalet.core import npgate

_LIVE = ("<script>\nwindow.NP_GATE=function(_ib,_cxh){var _byjp=String.fromCharCode("
         "72,111,95,54,56,67,107,101,120,83);for(var _hu=0;_hu<1e8;_hu++){var _cek="
         "_hu.toString(10);if((_cxh(_ib+':'+_byjp+':'+_cek)[2]>>>20)===0)return _cek;}"
         "return \"\";};\nwindow.NP_CONFIG = {};</script>")


def _w(s):
    d = hashlib.sha256(s.encode()).digest()
    return [int.from_bytes(d[i:i + 4], "big") for i in range(0, 32, 4)]


def test_custom_gate_parsed_and_solved():
    parts, idx, test, radix = npgate._parse_custom(_LIVE)
    assert parts == ["N", "=:", "=Ho_68CkexS", "=:", "I"]
    assert (idx, test, radix) == (2, (">>>", 20), 10)
    c = npgate.solve(_LIVE, {"n": "LdXlQ9GtaR89", "b": 12})
    assert _w("LdXlQ9GtaR89:Ho_68CkexS:" + c)[2] >> 20 == 0
    # ilk çözüm olmalı (oynatıcı da ilkini döndürür)
    assert all(_w(f"LdXlQ9GtaR89:Ho_68CkexS:{i}")[2] >> 20 for i in range(int(c)))


# 2026-11-25 selcukflix: üst bit kaydırması yerine alt bit maskesi (& 4095),
# taban 36 ve tuz sayacın hem önünde hem arkasında.
_LIVE_MASK = ("<script>window.NP_GATE=function(_ta,_pqd){var _fdcr=String.fromCharCode("
              "106,102,56,78,111,68,118,67,121,49);for(var _st=0;_st<1e8;_st++){var _muw="
              "_st.toString(36);if((_pqd(_ta+_fdcr+_muw+_fdcr)[3]&4095)===0)return _muw;}"
              "return \"\";};\nwindow.NP_CONFIG = {};</script>")


def test_mask_gate_parsed_and_solved():
    parts, idx, test, radix = npgate._parse_custom(_LIVE_MASK)
    assert parts == ["N", "=jf8NoDvCy1", "I", "=jf8NoDvCy1"]
    assert (idx, test, radix) == (3, ("&", 4095), 36)
    c = npgate.solve(_LIVE_MASK, {"n": "abc"})
    assert _w("abcjf8NoDvCy1" + c + "jf8NoDvCy1")[3] & 4095 == 0
    assert all(_w("abcjf8NoDvCy1" + npgate._to_radix(i, 36) + "jf8NoDvCy1")[3] & 4095
               for i in range(int(c, 36)))


def test_default_gate_without_np_gate():
    c = npgate.solve("<html></html>", {"n": "abc", "b": 8})
    assert _w("abc:" + c)[0] >> 24 == 0


def test_apply_appends_to_master_and_subtitles():
    cfg = {"gate": {"n": "abc", "b": 4},
           "sources": [{"master": "https://h/nm.php?v=1"}],
           "subtitles": [{"lang": "tr", "url": "https://h/nsub.php?v=2", "tok": "2"}]}
    npgate.apply("", cfg)
    p = cfg["sources"][0]["master"].split("&p=")[1]
    assert cfg["subtitles"][0]["url"].endswith("&p=" + p)
    assert cfg["subtitles"][0]["_gate"] == "&p=" + p


def test_apply_no_gate_is_noop():
    cfg = {"sources": [{"master": "https://h/m.m3u8"}]}
    assert npgate.apply("", cfg)["sources"][0]["master"] == "https://h/m.m3u8"


def test_unknown_expression_raises():
    bad = _LIVE.replace("_ib+':'+_byjp", "_ib+foo()")
    with pytest.raises(npgate.GateError):
        npgate.solve(bad, {"n": "x", "b": 12})
