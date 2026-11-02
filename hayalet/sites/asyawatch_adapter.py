"""asyawatch.com site adapter'ı (Asya/Kore/Çin dizileri, Dizipal Next.js klonu).

Ortak `_dizipal_next.DizipalNextAdapter` tabanını kullanır; tek farkı, bu sitede
`secureData`/`response` yükünün **şifresiz, düz base64 JSON** olması (selcukflix
aynı motoru AES şifreyle kullanıyor — bkz. selcukflix_adapter).
"""
from __future__ import annotations

import base64
import json

from hayalet.core.sites import register
from hayalet.sites._dizipal_next import DizipalNextAdapter


class AsyawatchAdapter(DizipalNextAdapter):
    name = "asyawatch"
    known_domain = "https://asyawatch.com"

    def _decode(self, raw: str):
        """asyawatch yükü: düz base64 JSON (`eyJ...`); '=' dolgusu eksik olabilir."""
        return json.loads(base64.b64decode(raw + "==="))


register(AsyawatchAdapter())
