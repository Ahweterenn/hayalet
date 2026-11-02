"""remote.get (ağsız): cache yoksa/bozuksa gömülü varsayılana düşer, varsa okur.
Uzaktan ayarın GÜVENLİK AĞI'nı kilitler: en kötü durumda bugünkü davranış."""
import json
import os

import pytest

from hayalet import config
from hayalet.core import remote


@pytest.fixture(autouse=True)
def _remote_temiz():
    """Her testten ÖNCE ve SONRA modül-seviyesi remote durumunu sıfırla —
    yoksa bellekte kalan ayar başka test dosyalarına sızar (ör. selcukflix)."""
    remote._reset_for_test()
    yield
    remote._reset_for_test()


def _write(path, data):
    os.makedirs(str(path), exist_ok=True)
    # ts çok ileri → "taze" sayılır, arka plan çekmesi tetiklenmez (ağ yok).
    with open(os.path.join(str(path), "remote_config.json"), "w", encoding="utf-8") as f:
        json.dump({"ts": 9e18, "data": data}, f)


def test_cache_yoksa_varsayilan(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(remote, "REMOTE_CONFIG_URL", "")   # uzak kapalı
    remote._reset_for_test()
    assert remote.get("dizibox.known_domain", "https://dizibox.live") == "https://dizibox.live"


def test_cache_degeri_okunur(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(remote, "REMOTE_CONFIG_URL", "")
    remote._reset_for_test()
    _write(tmp_path, {"dizibox.known_domain": "https://yeni-adres.site",
                      "selcukflix.key_seed": "YENI_TOHUM"})
    assert remote.get("dizibox.known_domain", "https://eski") == "https://yeni-adres.site"
    assert remote.get("selcukflix.key_seed", "!!22xx!!90!!") == "YENI_TOHUM"
    # Dosyada olmayan anahtar → varsayılan.
    assert remote.get("yok.anahtar", "varsayilan") == "varsayilan"


def test_bozuk_cache_varsayilana_duser(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(remote, "REMOTE_CONFIG_URL", "")
    remote._reset_for_test()
    (tmp_path / "remote_config.json").write_text("{bozuk json", encoding="utf-8")
    assert remote.get("a.b", "gömülü") == "gömülü"


def test_selcukflix_anahtari_uzaktan_tohumla_turetilir(tmp_path, monkeypatch):
    """Adaptör entegrasyonu: tohum uzaktan gelince AES anahtarı ona göre türer."""
    import base64
    import hashlib
    from hayalet.sites import selcukflix_adapter as sf
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(remote, "REMOTE_CONFIG_URL", "")
    remote._reset_for_test()
    _write(tmp_path, {"selcukflix.key_seed": "DENEME"})
    beklenen = base64.b64encode(hashlib.sha256(b"DENEME").digest())[:32]
    assert sf._key() == beklenen
