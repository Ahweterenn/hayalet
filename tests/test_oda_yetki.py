"""Oda yetkileri, öneriler, sohbet kimliği, katalog — ağsız.

Olay işleyicileri sahte bir socket.io ile doğrudan çağrılıyor; sunucu ya da
tarayıcı gerekmiyor.
"""
import pytest

from hayalet.oda import rooms as R
from hayalet.oda.catalog import Catalog, CatalogError, Resolved


class _Sio:
    def __init__(self):
        self.handlers, self.emits, self._sess = {}, [], {}
        self.disconnected = []

    def on(self, ev):
        def dec(f):
            self.handlers[ev] = f
            return f
        return dec

    def event(self, f):
        self.handlers[f.__name__.replace("_", "-")] = f
        return f

    def get_environ(self, sid): return {}
    def enter_room(self, sid, room): pass
    def save_session(self, sid, d): self._sess[sid] = d
    def get_session(self, sid): return self._sess.get(sid, {})
    def emit(self, ev, data=None, **kw): self.emits.append((ev, data, kw))
    def disconnect(self, sid): self.disconnected.append(sid)
    # arka plan işi testte hemen çalışsın
    def start_background_task(self, fn, *a): fn(*a)

    def sent(self, ev, to=None):
        return [d for e, d, kw in self.emits if e == ev and (to is None or kw.get("to") == to)]


class _Katalog:
    def __init__(self):
        self.resolved = []

    def search(self, q): return [{"ref": "S1", "title": q}]
    def detail(self, ref): return {"ref": ref, "seasons": []}

    def resolve(self, ref):
        self.resolved.append(ref)
        return Resolved(url=f"https://cdn/{ref}.m3u8", referer="https://oynatici/",
                        user_agent="UA", impersonate="chrome",
                        now={"kind": "hayalet", "ref": ref, "title": "Dizi", "hasNext": True})

    def next_of(self, ref): return ref + "+1"


def _oda(catalog=None, identity=None):
    from hayalet.oda import events
    sio, store = _Sio(), R.RoomStore()
    events.register(sio, store, "ANAHTAR", catalog=catalog, on_identity=identity)
    katil = sio.handlers["join-room"]
    katil("ev", {"roomId": "O", "username": "Ev", "hostToken": "ANAHTAR", "color": "#EA5814"})
    katil("misafir", {"roomId": "O", "username": "Misafir", "color": "kırmızı; background:url(x)"})
    sio.emits.clear()
    return sio, store.get("O")


# --- kumanda --------------------------------------------------------------------
def test_kumanda_herkesteyken_misafir_durdurabilir():
    sio, oda = _oda()
    sio.handlers["pause"]("misafir", {"roomId": "O", "currentTime": 12.0})
    assert sio.sent("pause"), "herkese yayılmalıydı"
    assert oda.current_time == 12.0


def test_kumanda_ev_sahibindeyken_misafirin_hareketi_geri_cevrilir():
    sio, oda = _oda()
    oda.current_time, oda.is_playing = 30.0, True
    sio.handlers["room-settings"]("ev", {"roomId": "O", "controlMode": "host"})
    sio.emits.clear()
    sio.handlers["seek"]("misafir", {"roomId": "O", "currentTime": 999.0})
    assert not sio.sent("seek"), "yetkisiz sarma yayılmamalı"
    assert oda.current_time == 30.0, "oda saati değişmemeli"
    red = sio.sent("control-denied", to="misafir")
    assert red and red[0]["currentTime"] == 30.0 and red[0]["isPlaying"] is True


def test_kumanda_modunu_yalniz_ev_sahibi_degistirir():
    sio, oda = _oda()
    sio.handlers["room-settings"]("misafir", {"roomId": "O", "controlMode": "host"})
    assert oda.control_mode == "all"
    sio.handlers["room-settings"]("ev", {"roomId": "O", "controlMode": "saçma"})
    assert oda.control_mode == "all"


