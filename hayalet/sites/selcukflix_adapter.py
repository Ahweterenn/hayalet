"""selcukflix.app site adapter'ı (yabancı dizi/film, Dizipal Next.js klonu).

asyawatch ile **aynı motor** (ortak `_dizipal_next.DizipalNextAdapter`): arama
`/api/bg/searchContent`, veri `__NEXT_DATA__.secureData`, oynatıcı pichive. Tek
fark: yük **AES-256-CBC ile şifreli** (asyawatch'ta düz base64'tü). Anahtar
dizisol ile birebir aynı: `base64(sha256("!!22xx!!90!!"))[:32]`, sıfır IV
(canlı doğrulandı 2026-11; sitenin `_app` bundle'ındaki aes-256-cbc şemasından).
"""
from __future__ import annotations

import base64
import hashlib
import json

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from hayalet.core import remote
from hayalet.core.extractor import ExtractError
from hayalet.core.sites import register
from hayalet.sites._dizipal_next import DizipalNextAdapter

# Tohum (passphrase) UZAKTAN güncellenebilir: site anahtarı döndürürse APK'sız
# `remote_config.json`'da "selcukflix.key_seed" ile düzeltilir; yoksa gömülü.
_DEFAULT_SEED = "!!22xx!!90!!"
_IV = b"\x00" * 16
# Gömülü (varsayılan) anahtar — remote kapalıyken ve testlerde kullanılır.
_KEY = base64.b64encode(hashlib.sha256(_DEFAULT_SEED.encode("utf-8")).digest())[:32]


def _key() -> bytes:
    """Çalışma anındaki anahtar: tohum UZAKTAN güncellenebilir, yoksa gömülü."""
    seed = remote.get("selcukflix.key_seed", _DEFAULT_SEED)
    return _KEY if seed == _DEFAULT_SEED else \
        base64.b64encode(hashlib.sha256(seed.encode("utf-8")).digest())[:32]


class SelcukflixAdapter(DizipalNextAdapter):
    name = "selcukflix"
    known_domain = "https://selcukflix.app"

    def _decode(self, raw: str):
        """base64 -> AES-256-CBC çöz -> PKCS7 kaldır -> JSON."""
        try:
            plain = unpad(AES.new(_key(), AES.MODE_CBC, _IV)
                          .decrypt(base64.b64decode(raw)), AES.block_size)
            return json.loads(plain.decode("utf-8"))
        except (ValueError, json.JSONDecodeError) as e:
            raise ExtractError(f"selcukflix: yük çözülemedi "
                               f"(anahtar/şema değişmiş olabilir): {e}")


register(SelcukflixAdapter())
