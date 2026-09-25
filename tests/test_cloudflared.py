"""cloudflared hızlı tüneli — ağsız sınamalar.

Gerçek cloudflared yerine onun çıktısını taklit eden küçük bir Python süreci
çalıştırılıyor; API isteği ve DNS de sahte. Canlı doğrulama (tablet, gerçek
Cloudflare) bunların yerini tutmaz, yalnız bizim kodumuzun kırılıp
kırılmadığını söyler.
"""
import json
import socket
import sys
import textwrap
import time

import pytest

from hayalet.oda import cloudflared as C

GERCEK_YANIT = json.dumps({
    "success": True,
    "result": {"id": "9dc8c40b-8f72-407a-b3de-fc5b077942a4",
               "name": "qt-abc", "hostname": "ornek-dort-kelime.trycloudflare.com",
               "account_tag": "5ab4e9dfbd435d24068829fda0077963",
               "secret": "c2lyLWtpbWxpay1iaWxnaXNpLTMyLWJheXQtdXp1bmx1Z3U="},
    "errors": []})


def _qt():
    return C.parse_quick_response(GERCEK_YANIT)


def _sahte_cozucu(tablo):
    def resolver(host, port, family, type_):
        if host not in tablo:
            raise socket.gaierror(8, "çözülemedi")
        return [(family, type_, 6, "", (ip, port)) for ip in tablo[host]]
    return resolver


# --- saf yardımcılar ---------------------------------------------------------

def test_yanit_cozulur_ve_kimlik_dosyasi_bicimi_dogru():
    qt = _qt()
    assert qt.url == "https://ornek-dort-kelime.trycloudflare.com"
    assert qt.credentials() == {
        "AccountTag": "5ab4e9dfbd435d24068829fda0077963",
        "TunnelSecret": "c2lyLWtpbWxpay1iaWxnaXNpLTMyLWJheXQtdXp1bmx1Z3U=",
        "TunnelID": "9dc8c40b-8f72-407a-b3de-fc5b077942a4"}


@pytest.mark.parametrize("govde", [
    "<html>bakım</html>",
    json.dumps({"success": False, "errors": [{"code": 1000}]}),
    json.dumps({"success": True, "result": {"id": "x"}}),
    json.dumps({"success": True, "result": {"id": "", "hostname": "h",
                                            "account_tag": "a", "secret": "s"}}),
])
def test_bozuk_yanit_acik_hata_verir(govde):
    with pytest.raises(C.CloudflaredError):
        C.parse_quick_response(govde)


def test_sunucular_iki_bolgeden_ipv4_olarak_cozulur():
    edges = C.resolve_edges(_sahte_cozucu({
        "region1.v2.argotunnel.com": ["198.41.192.7", "198.41.192.27", "198.41.192.37"],
        "region2.v2.argotunnel.com": ["198.41.200.13", "198.41.200.23"]}))
    assert len(edges) == 4
    assert all(e.endswith(":7844") for e in edges)
    assert sum(e.startswith("198.41.192.") for e in edges) == 2
    assert sum(e.startswith("198.41.200.") for e in edges) == 2


def test_bir_bolge_cozulemezse_otekiyle_devam_edilir():
    edges = C.resolve_edges(_sahte_cozucu({"region2.v2.argotunnel.com": ["198.41.200.13"]}))
    assert edges == ["198.41.200.13:7844"]


def test_hicbir_sunucu_cozulemezse_hata():
    with pytest.raises(C.CloudflaredError, match="çözülemedi"):
        C.resolve_edges(_sahte_cozucu({}))


def test_komut_dns_gerektirmeyen_bayraklari_tasir():
    cmd = C.build_command("/x/libcloudflared.so", "/c/k.json",
                          ["1.2.3.4:7844", "5.6.7.8:7844"], 8477, "TID")
    assert cmd[0] == "/x/libcloudflared.so"
    assert cmd[-2:] == ["run", "TID"]
    assert cmd.count("--edge") == 2 and "1.2.3.4:7844" in cmd
    assert "--no-autoupdate" in cmd
    assert cmd[cmd.index("--edge-ip-version") + 1] == "4"
    assert cmd[cmd.index("--url") + 1] == "http://127.0.0.1:8477"
    assert cmd[cmd.index("--credentials-file") + 1] == "/c/k.json"


