"""categories.build_genre_rows (ağsız): türe göre kovalama, sıra, eşik, tekilleştirme."""
from hayalet.core import categories


def test_ture_gore_kovalar_ve_sirali():
    # 4 Aksiyon, 4 Komedi (eşik=4). Bir yapım iki türde de olabilir.
    pairs = [
        (["Aksiyon", "Komedi"], "a1"),
        (["Aksiyon"], "a2"), (["Aksiyon"], "a3"), (["Aksiyon"], "a4"),
        (["Komedi"], "k1"), (["Komedi"], "k2"), (["Komedi"], "k3"),
    ]
    rows = categories.build_genre_rows(pairs, min_items=4)
    names = [g for g, _ in rows]
    # Aksiyon 4 → kalır, Komedi 4 (k1,k2,k3 + a1) → kalır; Aksiyon önce (sıra).
    assert names == ["Aksiyon", "Komedi"]
    assert rows[0][1] == ["a1", "a2", "a3", "a4"]
    assert set(rows[1][1]) == {"a1", "k1", "k2", "k3"}


def test_esik_altini_eler():
    pairs = [(["Korku"], "x"), (["Korku"], "y")]   # 2 < 4
    assert categories.build_genre_rows(pairs, min_items=4) == []


def test_ayni_yapim_turde_iki_kez_gelmez():
    pairs = [(["Dram"], "s"), (["Dram"], "s"), (["Dram"], "t"),
             (["Dram"], "u"), (["Dram"], "v")]
    rows = categories.build_genre_rows(pairs, min_items=4, key=lambda s: s)
    assert rows[0][0] == "Dram"
    assert rows[0][1] == ["s", "t", "u", "v"]        # 's' tek


def test_none_atlanir_ve_max_uygulanir():
    pairs = [(["Suç"], None)] + [(["Suç"], f"c{i}") for i in range(10)]
    rows = categories.build_genre_rows(pairs, min_items=4, max_items=3)
    assert rows[0][0] == "Suç" and len(rows[0][1]) == 3


def test_order_disi_tur_sona_eklenir():
    pairs = [(["Aksiyon"], f"a{i}") for i in range(4)] + \
            [(["Bollywood"], f"b{i}") for i in range(4)]
    rows = categories.build_genre_rows(pairs, order=["Aksiyon"], min_items=4)
    assert [g for g, _ in rows] == ["Aksiyon", "Bollywood"]