# --- öneriler ----------------------------------------------------------------------
def test_misafirin_linki_odayi_degistirmez_oneri_olur():
    sio, oda = _oda()
    sio.handlers["set-video"]("misafir", {"roomId": "O", "videoUrl": "https://youtu.be/abcdefghijk"})
    assert oda.video_url == "", "misafir doğrudan içerik koyamamalı"
    assert len(oda.suggestions) == 1 and oda.suggestions[0]["by"] == "Misafir"
    assert sio.sent("suggestions", to="ev"), "öneri ev sahibine gitmeli"
    assert not sio.sent("suggestions", to="misafir"), "misafir öneri listesini görmemeli"


def test_ayni_oneri_iki_kez_eklenmez():
    sio, oda = _oda()
    for _ in range(3):
        sio.handlers["set-video"]("misafir", {"roomId": "O", "videoUrl": "https://ornek/v.m3u8"})
    assert len(oda.suggestions) == 1


def test_ev_sahibi_linki_dogrudan_acar():
    sio, oda = _oda()
    sio.handlers["set-video"]("ev", {"roomId": "O", "videoUrl": "https://youtu.be/abcdefghijk"})
    assert oda.video_url.startswith("https://youtu.be/")
    assert oda.now["kind"] == "youtube"
    assert sio.sent("video-changed")


def test_oneri_kabul_edilince_katalogdan_acilir():
    kat, kimlik = _Katalog(), []
    sio, oda = _oda(catalog=kat, identity=kimlik.append)
    sio.handlers["catalog-play"]("misafir", {"roomId": "O", "ref": "B7", "title": "Dizi"})
    assert kat.resolved == [], "misafirin seçimi çözülmemeli, öneri olmalı"
    sid = oda.suggestions[0]["id"]
    sio.handlers["suggestion-accept"]("misafir", {"roomId": "O", "id": sid})
    assert oda.suggestions, "misafir kabul edemez"
    sio.handlers["suggestion-accept"]("ev", {"roomId": "O", "id": sid})
    assert kat.resolved == ["B7"]
    assert oda.video_url == "https://cdn/B7.m3u8"
    assert oda.headers["referer"] == "https://oynatici/"
    assert kimlik and kimlik[0].impersonate == "chrome", "proxy kimliği taşınmalı"
    assert not oda.suggestions


def test_sonraki_bolumu_yalniz_ev_sahibi_acar():
    kat = _Katalog()
    sio, oda = _oda(catalog=kat)
    oda.now = {"ref": "B1"}
    sio.handlers["next-episode"]("misafir", {"roomId": "O"})
    assert kat.resolved == []
    sio.handlers["next-episode"]("ev", {"roomId": "O"})
    assert kat.resolved == ["B1+1"]


def test_katalog_hatasi_yalniz_isteyene_gider_ve_oda_bozulmaz():
    class Bozuk(_Katalog):
        def resolve(self, ref): raise RuntimeError("kaynak ölü")
    sio, oda = _oda(catalog=Bozuk())
    sio.handlers["catalog-play"]("ev", {"roomId": "O", "ref": "X"})
    assert oda.video_url == ""
    hata = sio.sent("catalog-error", to="ev")
    assert hata and "kaynak ölü" in hata[0]["message"]
    assert sio.sent("content-loading")[-1] == {"loading": False}, "yükleniyor göstergesi kapanmalı"


# --- sohbet ve kişiler ------------------------------------------------------------------
def test_sohbette_ad_istemciden_degil_sunucudan_gelir():
    sio, _ = _oda()
    sio.handlers["chat-message"]("misafir", {"roomId": "O", "username": "Ev", "message": " selam "})
    m = sio.sent("chat-message")[0]
    assert m["username"] == "Misafir", "başkasının adıyla yazılamamalı"
    assert m["message"] == "selam" and m["id"] == "misafir"


def test_bos_ve_cok_uzun_mesaj():
    sio, _ = _oda()
    sio.handlers["chat-message"]("misafir", {"roomId": "O", "message": "   "})
    assert not sio.sent("chat-message")
    sio.handlers["chat-message"]("misafir", {"roomId": "O", "message": "a" * 5000})
    assert len(sio.sent("chat-message")[0]["message"]) == 1000


