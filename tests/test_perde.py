"""Perde'nin saf mantığı — ağsız.

Buradakiler "bizim kodumuz bozuldu mu" sorusunu cevaplar; sitenin/CDN'in hâlâ
öyle davrandığını doğrulamaz (onun için canlı deneme gerekir).
"""
import threading

import pytest

from hayalet.perde import guard, manifest
from hayalet.perde import rooms as R
from hayalet.perde import tunnel as T


# --- oda / lider --------------------------------------------------------
def test_ev_sahibi_lider_olur_ip_ayni_olsa_bile():
    """Röle arkasında herkes aynı IP'den gelir; lider yine de ev sahibi olmalı.
    Eski IP tahmini tam burada yanlış kişiyi seçiyordu."""
    room = R.Room(id="X")
    room.add_user("s1", "Misafir", "10.0.0.1")
    room.add_user("s2", "Ahmet", "10.0.0.1", is_host=True)
    room.add_user("s3", "Baska", "10.0.0.1")
    assert R.compute_leader(room).username == "Ahmet"


def test_ev_sahibi_yoksa_en_erken_katilan_lider():
    room = R.Room(id="X")
    a = room.add_user("s1", "Ilk", "1.1.1.1")
    a.joined_at = 100.0
    b = room.add_user("s2", "Sonra", "2.2.2.2")
    b.joined_at = 200.0
    assert R.compute_leader(room).username == "Ilk"


def test_bos_odada_lider_yok():
    assert R.compute_leader(R.Room(id="X")) is None


def test_yeniden_baglanma_kullaniciyi_iki_kez_eklemez():
    room = R.Room(id="X")
    room.add_user("s1", "Ahmet", "1.1.1.1")
    room.add_user("s1", "Ahmet", "1.1.1.1")
    assert room.usernames() == ["Ahmet"]


def test_yasak_ad_ve_ip_uzerinden_calisir():
    room = R.Room(id="X")
    u = room.add_user("s1", "Kotu", "9.9.9.9")
    room.ban(u)
    assert room.is_banned("kotu", "1.1.1.1")
    assert room.is_banned("baskasi", "9.9.9.9")


def test_ayni_ipden_iki_kisi_varsa_ip_yasaklanmaz():
    """Röle arkasında IP yasağı masum izleyicileri de keserdi."""
    room = R.Room(id="X")
    room.add_user("s1", "Kotu", "10.0.0.1")
    room.add_user("s2", "Masum", "10.0.0.1")
    room.ban(room.find_user("s1"))
    assert room.is_banned("kotu", "0.0.0.0")
    assert not room.is_banned("masum", "10.0.0.1")


def test_buffering_yalniz_ilk_ve_son_degisimde_duyurulur():
    room = R.Room(id="X")
    assert room.start_buffering("a") is True      # oda beklemeye girdi
    assert room.start_buffering("b") is False     # zaten bekliyordu
    assert room.start_buffering("a") is False     # tekrar
    assert room.end_buffering("a") is False       # b hâlâ bekliyor
    assert room.end_buffering("b") is True        # oda devam edebilir


# --- altyazi normalizasyonu ---------------------------------------------
def test_altyazi_iki_bicimi_de_kabul_eder():
    subs = R.normalize_subtitles(["http://a/x.vtt",
                                  {"url": "http://b/y.vtt", "label": "TR"}])
    assert [s.url for s in subs] == ["http://a/x.vtt", "http://b/y.vtt"]
    assert subs[1].label == "TR"


def test_bozuk_altyazi_kayitlari_atilir():
    assert R.normalize_subtitles([None, {}, {"label": "TR"}, ""]) == []


def test_altyazi_esitligi():
    a = R.normalize_subtitles([{"url": "u", "label": "TR"}])
    b = R.normalize_subtitles([{"url": "u", "label": "TR"}])
    c = R.normalize_subtitles([{"url": "u", "label": "EN"}])
    assert R.subtitles_equal(a, b)
    assert not R.subtitles_equal(a, c)


