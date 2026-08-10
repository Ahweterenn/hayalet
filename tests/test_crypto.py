"""Site kripto/deşifreleme zincirlerinin saf-mantık testleri (ağsız).

Buradaki fixture'lar CANLI siteden BİR KEZ yakalanmış, dondurulmuş gerçek
örneklerdir (tests/fixtures/). Amaç: site kripto şemasını (passphrase, packer/
unmix sabitleri) DEĞİL, BİZİM KODUMUZUN o örneği doğru çözdüğünü doğrulamak.
Bu testler kırılırsa iki olasılık var: (a) bizim kodumuzda regresyon var, ya da
(b) site şemasını değiştirdi ve fixture bayatladı — --doctor canlı kontrolüyle
birlikte kullanılınca ikisini ayırt etmek kolaylaşır (bkz. CLAUDE.md).
"""
from __future__ import annotations

import html
import json
from pathlib import Path

import pytest

from hayalet.core import utils
from hayalet.sites import hdfilmcehennemi_adapter as hdfc

_FIXTURES = Path(__file__).parent / "fixtures"


# --- Dizipal: data-rm-k (AES-CBC + PBKDF2-SHA512) --------------------------
def test_decrypt_rmk_known_sample():
    fixture = json.loads((_FIXTURES / "dizipal_rmk_sample.json").read_text(encoding="utf-8"))
    plaintext = utils.decrypt_rmk(fixture["raw"])
    assert plaintext == fixture["plaintext"]


def test_decrypt_rmk_wrong_passphrase_does_not_match():
    """Yanlış passphrase'le çözülen metin doğru düz metinle asla eşleşmemeli —
    decrypt_rmk'nın PBKDF2/AES'i gerçekten passphrase'e duyarlı kullandığını
    (ör. yanlışlıkla sabit/hardcoded bir anahtara düşmediğini) doğrular. Yanlış
    anahtarla ya PKCS7 unpad/utf-8 decode hata verir (beklenen, test geçer) ya da
    -çok düşük ihtimalle- çöp bir metin üretir; o durumda da doğru metinle
    eşleşmemesi gerekir."""
    fixture = json.loads((_FIXTURES / "dizipal_rmk_sample.json").read_text(encoding="utf-8"))
    try:
        result = utils.decrypt_rmk(fixture["raw"], passphrase="yanlis-passphrase")
    except Exception:
        return
    assert result != fixture["plaintext"]


def test_decrypt_rmk_html_entities_unescaped():
    """raw JSON, HTML-escape edilmiş (&quot; vb.) geliyor — decrypt_rmk bunu
    kendi içinde unescape ediyor mu? (bkz. utils.decrypt_rmk: html.unescape)"""
    fixture = json.loads((_FIXTURES / "dizipal_rmk_sample.json").read_text(encoding="utf-8"))
    assert "&quot;" in fixture["raw"]           # fixture gerçekten escape'li mi (test kendini doğrular)
    assert html.unescape(fixture["raw"]).startswith('{"ciphertext"')
    # decrypt_rmk doğrudan escape'li haliyle çağrılabilmeli (extractor.py'nin
    # kullandığı gerçek şekil budur):
    assert utils.decrypt_rmk(fixture["raw"]) == fixture["plaintext"]


# --- hdfilmcehennemi: Dean Edwards packer + rastgele sıralı "unmix" --------
def test_extract_master_url_known_sample():
    """Gerçek /rplayer/ embed sayfasından yakalanmış bir örnek: packer açımlama +
    (reverse/rot/atob adımlarının o anki rastgele sırası) + son sayısal
    bayt-kaydırma zincirinin TAMAMI. Ops sırası her sayfa yüklemesinde rastgele
    değişse de, bu SABİT örnekte hep aynı ops sırası ayrıştırılıp uygulanmalı —
    yani test deterministiktir (bkz. hdfc adapter modül docstring'i)."""
    embed_html = (_FIXTURES / "hdfc_embed_sample.html").read_text(encoding="utf-8")
    expected = (_FIXTURES / "hdfc_expected_master.txt").read_text(encoding="utf-8").strip()
    assert hdfc._extract_master_url(embed_html) == expected


def test_apply_ops_reverse():
    assert hdfc._apply_ops("abcdef", [("reverse", None)]) == "fedcba"


def test_apply_ops_rot():
    # ROT13: 'a'+13 -> 'n', 'z'+13 -> 'm' (26'ya sar); büyük/küçük harf korunur.
    assert hdfc._apply_ops("abcXYZ", [("rot", 13)]) == "nopKLM"


def test_apply_ops_atob():
    import base64
    encoded = base64.b64encode(b"hello").decode()
    assert hdfc._apply_ops(encoded, [("atob", None)]) == "hello"


def test_apply_ops_combination_order_matters():
    """atob+reverse ile reverse+atob FARKLI davranmalı (biri geçerli base64
    üretirken diğeri bozuyor) — _apply_ops'un ops listesini SIRAYLA (kaynak
    koddaki görünme sırasına göre) uyguladığını, sabit bir sıra varsaymadığını
    doğrular (bkz. modül docstring'i: gerçek sitede sıra rastgele değişiyor)."""
    import base64
    b64 = base64.b64encode(b"hello world").decode()

    atob_then_reverse = hdfc._apply_ops(b64, [("atob", None), ("reverse", None)])
    assert atob_then_reverse == "dlrow olleh"

    with pytest.raises(Exception):
        # base64 metnini TERS ÇEVİRİP SONRA çözmeye çalışmak geçersiz padding'e
        # düşer — sıra önemli olmasaydı bu da sorunsuz çözülürdü.
        hdfc._apply_ops(b64, [("reverse", None), ("atob", None)])
