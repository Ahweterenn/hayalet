"""core/query.py — normalleştirme, varyant üretimi, puanlama (saf, ağsız).

Buradaki asıl vaka gerçek bir kullanıcı şikayetinden geliyor: hdfilmcehennemi'de
"spiderman" hiç sonuç vermiyor, "spider-man" veriyor. Testler hem eşleştirmenin
bu farkı yok saydığını hem de "geniş ağ"ın alakasız sonuçları içeri almadığını
(eşik) doğruluyor.
"""
import pytest

from hayalet.core import query as q
from hayalet.core.models import Series


# --- normalize / squash ----------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("Spider-Man", "spider man"),
    ("Örümcek Adam", "orumcek adam"),
    ("YÜZÜKLERİN EFENDİSİ", "yuzuklerin efendisi"),
    ("Şahsiyet", "sahsiyet"),
    ("  Ağır   Suçlar  ", "agir suclar"),
    ("Wall·E (2008)", "wall e 2008"),
])
def test_normalize(raw, expected):
    assert q.normalize(raw) == expected


def test_squash_bosluk_ve_tireyi_yok_sayar():
    assert q.squash("spider-man") == q.squash("Spider Man") == "spiderman"
    assert q.squash("Örümcek-Adam") == "orumcekadam"


# --- score -----------------------------------------------------------------
def test_tire_farki_tam_eslesme_sayilir():
    assert q.score("spiderman", "Spider-Man") == 1.0
    assert q.score("spider man", "Spider-Man") == 1.0


def test_turkce_karakter_farki_tam_eslesme_sayilir():
    assert q.score("orumcek adam", "Örümcek Adam") == 1.0
    assert q.score("yuzuklerin efendisi", "Yüzüklerin Efendisi") == 1.0


def test_devam_filmi_yuksek_ama_birebirden_dusuk():
    tam = q.score("spiderman", "Spider-Man")
    devam = q.score("spiderman", "Spider-Man: No Way Home")
    assert devam >= q.MIN_SCORE
    assert devam < tam


def test_alakasiz_baslik_esigin_altinda():
    for baslik in ["Kayıp Balık Nemo", "Breaking Bad", "Interstellar"]:
        assert q.score("spiderman", baslik) < q.MIN_SCORE


def test_dublaj_eki_eslesmeyi_bozmaz():
    assert q.score("house", "House M.D. Türkçe Dublaj") >= q.MIN_SCORE


# --- variants --------------------------------------------------------------
def test_bilesik_kelime_ayrilir():
    v = q.variants("spiderman")
    assert "spider man" in v


def test_varyantlar_sorgunun_kendisini_tekrarlamaz():
    # Birebir aynı metin tekrar aranmaz (boşa istek); ama "spiderman" için
    # "spider man" GEÇERLİ bir varyanttır — bizce aynı, site için değil.
    for sorgu in ["spiderman", "spider-man", "Örümcek Adam", "matrix"]:
        assert sorgu.lower() not in [x.lower() for x in q.variants(sorgu)]


def test_noktalama_katlanmis_hali_varyant_olur():
    assert "spider man" in q.variants("Spider-Man")


def test_gurultu_kelimeleri_atilir():
    assert "shrek" in q.variants("shrek türkçe dublaj izle")


def test_onek_varyanti_uretilir():
    # 'yuzuklerin' -> daha kısa bir ön ek; site "içinde geçen" araması yaptığı
    # için uzun/yanlış çekimlenmiş sorguları da yakalar.
    v = q.variants("yuzuklerin")
    assert any(len(x) < len("yuzuklerin") and "yuzuk" in x for x in v)


def test_varyant_sayisi_sinirli():
    # "Dozunda" olmalı: her arama site başına en fazla MAX_VARIANTS ek istek.
    assert len(q.variants("türkçe dublaj spiderman izle full hd")) <= q.MAX_VARIANTS


def test_kisa_sorgu_asiri_varyant_uretmez():
    assert len(q.variants("up")) <= 1


# --- rank ------------------------------------------------------------------
def _s(name):
    return Series(name=name, slug=name.lower().replace(" ", "-"), site="test")


def test_rank_birebir_eslesmeyi_basa_alir():
    results = [_s("Spider-Man: No Way Home"), _s("Spider-Man"),
               _s("Spider-Man 2")]
    assert q.rank("spiderman", results)[0].name == "Spider-Man"


def test_rank_gurultuyu_atar():
    results = [_s("Spider-Man"), _s("Breaking Bad"), _s("Interstellar")]
    kept = q.rank("spiderman", results)
    assert [r.name for r in kept] == ["Spider-Man"]


def test_rank_hepsi_zayifsa_listeyi_bosaltmaz():
    # Kullanıcıya "hiç sonuç yok" demektense zayıf eşleşmeleri göstermek yeğdir.
    results = [_s("Breaking Bad"), _s("Interstellar")]
    assert len(q.rank("spiderman", results)) == 2


def test_best_score_bos_listede_sifir():
    assert q.best_score("spiderman", []) == 0.0


# --- puanın uzunluğa duyarlılığı -------------------------------------------
# Canlı şikayet: "dizinin adını yazıyorum, başka şeyler çıkıyor". Sebebi puanın
# doymasıydı — sorguyu İÇEREN her başlık sabit 0.90 alıyordu, "gibi" araması 29
# sonucun hepsini aynı puana oturtuyordu. Artık fazlalık uzunluk puanı düşürür.
def test_iceren_baslik_fazlaligi_kadar_puan_kaybeder():
    tam = q.score("dark", "Dark")
    az_fazla = q.score("dark", "Dark Places")
    cok_fazla = q.score("dark", "The Dark Money Game")
    assert tam == 1.0
    assert tam > az_fazla > cok_fazla >= q.MIN_SCORE


