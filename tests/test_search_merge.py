"""Çoklu-site sonuç birleştirme: aynı yapımın kopyaları tek satıra iner.

Kullanıcının kurgusu "filmler hdfilmcehennemi, diziler Dizipal"dı ve bu eskiden
adapter'da KATI SÜZGEÇ olarak uygulanıyordu: Dizipal'in bütün "Movies" kayıtları
atılıyordu. Sonuç: yalnızca Dizipal'de bulunan Türk filmleri (Sıfır Bir, Adana
İşi…) hiçbir şekilde erişilemiyordu. Artık aynı kurgu TERCİH olarak, tekrarın
asıl oluştuğu yerde — birleştirme anında — uygulanıyor.

Saf/ağsız: `_dedupe_cross_site` yalnızca `catalog.is_movie` ve `query`ye bakar.
"""
from hayalet.core import sites
from hayalet.core.models import Series


def _s(name, site, movie=False, slug=None):
    return Series(name=name, slug=slug or name.lower().replace(" ", "-"),
                  type="Movies" if movie else "Series", site=site)


def _names(rows):
    return [(r.name, r.site) for r in rows]


# --- asıl amaç: tekrar tek satıra insin ------------------------------------
def test_ayni_film_iki_siteden_tek_satir():
    rows = sites._dedupe_cross_site([
        _s("The Matrix", "dizipal", movie=True),
        _s("The Matrix", "hdfilmcehennemi", movie=True),
    ])
    assert _names(rows) == [("The Matrix", "hdfilmcehennemi")]


def test_ayni_dizi_iki_siteden_tek_satir():
    rows = sites._dedupe_cross_site([
        _s("The Last of Us", "hdfilmcehennemi"),
        _s("The Last of Us", "dizipal"),
    ])
    assert _names(rows) == [("The Last of Us", "dizipal")]


def test_tercih_sirasi_gelis_sirasindan_bagimsiz():
    # Siteler paralel sorgulanıyor; hangisinin önce döndüğü rastgele.
    for sira in ([("dizipal", True), ("hdfilmcehennemi", True)],
                 [("hdfilmcehennemi", True), ("dizipal", True)]):
        rows = sites._dedupe_cross_site([_s("Inception", s, movie=m)
                                         for s, m in sira])
        assert _names(rows) == [("Inception", "hdfilmcehennemi")]


def test_cok_dilli_ad_parcasi_uzerinden_eslesir():
    # hdfilmcehennemi tek alanda birkaç dilde ad taşıyor; ortak parça yeterli.
    rows = sites._dedupe_cross_site([
        _s("The Matrix 2 - The Matrix Reloaded", "hdfilmcehennemi", movie=True),
        _s("The Matrix Reloaded", "dizipal", movie=True),
    ])
    assert len(rows) == 1
    assert rows[0].site == "hdfilmcehennemi"


def test_turkce_karakter_ve_dublaj_eki_tekrari_gizlemez():
    rows = sites._dedupe_cross_site([
        _s("Adana isi", "dizipal", movie=True),
        _s("Adana İşi Türkçe Dublaj", "hdfilmcehennemi", movie=True),
    ])
    assert len(rows) == 1


# --- asıl regresyon: tek kaynakta olan yapım kaybolmamalı ------------------
def test_yalniz_dizipalde_olan_film_elenmez():
    # Eski katı süzgecin kurbanı. hdfilmcehennemi'de bu film HİÇ yok.
    rows = sites._dedupe_cross_site([_s("Sıfır Bir", "dizipal", movie=True)])
    assert _names(rows) == [("Sıfır Bir", "dizipal")]


def test_ayni_adli_dizi_ve_film_birlestirilmez():
    # Dizipal'de "Sıfır Bir" hem dizi hem film — bunlar tekrar değil, iki yapım.
    rows = sites._dedupe_cross_site([
        _s("Sıfır Bir", "dizipal", movie=True, slug="movies/sifir-bir"),
        _s("Sıfır Bir", "dizipal", movie=False, slug="series/sifir-bir"),
    ])
    assert len(rows) == 2
    assert {r.type for r in rows} == {"Movies", "Series"}


def test_farkli_yapimlar_birlestirilmez():
    rows = sites._dedupe_cross_site([
        _s("Dark", "dizipal"),
        _s("Dark Places", "hdfilmcehennemi", movie=True),
        _s("The Dark Money Game", "hdfilmcehennemi", movie=True),
    ])
    assert len(rows) == 3


def test_bos_liste():
    assert sites._dedupe_cross_site([]) == []