# --- SSRF suzgeci --------------------------------------------------------
@pytest.mark.parametrize("host", [
    "localhost", "x.localhost", "127.0.0.1", "10.0.0.5", "192.168.1.1",
    "172.16.0.1", "169.254.1.1", "::1", "[::1]", "::ffff:127.0.0.1",
    "0.0.0.0", "fd00::1",
])
def test_ozel_adresler_engellenir(host):
    assert guard.is_blocked_host(host) is True


@pytest.mark.parametrize("host", ["1.1.1.1", "example.com", "8.8.8.8"])
def test_genel_adresler_gecer(host):
    assert guard.is_blocked_host(host) is False


def test_donguesel_proxy_istegi_reddedilir():
    with pytest.raises(guard.BlockedTarget):
        guard.check_target("https://baska.site/api/proxy?url=x")


def test_http_disi_semalar_reddedilir():
    with pytest.raises(guard.BlockedTarget):
        guard.check_target("file:///etc/passwd")


def test_yerel_hedef_reddedilir():
    with pytest.raises(guard.BlockedTarget):
        guard.check_target("http://127.0.0.1:8080/gizli")


# --- manifest yeniden yazma ---------------------------------------------
MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
360/index.m3u8
#EXT-X-MEDIA:TYPE=AUDIO,URI="audio/tr.m3u8",NAME="TR"
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1920x1080
https://cdn.baska/1080/index.m3u8
"""


def _build(base="https://cdn.ornek/hls/master.m3u8"):
    return manifest.make_proxy_url_builder(base, "https://ref.site/", "ODA1")


def test_goreceli_ve_mutlak_adresler_proxyye_yonlendirilir():
    out = manifest.rewrite_body(MASTER, _build())
    assert "/api/proxy?url=https%3A%2F%2Fcdn.ornek%2Fhls%2F360%2Findex.m3u8" in out
    assert "/api/proxy?url=https%3A%2F%2Fcdn.baska%2F1080%2Findex.m3u8" in out


def test_uri_nitelikleri_de_yeniden_yazilir():
    out = manifest.rewrite_body(MASTER, _build())
    assert 'URI="/api/proxy?url=' in out


def test_yorum_satirlari_bozulmaz():
    out = manifest.rewrite_body(MASTER, _build())
    assert "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360" in out
    assert out.startswith("#EXTM3U")


def test_referer_ve_oda_kimligi_tasinir():
    out = manifest.rewrite_body(MASTER, _build())
    assert "ref=https%3A%2F%2Fref.site%2F" in out
    assert "roomId=ODA1" in out


def test_master_query_parametreleri_alt_adreslere_tasinir():
    """Bazı CDN'ler yetki belirtecini yalnız master'ın query'sinde veriyor;
    taşımazsak alt isteklere 403 geliyor."""
    build = manifest.make_proxy_url_builder(
        "https://cdn.ornek/master.m3u8?token=ABC", None, "R")
    out = build("seg1.ts")
    assert "token%3DABC" in out


def test_var_olan_parametrenin_ustune_yazilmaz():
    build = manifest.make_proxy_url_builder(
        "https://cdn.ornek/master.m3u8?token=ABC", None, "R")
    out = build("seg.ts?token=KENDI")
    assert "token%3DKENDI" in out
    assert "token%3DABC" not in out


def test_ttl_manifest_turune_gore():
    assert manifest.ttl_seconds(MASTER) == 300.0
    assert manifest.ttl_seconds("#EXTM3U\n#EXTINF:4\na.ts\n#EXT-X-ENDLIST") == 120.0
    assert manifest.ttl_seconds("#EXTM3U\n#EXTINF:4\na.ts") == 4.0


def test_bayat_kopya_sure_dolunca_da_durur():
    c = manifest.ManifestCache()
    c.put("k", "govde", ttl=-1)          # süresi geçmiş
    assert c.get("k") is None            # normal okuma vermez
    assert c.get_stale("k") == "govde"   # yedek olarak durur


def test_gizli_manifest_tanimir():
    assert manifest.looks_like_manifest("#EXTM3U\n#EXTINF:4,\na.ts")
    assert not manifest.looks_like_manifest("<html><body>hata</body></html>")


def test_srt_webvttye_cevrilir():
    out = manifest.to_webvtt("1\n00:00:01,500 --> 00:00:03,000\nmerhaba\n")
    assert out.startswith("WEBVTT")
    assert "00:00:01.500" in out


def test_zaten_webvtt_olan_bozulmaz():
    src = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nx\n"
    assert manifest.to_webvtt(src) == src


# --- tunel uc zinciri ----------------------------------------------------
def test_calisan_uc_listenin_basinda():
    """Uçlar paralel yarışıyor; sıra yalnız hata okunuşunu etkiliyor ama
    ölçülen çalışan uç (srv.us) başta dursun ki hata listesi de öyle okunsun."""
    assert T.ENDPOINTS[0].name == "srv.us"
    assert len(T.ENDPOINTS) >= 2, "tek uca bağlıyız, sağlayıcı ölürse çare yok"


def test_localhost_run_once_anahtarsiz_deniyor():
    """Ölçüldü (2026-09-22, telefonda): localhost.run hem RSA hem ed25519
    anahtarını reddetti — yani anahtar SUNMAK sorun. Kullanıcı adı `nokey`,
    sunucu anahtarsız doğrulama bekliyor."""
    lhr = [e for e in T.ENDPOINTS if e.name == "localhost.run"][0]
    assert lhr.auths[0] == "none"
    assert "ed25519" in lhr.auths, "anahtarlı yol yedek kalmalı"


def test_her_ucun_adres_deseni_kendi_alanini_tanir():
    """Adres oturum kanalından okunuyor; desen şaşarsa yanlış link paylaşılır."""
    import re
    srv = [e for e in T.ENDPOINTS if e.name == "srv.us"][0]
    lhr = [e for e in T.ENDPOINTS if e.name == "localhost.run"][0]
    assert re.search(lhr.url_re, "gelen satir: https://abc-12.lhr.life/ falan")
    assert not re.search(lhr.url_re, "https://abc.srv.us/")
    assert re.search(srv.url_re, "https://xy9.srv.us/")


def test_cevapsiz_istek_zaman_asimina_dusuyor():
    """paramiko'nun global isteği cevap gelmezse sonsuza kadar bekliyor
    (srv.us'ta yaşandı, iş parçacığı asılı kaldı)."""
    import time as _t
    with pytest.raises(T.TunnelError):
        T._call_with_timeout(lambda: _t.sleep(5), 0.2)


def test_istegin_kendi_hatasi_aynen_tasinir():
    with pytest.raises(ZeroDivisionError):
        T._call_with_timeout(lambda: 1 / 0, 5)


def test_istegin_donus_degeri_geri_gelir():
    """Kanal açma da bu sarmalayıcıdan geçiyor; değeri kaybedersek kanal yok."""
    assert T._call_with_timeout(lambda: "kanal", 5) == "kanal"


def _sahte_tunel(sonuc):
    """`_connect_once`u ağa gitmeden taklit eder; denenen sırayı kaydeder."""
    t = T.SshTunnel(8477)
    denenen = []
    kilit_denenen = threading.Lock()

    def fake(paramiko, ep, kind, kazanan, kilit):
        with kilit_denenen:
            denenen.append((ep.name, kind))
        return sonuc(ep, kind, kazanan, kilit)

    t._connect_once = fake
    return t, denenen


def test_butun_uclar_ve_yontemler_denenir_hatalar_toplanir():
    """Hiçbiri tutmazsa: her uç × her yöntem denenmeli ve her birinin kendi
    hatası yazılmalı — "tünel kurulamadı" teşhise yetmiyordu."""
    def hep_patla(ep, kind, kazanan, kilit):
        raise RuntimeError("reddedildi")

    t, denenen = _sahte_tunel(hep_patla)
    oldu, hatalar = t._one_pass(None)
    assert not oldu
    beklenen = {(e.name, k) for e in T.ENDPOINTS for k in e.auths}
    assert set(denenen) == beklenen
    # Bir ucun KENDI yöntemleri sırayla denenmeli (aynı sunucuya aynı anda
    # üç bağlantı açmanın anlamı yok).
    lhr = [k for (ad, k) in denenen if ad == "localhost.run"]
    assert lhr == list([e for e in T.ENDPOINTS if e.name == "localhost.run"][0].auths)
    assert len(hatalar) == len(beklenen)
    assert all("reddedildi" in h for h in hatalar)


def test_bir_uc_kazaninca_otekinin_kalan_yontemleri_denenmez():
    """Yarışın kazananı belli olunca kaybeden uç kendi sıradaki yöntemlerini
    denemeyi bırakmalı; yoksa ölü sağlayıcı yine saniyeler yiyor."""
    kazandi = threading.Event()

    def sonuc(ep, kind, kazanan, kilit):
        if ep.name == "srv.us":
            kazanan.set()               # gerçek koddaki gibi: adres geldi
            kazandi.set()
            return True
        # localhost.run: ilk yöntem kazanan belli olana kadar oyalanıyor
        if kind == "none":
            kazandi.wait(5)
        raise RuntimeError("reddedildi")

    t, denenen = _sahte_tunel(sonuc)
    oldu, hatalar = t._one_pass(None)
    assert oldu
    assert ("srv.us", "ed25519") in denenen
    assert ("localhost.run", "ed25519") not in denenen
    assert ("localhost.run", "rsa") not in denenen


# --- proxy: akis modunda gelen playlist ---------------------------------
class _SahteYanit:
    """curl-cffi'nin `stream=True` yanıtını taklit eder: `text` BOŞTUR."""

    def __init__(self, govde=b"", ctype="application/vnd.apple.mpegurl",
                 status=200):
        self._govde = govde
        self.status_code = status
        self.headers = {"content-type": ctype}

    @property
    def text(self):          # akışta gövde henüz okunmamıştır
        return ""

    def iter_content(self, n=None):
        if self._govde:
            yield self._govde

    def close(self):
        pass


class _SahteOturum:
    def __init__(self, yanit):
        self.yanit = yanit
        self.istekler = []

    def get(self, url, **kw):
        self.istekler.append((url, kw))
        return self.yanit


def _proxy_cagir(monkeypatch, yanit, target, ctx=None):
    from urllib.parse import urlencode
    from hayalet.perde import guard, proxy_api

    monkeypatch.setattr(proxy_api, "_session", lambda c: _SahteOturum(yanit))
    monkeypatch.setattr(guard, "check_target", lambda url: "203.0.113.5")
    ctx = ctx or proxy_api.ProxyContext()
    env = {"REQUEST_METHOD": "GET",
           "QUERY_STRING": urlencode({"url": target, "roomId": "ODA",
                                      "ref": "https://sn.ornek.site/iframe.php"})}
    status, headers, out = proxy_api.handle(env, ctx, lambda rid: None)
    return status, dict(headers), b"".join(out), ctx


_PLAYLIST = (b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:3\n"
             b"#EXTINF:3.0,\nhttps://cdn.ornek/seg1.ts\n")


def test_akis_modunda_alinan_playlist_bos_donmemeli(monkeypatch):
    """Adreste `.m3u8` yoksa istek akış modunda yapılıyor; content-type
    mpegurl gelince kod `resp.text` okuyordu ve o BOŞTU → 200 + 0 bayt.
    Telefonda "odaya gönderilen video 0:00'da kalıyor" bunun belirtisiydi."""
    status, hdr, body, _ = _proxy_cagir(
        monkeypatch, _SahteYanit(_PLAYLIST),
        "https://sn.ornek.site/l.php?v=abc")       # .m3u8 YOK -> akış modu
    assert status.startswith("200")
    assert b"#EXTM3U" in body
    assert b"/api/proxy?url=" in body, "segment adresleri bize yönlendirilmeli"


def test_bos_playlist_onbellege_alinmaz(monkeypatch):
    """Boş gövde önbelleğe girerse geçici aksaklık KALICI takılmaya dönüşür:
    sonraki istekler de boş cevabı yer."""
    from hayalet.perde import proxy_api

    ctx = proxy_api.ProxyContext()
    target = "https://sn.ornek.site/l.php?v=abc"
    status, hdr, body, ctx = _proxy_cagir(monkeypatch, _SahteYanit(b""),
                                          target, ctx)
    assert not status.startswith("200"), "boş playlist 200 ile dönmemeli"
    # İkinci istek dolu gövde alırsa düzgün cevap vermeli (önbellek zehirli değil).
    status2, hdr2, body2, _ = _proxy_cagir(monkeypatch, _SahteYanit(_PLAYLIST),
                                           target, ctx)
    assert status2.startswith("200")
    assert b"#EXTM3U" in body2


def test_akissiz_playlist_yolu_bozulmadi(monkeypatch):
    """`.m3u8` ile gelen normal yol (stream=False, `resp.text` dolu)."""
    class _Dolu(_SahteYanit):
        @property
        def text(self):
            return self._govde.decode()

    status, hdr, body, _ = _proxy_cagir(
        monkeypatch, _Dolu(_PLAYLIST), "https://sn.ornek.site/master.m3u8?v=1")
    assert status.startswith("200")
    assert b"/api/proxy?url=" in body


# --- proxy: segment onbellegi + tek ucus --------------------------------
class _SayanOturum:
    """Aynı yanıtı döner ama KAÇ kez yukarı akışa gidildiğini sayar."""

    def __init__(self, govde, gecikme=0.0, ctype="video/mp2t"):
        self.govde = govde
        self.gecikme = gecikme
        self.ctype = ctype
        self.sayac = 0
        self._kilit = threading.Lock()

    def get(self, url, **kw):
        with self._kilit:
            self.sayac += 1
        if self.gecikme:
            import time as _t
            _t.sleep(self.gecikme)
        return _SahteYanit(self.govde, ctype=self.ctype)


def _segment_istegi(monkeypatch, oturum, ctx, url, menzil=None):
    from urllib.parse import urlencode
    from hayalet.perde import guard, proxy_api

    monkeypatch.setattr(proxy_api, "_session", lambda c: oturum)
    monkeypatch.setattr(guard, "check_target", lambda u: "203.0.113.5")
    env = {"REQUEST_METHOD": "GET",
           "QUERY_STRING": urlencode({"url": url, "roomId": "ODA"})}
    if menzil:
        env["HTTP_RANGE"] = menzil
    status, headers, out = proxy_api.handle(env, ctx, lambda rid: None)
    return status, b"".join(out)


_SEGMENT = b"\x47" + b"video-baytlari" * 50


def test_ayni_segment_ikinci_kez_yukari_akisa_gitmez(monkeypatch):
    """Odadaki her izleyici aynı segmenti istiyor; eskiden her istek ayrı bir
    indirme başlatıyordu (üç izleyici = aynı 1 MB'ın üç kez inmesi)."""
    from hayalet.perde import proxy_api

    ctx = proxy_api.ProxyContext()
    oturum = _SayanOturum(_SEGMENT)
    url = "https://cdn.ornek/seg1.ts"
    s1, b1 = _segment_istegi(monkeypatch, oturum, ctx, url)
    s2, b2 = _segment_istegi(monkeypatch, oturum, ctx, url)
    assert s1.startswith("200") and s2.startswith("200")
    assert b1 == b2 == _SEGMENT
    assert oturum.sayac == 1, "ikinci istek önbellekten gelmeliydi"


def test_es_zamanli_iki_istek_tek_indirme_yapar(monkeypatch):
    """Senkron oda: ikinci izleyicinin isteği birincisi HÂLÂ inerken gelir.
    Tek uçuş olmazsa iki indirme birden başlar."""
    from hayalet.perde import proxy_api

    ctx = proxy_api.ProxyContext()
    oturum = _SayanOturum(_SEGMENT, gecikme=0.4)
    url = "https://cdn.ornek/seg2.ts"
    sonuc = {}

    def calis(ad):
        sonuc[ad] = _segment_istegi(monkeypatch, oturum, ctx, url)

    a = threading.Thread(target=calis, args=("a",))
    b = threading.Thread(target=calis, args=("b",))
    a.start(); b.start(); a.join(20); b.join(20)
    assert sonuc["a"][1] == sonuc["b"][1] == _SEGMENT
    assert oturum.sayac == 1, "eş zamanlı istekler iki indirme başlatmamalı"


def test_menzilli_istek_onbellege_takilmaz(monkeypatch):
    """Range'li istekte gövde parça parça; önbelleğe alırsak yanlış baytları
    servis ederiz."""
    from hayalet.perde import proxy_api

    ctx = proxy_api.ProxyContext()
    oturum = _SayanOturum(_SEGMENT)
    url = "https://cdn.ornek/seg3.ts"
    _segment_istegi(monkeypatch, oturum, ctx, url, menzil="bytes=0-99")
    _segment_istegi(monkeypatch, oturum, ctx, url, menzil="bytes=0-99")
    assert oturum.sayac == 2, "menzilli istekler önbellekten servis edilmemeli"


def test_onbellek_bellegi_sinirli():
    """Telefonda sınırsız segment biriktirmek uygulamayı şişirir."""
    from hayalet.perde.manifest import SegmentCache

    c = SegmentCache(max_bytes=1000, max_item=400)
    c.put("a", b"x" * 400, "video/mp2t")
    c.put("b", b"y" * 400, "video/mp2t")
    c.put("c", b"z" * 400, "video/mp2t")       # yer açmak için "a" düşmeli
    assert c.size <= 1000
    assert c.get("a") is None
    assert c.get("c")[0] == b"z" * 400
    c.put("d", b"q" * 900, "video/mp2t")       # parça sınırını aşıyor
    assert c.get("d") is None


def test_octet_stream_segment_de_onbellege_girer(monkeypatch):
    """Bazı kaynaklar segmenti `application/octet-stream` ile veriyor; o yanıt
    "gizli manifest" dalına düşüyor ve orası önbelleği atlıyordu — odadaki her
    izleyici yine ayrı indirme yapıyordu (telefonda ölçüldü)."""
    from hayalet.perde import proxy_api

    ctx = proxy_api.ProxyContext()
    oturum = _SayanOturum(_SEGMENT, ctype="application/octet-stream")
    url = "https://cdn.ornek/seg-octet.bin"
    s1, b1 = _segment_istegi(monkeypatch, oturum, ctx, url)
    s2, b2 = _segment_istegi(monkeypatch, oturum, ctx, url)
    assert b1 == b2 == _SEGMENT
    assert oturum.sayac == 1, "ikinci istek önbellekten gelmeliydi"


# --- davet -> oda kimligi tasiniyor mu ----------------------------------
def test_davet_sayfasi_oda_kimligini_tasir():
    """Katıl düğmesi `room`u düşürürse room.js sabit 'PERDE' odasına düşüyor:
    linkten girenler o hayalet odada buluşuyor, ev sahibi kendi odasında
    yalnız kalıyor (sohbet geçmiyor, odaya gönderilen video görünmüyor).
    Telefonda ölçüldü — sunucuda iki oda birden vardı."""
    from pathlib import Path

    kaynak = (Path(__file__).resolve().parents[1]
              / "hayalet" / "perde" / "public" / "invite.html").read_text(encoding="utf-8")
    assert "'/room.html?username='" not in kaynak, "oda kimliği yine düşürülüyor"
    assert "get('room')" in kaynak and "set('room'" in kaynak


def test_odasiz_room_html_varsayilan_odaya_yonlendirilir():
    """Misafirin tarayıcısında ESKİ davet sayfası önbellekte kalabilir; sunucu
    da kendi odasına almalı ki kimse 'PERDE'ye düşmesin."""
    socketio = pytest.importorskip("socketio")          # perde ekstrası
    from hayalet.perde import server as psrv

    srv = psrv.PerdeServer(port=0, room_id="ABC123")
    yakalanan = {}

    def start_response(status, headers):
        yakalanan["status"] = status
        yakalanan["headers"] = dict(headers)

    srv._wsgi({"PATH_INFO": "/room.html", "QUERY_STRING": "username=Ali",
               "REQUEST_METHOD": "GET"}, start_response)
    assert yakalanan["status"].startswith("302")
    yer = yakalanan["headers"]["Location"]
    assert "room=ABC123" in yer and "username=Ali" in yer

    # Oda kimliği zaten varsa dokunma.
    yakalanan.clear()
    srv._wsgi({"PATH_INFO": "/room.html", "QUERY_STRING": "room=XYZ789",
               "REQUEST_METHOD": "GET"}, start_response)
    assert not yakalanan["status"].startswith("302")


# --- liderlik: ev sahibi anahtari ---------------------------------------
class _SahteSio:
    """events.register'ın ihtiyacı kadar socket.io taklidi (ağsız)."""

    def __init__(self, environ=None):
        self.handlers = {}
        self._environ = environ or {}
        self.emits = []
        self._sessions = {}

    def on(self, ev):
        def dec(f):
            self.handlers[ev] = f
            return f
        return dec

    def event(self, f):
        self.handlers[f.__name__.replace("_", "-")] = f
        return f

    def get_environ(self, sid):
        return self._environ

    def enter_room(self, sid, room):
        pass

    def save_session(self, sid, d):
        self._sessions[sid] = d

    def get_session(self, sid):
        return self._sessions.get(sid, {})

    def emit(self, ev, data=None, **kw):
        self.emits.append((ev, data, kw))

    def disconnect(self, sid):
        pass


def _oda_kur(token="GIZLI", environ=None):
    from hayalet.perde import events
    store = R.RoomStore()
    sio = _SahteSio(environ)
    events.register(sio, store, token)
    return sio, store


def test_ev_sahibi_sonradan_girse_de_lider_olur():
    """Anahtar yalnız sayfanın adresindeydi, socket'e hiç gitmiyordu; bu yüzden
    `is_host` hiç doğru olmuyor ve liderlik "ilk giren"e düşüyordu. Telefonda
    ölçüldü: odayı açan telefon değil, sonradan giren PC sekmesi lider oldu —
    ev sahibi ne senkron oynatabiliyor ne /seek kullanabiliyordu."""
    sio, store = _oda_kur()
    katil = sio.handlers["join-room"]
    katil("misafir", {"roomId": "ODA", "username": "Misafir"})
    katil("evsahibi", {"roomId": "ODA", "username": "Ev sahibi",
                       "hostToken": "GIZLI"})
    lider = R.compute_leader(store.get("ODA"))
    assert lider.username == "Ev sahibi"


def test_yanlis_anahtar_ev_sahibi_yapmaz():
    sio, store = _oda_kur()
    katil = sio.handlers["join-room"]
    katil("misafir", {"roomId": "ODA", "username": "Misafir"})
    katil("sahtekar", {"roomId": "ODA", "username": "Sahtekar",
                       "hostToken": "YANLIS"})
    assert R.compute_leader(store.get("ODA")).username == "Misafir"


def test_anahtar_el_sikismasindan_da_okunur():
    """Eski yol (socket sorgu dizesi) da çalışmaya devam etmeli."""
    sio, store = _oda_kur(environ={"QUERY_STRING": "EIO=4&hostToken=GIZLI"})
    katil = sio.handlers["join-room"]
    katil("misafir", {"roomId": "ODA", "username": "Misafir"})
    lider = R.compute_leader(store.get("ODA"))
    assert lider.username == "Misafir" and lider.is_host


def test_sikistirilmis_yanitta_uzunluk_iletilmez(monkeypatch):
    """curl-cffi gövdeyi açarak veriyor ama `content-length` sıkıştırılmış
    boyutu söylüyor; olduğu gibi iletilince istemci yanıtı kırpıyor
    (ölçüldü: 940 baytlık JSON 515 bayt olarak ulaştı, bozuk geldi)."""
    from hayalet.perde import proxy_api

    class _Gzipli(_SahteYanit):
        def __init__(self, govde):
            super().__init__(govde, ctype="application/json")
            self.headers = {"content-type": "application/json",
                            "content-encoding": "gzip",
                            "content-length": "551"}

    ctx = proxy_api.ProxyContext()
    oturum = _SayanOturum(b"x" * 940)
    oturum.yanit_sinifi = None
    govde = b'{"veri": "' + b"a" * 900 + b'"}'

    monkeypatch.setattr(proxy_api, "_session", lambda c: type(
        "O", (), {"get": lambda self, url, **kw: _Gzipli(govde)})())
    from hayalet.perde import guard
    monkeypatch.setattr(guard, "check_target", lambda u: "203.0.113.5")
    from urllib.parse import urlencode
    env = {"REQUEST_METHOD": "GET",
           "QUERY_STRING": urlencode({"url": "https://cdn.ornek/veri.bin",
                                      "roomId": "ODA"})}
    status, headers, out = proxy_api.handle(env, ctx, lambda rid: None)
    basliklar = {k.lower(): v for k, v in headers}
    assert "content-length" not in basliklar, "sıkıştırılmış boyut iletilmemeli"
    assert b"".join(out) == govde