# --- süreç yönetimi (sahte cloudflared) --------------------------------------

def _sahte_ikili(tmp_path, govde):
    betik = tmp_path / "sahte_cloudflared.py"
    betik.write_text(textwrap.dedent(govde), encoding="utf-8")
    return (sys.executable, "-u", str(betik))


KAYITLI_VE_BEKLEYEN = """
    import sys, time, json
    args = sys.argv[1:]
    kimlik = json.load(open(args[args.index("--credentials-file") + 1]))
    assert kimlik["TunnelID"] == args[-1]
    print("2026-09-24T15:07:23Z INF Starting tunnel tunnelID=" + args[-1])
    print("2026-09-24T15:07:23Z INF Registered tunnel connection connIndex=0 location=ist03")
    sys.stdout.flush()
    time.sleep(60)
"""

HEMEN_OLEN = """
    import sys
    print("2026-09-24T15:07:23Z ERR Unable to establish connection error=\\"dial tcp: refused\\"")
    sys.exit(1)
"""


def _tunel(tmp_path, govde, **kw):
    return C.CloudflaredTunnel(8477, binary=_sahte_ikili(tmp_path, govde),
                               work_dir=tmp_path / "is", request_fn=_qt,
                               resolver=_sahte_cozucu({
                                   "region1.v2.argotunnel.com": ["198.41.192.7"]}),
                               **kw)


def test_baglanti_kurulunca_adres_doner_ve_bildirilir(tmp_path):
    gelen = []
    t = _tunel(tmp_path, KAYITLI_VE_BEKLEYEN, on_url=gelen.append)
    try:
        url = t.start(timeout=20)
        assert url == "https://ornek-dort-kelime.trycloudflare.com"
        assert gelen == [url]
        assert t.last_error is None
    finally:
        t.stop()


def test_durdurunca_surec_olur_ve_gizli_kimlik_silinir(tmp_path):
    t = _tunel(tmp_path, KAYITLI_VE_BEKLEYEN)
    t.start(timeout=20)
    proc = t._proc
    kimlik = t._creds_path
    assert kimlik is not None and kimlik.exists()
    t.stop()
    assert proc.poll() is not None, "cloudflared süreci arkada kaldı"
    assert not kimlik.exists(), "gizli anahtar diskte kaldı"
    assert t.url is None


def test_kurulamazsa_hata_metni_cloudflared_satirini_tasir(tmp_path):
    t = _tunel(tmp_path, HEMEN_OLEN)
    try:
        with pytest.raises(C.CloudflaredError):
            t.start(timeout=3)
        assert "koduyla çıktı" in (t.last_error or "")
        assert "Unable to establish connection" in (t.last_error or "")
    finally:
        t.stop()


def test_ikili_yoksa_hemen_hata(tmp_path):
    t = C.CloudflaredTunnel(8477, binary="", request_fn=_qt)
    t._binary = None
    with pytest.raises(C.CloudflaredError, match="bulunamadı"):
        t.start(timeout=1)


def test_api_hatasi_gorunur_ve_arka_plan_yeniden_dener(tmp_path):
    cagri = []

    def bozuk_istek():
        cagri.append(time.time())
        raise C.CloudflaredError("hızlı tünel istenemedi: ağ yok")

    t = C.CloudflaredTunnel(8477, binary=_sahte_ikili(tmp_path, KAYITLI_VE_BEKLEYEN),
                            work_dir=tmp_path, request_fn=bozuk_istek)
    try:
        with pytest.raises(C.CloudflaredError, match="ağ yok"):
            t.start(timeout=1)
        assert t.errors == ["hızlı tünel istenemedi: ağ yok"]
    finally:
        t.stop()
    assert len(cagri) >= 1