def test_renk_yalniz_hex_kabul_edilir():
    _, oda = _oda()
    kisiler = {p["name"]: p for p in oda.people()}
    assert kisiler["Ev"]["color"] == "#EA5814"
    assert kisiler["Misafir"]["color"] == "", "CSS'e gidecek değer süzülmeli"
    assert "ip" not in kisiler["Ev"], "kişi listesi iç bilgi taşımamalı"
    assert kisiler["Ev"]["leader"] and not kisiler["Misafir"]["leader"]


def test_kisiyi_yalniz_ev_sahibi_cikarir():
    sio, oda = _oda()
    sio.handlers["kick-user"]("misafir", {"roomId": "O", "id": "ev"})
    assert len(oda.users) == 2
    sio.handlers["kick-user"]("ev", {"roomId": "O", "id": "ev"})
    assert len(oda.users) == 2, "kendini çıkaramaz"
    sio.handlers["kick-user"]("ev", {"roomId": "O", "id": "misafir"})
    assert [u.username for u in oda.users] == ["Ev"]
    assert sio.disconnected == ["misafir"]
    assert oda.is_banned("Misafir", "")


# --- katalog ref güvenliği ----------------------------------------------------------------
def test_katalog_yalniz_kendi_urettigi_refi_tanir():
    kat = Catalog(contexts=lambda: {}, context_for=lambda s: (None, None))
    with pytest.raises(CatalogError):
        kat.resolve("https://kotu.site/elle-yazilmis")
    with pytest.raises(CatalogError):
        kat.detail("uydurma")
    assert kat.next_of("uydurma") is None


def test_sohbet_gecmisi_sonradan_katilana_gider_ve_sinirli():
    sio, oda = _oda()
    for i in range(60):
        sio.handlers["chat-message"]("misafir", {"roomId": "O", "message": f"m{i}"})
    sio.emits.clear()
    sio.handlers["join-room"]("yeni", {"roomId": "O", "username": "Yeni"})
    durum = sio.sent("room-state", to="yeni")[0]
    assert len(durum["chat"]) == 50 and durum["chat"][-1]["message"] == "m59"
    sio.handlers["admin-command"]("ev", {"roomId": "O", "command": "clearall"})
    assert oda.chat == [], "temizlenen sohbet yeni gelene gitmemeli"


def test_uygulamadan_gelen_bolum_acilir_hata_ev_sahibine_gider():
    from hayalet.oda import events
    kat = _Katalog()
    sio, store = _Sio(), R.RoomStore()
    ops = events.register(sio, store, "ANAHTAR", catalog=kat)
    sio.handlers["join-room"]("ev", {"roomId": "O", "username": "Ev", "hostToken": "ANAHTAR"})
    sio.handlers["join-room"]("misafir", {"roomId": "O", "username": "Misafir"})
    ops["play"]("O", "B3")
    assert store.get("O").video_url == "https://cdn/B3.m3u8"

    class Bozuk(_Katalog):
        def resolve(self, ref): raise RuntimeError("ölü")
    sio2, store2 = _Sio(), R.RoomStore()
    ops2 = events.register(sio2, store2, "ANAHTAR", catalog=Bozuk())
    sio2.handlers["join-room"]("ev", {"roomId": "O", "username": "Ev", "hostToken": "ANAHTAR"})
    sio2.handlers["join-room"]("misafir", {"roomId": "O", "username": "Misafir"})
    ops2["play"]("O", "X")
    hedefler = [kw.get("to") for e, d, kw in sio2.emits if e == "catalog-error"]
    assert hedefler == ["ev"], "hata herkese değil yalnız ev sahibine gitmeli"


def test_katalog_uygulamanin_bolum_listesini_kaydeder():
    from hayalet.core.models import Episode, Series
    kat = Catalog(contexts=lambda: {}, context_for=lambda s: (None, None))
    dizi = Series(name="D", slug="d", site="x")
    eps = [Episode(1, i, f"u{i}") for i in (1, 2, 3)]
    refs = kat.episode_refs(dizi, eps)
    assert len(refs) == 3 and len(set(refs)) == 3
    nxt = kat.next_of(refs[0])
    assert nxt and kat._get(nxt, "episode")[1].number == 2
    assert kat.next_of(refs[2]) is None, "son bölümden sonrası yok"