def test_ayni_kelimeyi_iceren_basliklar_ayni_puani_almaz():
    # Eskiden dördü de 0.90 alıyordu; artık puan başlıktaki fazlalıkla azalıyor,
    # yani sıralama anlamlı. (Aynı uzunluktaki başlıkların eşit puan alması
    # doğaldır — iddia "hepsi farklı" değil, "kısa olan öne geçer".)
    basliklar = ["Patron Gibi", "Krallar Gibi Yaşa",
                 "Ne Zaman Her Şey Eskisi Gibi Olacak"]
    puanlar = [q.score("gibi", b) for b in basliklar]
    assert puanlar == sorted(puanlar, reverse=True)
    assert len(set(puanlar)) == len(basliklar)
    assert all(p < q.score("gibi", "Gibi") for p in puanlar)


# --- çok dilli başlıklar (hdfilmcehennemi tek alanda 2-3 dil taşıyor) -------
def test_cok_dilli_baslikta_tutan_ad_parcasi_gecerli():
    ad = "Kara Şövalye - The Dark Knight"
    assert q.score("the dark knight", ad) == 1.0
    assert q.score("kara sovalye", ad) == 1.0


def test_alt_titles_yalnizca_bosluklu_ayracta_boler():
    # 'Spider-Man'in tiresi ad parçası ayracı DEĞİL.
    assert q.alt_titles("Spider-Man") == ["Spider-Man"]
    assert "The Dark Knight" in q.alt_titles("Kara Şövalye - The Dark Knight")


def test_devam_filmi_alt_baslik_ayraci_ile_bolunmez():
    # ':' bölseydi 'No Way Home' ayrı ad parçası olur ve devam filmi birebir
    # eşleşme puanı alırdı.
    assert q.score("spiderman", "Spider-Man: No Way Home") < 1.0


# --- kuyruk kesimi ---------------------------------------------------------
def test_net_kazanan_varken_kuyruk_kesilir():
    results = [_s("Dark"), _s("Dark Places"), _s("The Dark Wizard"),
               _s("The Dark Money Game"), _s("Dark Minds"), _s("Dark Frequency"),
               _s("Echoes in the Dark"), _s("Dark Side Of Night"),
               _s("The Bleeding Dark"), _s("Bad Influence The Dark Side")]
    kept = q.rank("dark", results)
    assert kept[0].name == "Dark"
    assert len(kept) <= q.MIN_KEEP < len(results)


def test_kesim_devam_filmlerini_silip_listeyi_tek_satira_indirmez():
    results = [_s("Spider-Man"), _s("Spider-Man 2"), _s("Spider-Man 3"),
               _s("Spider-Man: No Way Home")]
    kept = q.rank("spiderman", results)
    assert kept[0].name == "Spider-Man"
    assert len(kept) == len(results)


def test_zayif_liste_kirpilir():
    # Hiçbiri eşiği geçemiyor: yine de bir şeyler gösterilir ama sayfa dolusu değil.
    results = [_s("Alakasız %d" % i) for i in range(20)]
    assert len(q.rank("spiderman", results)) == q.WEAK_LIMIT


def test_iki_kelimelik_sorguda_tek_kelime_tutmasi_yetmez():
    # "the last of us" -> 'The Last Rodeo' canlıda 16 çöp satırın kaynağıydı.
    results = [_s("The Last of Us"), _s("The Last Rodeo"), _s("The Last Kumite"),
               _s("The Last Frontier")]
    assert [r.name for r in q.rank("the last of us", results)] == ["The Last of Us"]


# --- coverage: çok kelimeli sorguda "tek kelimesi tutan" sonuçları ayıklama --
# Canlı gözlem: varyantlar doğru filmi buluyordu ama yanında "orumcek adam" ->
# 'Adam'/'Black Adam', "breaking bad" -> 'Break'/'Break In' gibi çöp de geliyordu.
def test_tek_kelimesi_tutan_sonuc_elenir():
    results = [_s("Örümcek Adam: Eve Dönüş Yok"), _s("Adam"), _s("Black Adam")]
    assert [r.name for r in q.rank("orumcek adam", results)] == \
        ["Örümcek Adam: Eve Dönüş Yok"]


def test_kelime_onunun_eki_kelime_sayilmaz():
    # 'break', 'breaking'in eki DEĞİL — uzunlukları yeterince yakın değil.
    results = [_s("Breaking Bad"), _s("Break"), _s("Break In")]
    assert [r.name for r in q.rank("breaking bad", results)] == ["Breaking Bad"]


def test_turkce_eki_affedilir():
    # 'yuzukler' ile 'yuzuklerin' aynı kelime sayılmalı (uzunluklar yakın).
    assert q.coverage("yuzuklerin efendisi", "Yüzükler Efendisi") == 1.0


def test_coverage_tek_kelimede_devre_disi():
    # Tek kelimelik sorguda kapsama süzgeci anlamsız; puan tek başına karar verir.
    assert q.coverage("spiderman", "Vjeran Tomic: The Spider-Man of Paris") == 1.0


def test_dilbilgisi_kelimeleri_kapsamayi_sulandirmaz():
    assert q.coverage("game of thrones", "Game of Thrones") == 1.0
    assert q.coverage("the dark knight", "Kara Şövalye - The Dark Knight") == 1.0
