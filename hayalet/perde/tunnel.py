"""Yerel sunucuyu internete açan SSH ters tüneli.

Neden SSH: cloudflared Android'de çalışmıyor — statik Go binary'si kendi DNS
çözümleyicisini kullanıyor, Android'de `/etc/resolv.conf` olmadığı için
`[::1]:53`'e düşüp başarısız oluyor (ölçüldü, root'suz kaçışı yok).
paramiko ise Python; soketleri Bionic'in çözümleyicisini kullanıyor, sorun
doğmuyor. Üstelik hesap/kurulum/dağıtım gerektirmiyor ve dönen adres
**HTTPS** — iPhone Safari kamera/mikrofon için bunu şart koşuyor.

Perde'nin Node sürümü bunu `ssh -R 80:localhost:PORT nokey@localhost.run`
komutuyla yapıyordu; burada aynı iş kütüphaneyle yapılıyor (Android'de ssh
komutu yok).

Tek uca güvenmiyoruz — bunlar ölçülen gerçekler (2026-09-22, Türkiye,
mobil veri):

* Sağlayıcılar tek tek ölüyor. `localhost.run:22` TCP'de açık ve banner
  veriyor ama **kimlik doğrulamada reddediyor**; `serveo.net`,
  `a.pinggy.io`, `bore.pub` TCP'yi kabul edip tek bayt yazmadan kapatıyor
  (yani listeye eklemek boşuna). Bu yüzden uçlar **paralel yarıştırılır**,
  ilk adres veren kazanır ve her ucun hatası ayrı yazılır — "tünel
  kurulamadı" demek teşhis için yetmiyordu.
* **Sıralı deneme ölçülerek elendi:** telefonda oda 1.2 sn'de kalkarken
  davet linki 23.8 sn sonra geliyordu; sürenin neredeyse tamamını ölü uç
  yiyordu. Paralelde süre = en hızlı ucun süresi.
* **Kimlik doğrulama yöntemi de sırayla denenir, anahtar tipi tek başına
  yetmiyor.** Telefonda ölçüldü: localhost.run hem RSA-2048 hem ed25519
  anahtarını `Authentication failed` ile reddetti — yani sorun anahtarın
  tipi değil, anahtar **sunmanın kendisi**. Kullanıcı adı zaten `nokey`:
  sunucu anahtarsız (`none`) doğrulama bekliyor. Bu yüzden denenecek
  yöntemler uç başına listelenir ve reddedilirse sunucunun **istediği
  yöntemler** (`allowed_types`) hataya yazılır — teşhisi tahmine
  bırakmamak için.
* Anahtar gereken uçlar için: paramiko 5.0.0'da `Ed25519Key.generate` YOK;
  anahtarı `cryptography` ile üretip OpenSSH biçiminde geri okumak gerekiyor
  (bkz. `_make_key`). ed25519'un srv.us'ta doğrulandığı ölçüldü.
* **`request_port_forward` cevap gelmezse sonsuza kadar bekler** (paramiko
  `global_request(wait=True)` içinde döner). srv.us'ta gerçekten yaşandı:
  iş parçacığı asılı kaldı. Bu yüzden istek `_call_with_timeout` ile
  sınırlanıyor.

Adres uçtan uca değişebildiği için (tünel düşüp yeniden kurulunca yeni
adres gelir) çağıran `on_url` ile haberdar edilir; `start()`in dönüş değeri
tek başına yeterli değil.
"""
from __future__ import annotations

import io
import re
import socket
import threading
import time
from dataclasses import dataclass


class TunnelError(Exception):
    pass


@dataclass(frozen=True)
class Endpoint:
    """Bir tünel sağlayıcısı: nereye bağlanılır, hangi portu ters açar."""

    name: str
    host: str
    port: int
    user: str
    bind_port: int
    # Doğrulama yöntemleri sırayla denenir: "none" (anahtarsız), "ed25519",
    # "rsa". İlki reddedilirse sıradaki denenir.
    auths: tuple[str, ...] = ("none", "ed25519", "rsa")
    # Genel adres oturum kanalına düz metin olarak yazılıyor, oradan okunur.
    url_re: str = r"https://[\w.-]+\.(?:lhr\.life|localhost\.run)"
    # srv.us, önce oturum kanalı açılmadan gönderilen yönlendirme isteğine
    # hiç cevap vermiyor (ölçüldü: 12 sn cevapsız); oturum önce açılınca en
    # azından açık bir cevap ("denied") dönüyor.
    session_first: bool = False
    # Uzak dinleme adresi (OpenSSH `-R 80:...` yazınca "localhost" gönderir).
    bind_addr: str = ""
    # localhost.run oturum kanalındaki isteklere (pty/shell/exec) HİÇ cevap
    # vermiyor ama adresi yine de kanala yazıyor. OpenSSH `shell`i gönderip
    # cevabı beklemeden okumaya geçtiği için orada çalışıyor; paramiko ise
    # cevabı bekleyip takılıyordu (ölçüldü 2026-09-24: none doğrulama ve
    # yönlendirme kabul, sonra 14 sn cevapsız). False → istek cevap
    # beklenmeden, pty'siz gönderilir.
    shell_reply: bool = True


# Hepsi AYNI ANDA denenir, sıra yalnızca hata listesinin okunuşunu etkiler.
# Yeni bir sağlayıcı eklemek bu listeye bir satır; gerisi (yöntem denemesi,
# zaman aşımı, hata toplama, yarış) ortak.
#
# 2026-09-23 ölçümü: srv.us çalışıyor, localhost.run reddediyor. Yine de
# listede duruyor — bedeli artık yok (paralel) ve bir gün geri dönerse
# kendiliğinden kullanılır.
ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("srv.us", "srv.us", 22, "hayalet", 1,
             auths=("ed25519",), url_re=r"https://[\w.-]+\.srv\.us",
             session_first=True),
    # 2026-09-24: `none` + cevapsız shell ile yeniden çalışıyor (2,3 sn).
    Endpoint("localhost.run", "localhost.run", 22, "nokey", 80,
             bind_addr="localhost", shell_reply=False),
)

# Tek bir uca ayrılan süreler. Ölü bir uç bütün açılışı geciktirmemeli:
# çalışan uçlar telefondan 1-3 sn içinde cevap veriyor (ölçüldü), 20 sn'lik
# eski pay üç yöntem × iki uç ile turu dakikalara çıkarıyordu.
_CONNECT_TIMEOUT = 12.0
_FORWARD_TIMEOUT = 12.0
_URL_TIMEOUT = 40.0
# Hepsi başarısız olursa kaç tur denenir (arada artan bekleme).
_PASSES = 3
# SSH soketinin gönderme/alma tamponu (bkz. _connect_once).
_SOCK_BUF = 4 * 1024 * 1024


def _make_key(paramiko, kind: str):
    """İstenen tipte tek kullanımlık anahtar üretir.

    ed25519 için paramiko'nun üreteci yok; `cryptography` ile üretip OpenSSH
    özel anahtar biçiminde geri okuyoruz (paramiko yalnız bu biçimi ayrıştırır,
    PKCS8 PEM'i değil).
    """
    if kind == "ed25519":
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        raw = ed25519.Ed25519PrivateKey.generate()
        pem = raw.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode()
        return paramiko.Ed25519Key.from_private_key(io.StringIO(pem))
    if kind == "ecdsa":
        return paramiko.ECDSAKey.generate()
    if kind == "rsa":
        return paramiko.RSAKey.generate(2048)
    raise ValueError(kind)


def _call_with_timeout(fn, timeout: float):
    """`fn`i ayrı iş parçacığında çağırır; süre dolarsa TunnelError atar.

    paramiko'nun hem global istekleri hem kanal açma/kabuk çağırma işleri
    cevap gelmediğinde sonsuza kadar bekliyor (ikisi de yaşandı); kendi
    süremizi dayatmanın başka yolu yok. `fn`in dönüş değeri aynen döner.
    """
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["ok"] = fn()
        except BaseException as e:  # noqa: BLE001 - aynen taşınacak
            box["err"] = e

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise TunnelError("istek cevapsız kaldı (%.0f sn)" % timeout)
    if "err" in box:
        raise box["err"]
    return box.get("ok")


class SshTunnel:
    """Yerel `local_port`'u dışarıya açar; `url` genel adresi taşır."""

    def __init__(self, local_port: int, on_url=None, endpoints=None):
        self.local_port = local_port
        self.url: str | None = None
        self._on_url = on_url
        self._endpoints = tuple(endpoints or ENDPOINTS)
        self._client = None
        self._transport = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None
        self._errors: list[str] = []

    # --- dış yüz ---------------------------------------------------------
    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def errors(self) -> list[str]:
        """Son turda her ucun kendi hatası — teşhis bunun üzerinden yapılıyor."""
        return list(self._errors)

    def start(self, timeout: float = 45.0) -> str:
        """Tüneli kurar ve genel adresi döndürür. Kurulamazsa TunnelError.

        Zaman aşımı yalnız **beklemeyi** bitirir: arka plandaki iş parçacığı
        denemeye devam eder ve adres sonradan gelirse `on_url` ile bildirilir.
        Oda ekranı tünelin gecikmesi yüzünden açılmadan bekleyemez.
        """
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.url:
                return self.url
            if not self._thread.is_alive():
                break
            time.sleep(0.25)
        raise TunnelError(self._last_error or "tünel adresi alınamadı")

    def stop(self) -> None:
        self._stop.set()
        self.url = None
        try:
            if self._transport is not None:
                self._transport.close()
        except Exception:
            pass
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass
        self._client = self._transport = None

    # --- iç işleyiş -------------------------------------------------------
    def _run(self) -> None:
        try:
            import paramiko
        except Exception as e:
            self._last_error = f"paramiko yok: {e}"
            return

        wait = 4.0
        for turn in range(_PASSES):
            if self._stop.is_set():
                return
            served, errors = self._one_pass(paramiko)
            self.url = None
            if self._stop.is_set():
                return
            if served:
                # Tünel kurulmuş ve düşmüş: baştan dene, adres DEĞİŞİR — bu
                # yüzden çağıran on_url ile yeni linki öğrenmeli.
                self._last_error = None
                self._errors = []
                wait = 4.0
            else:
                self._errors = errors
                self._last_error = "; ".join(errors) or "tünel adresi alınamadı"
            if turn == _PASSES - 1:
                return
            time.sleep(wait)
            wait = min(wait * 3, 60.0)

    def _one_pass(self, paramiko) -> tuple[bool, list[str]]:
        """Uçları PARALEL yarıştırır; ilk adres veren kazanır.

        Sıralı deneme ölçüldü ve pahalıydı: telefonda oda 1.2 sn'de
        kalkarken davet linki **23.8 sn** sonra geliyordu. Sürenin tamamına
        yakınını ölü uç yiyordu — localhost.run'ın anahtarsız denemesi 12 sn
        cevapsız kalıyor, iki anahtarlı denemesi reddediliyor (üstelik
        RSA-2048 üretimi telefonda saniyeler sürüyor) ve çalışan uç ancak
        ondan sonra sıraya geliyordu. Paralelde süre = en hızlı ucun süresi;
        ölü sağlayıcı kimseyi bekletmiyor.

        Bir uç içindeki doğrulama yöntemleri yine SIRAYLA denenir (aynı
        sunucuya aynı anda üç kez bağlanmak gereksiz).

        Hatalar tur sonunu BEKLEMEDEN yazılır: ekranda sebebin görünmesi
        için tur bitmesini beklemek gerekmiyordu.
        """
        errors: list[str] = []
        self._errors = errors
        kazanan = threading.Event()
        kilit = threading.Lock()

        def uc_dene(ep: Endpoint) -> None:
            for kind in ep.auths:
                if kazanan.is_set() or self._stop.is_set():
                    return
                try:
                    if self._connect_once(paramiko, ep, kind, kazanan, kilit):
                        return          # bu uç kazandı ve bağlantısı düştü
                except Exception as e:
                    with kilit:
                        errors.append(
                            f"{ep.name}({kind}): {type(e).__name__}: {e}")
                        self._last_error = "; ".join(errors)

        isler = [threading.Thread(target=uc_dene, args=(ep,), daemon=True)
                 for ep in self._endpoints]
        for t in isler:
            t.start()
        # Kaybedenler `kazanan` görünce kendiliğinden çıkıyor; kazanan ise
        # bağlantı düşene kadar burada bekliyor.
        for t in isler:
            t.join()
        return kazanan.is_set(), errors

    def _connect_once(self, paramiko, ep: Endpoint, kind: str,
                      kazanan, kilit) -> bool:
        """Tek uca bağlanır, adres gelirse bağlantı düşene kadar bekler.

        Uçlar paralel yarıştığı için adres `kilit` altında yayımlanıyor:
        ikinci gelen uç kendini kapatıp çekiliyor, tek bir kazanan kalıyor.

        Dönüş: bu uç gerçekten adres verdi mi (yani sağlayıcı çalışıyor mu).
        """
        # SSHClient yerine doğrudan Transport: anahtarsız (`none`) doğrulama
        # ve isteklerin sırası ancak burada denetlenebiliyor.
        sock = socket.create_connection((ep.host, ep.port),
                                        timeout=_CONNECT_TIMEOUT)
        # Video trafiğinin tamamı bu soketten geçiyor. Ölçüldü (2026-09-24,
        # Windows, srv.us, 5 MB): varsayılan tamponla 234 KB/s, 4 MB ile
        # 951 KB/s (OpenSSH aynı uçta 870 KB/s). Pencere/paket boyutu ve
        # şifre türünün belirgin etkisi olmadı. Android'de çekirdek sınırı
        # buna izin veriyor (tablet: wmem_max 8 MB, tcp_wmem üst 16 MB).
        for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
            try:
                sock.setsockopt(socket.SOL_SOCKET, opt, _SOCK_BUF)
            except OSError:
                pass
        transport = paramiko.Transport(sock)
        transport.banner_timeout = _CONNECT_TIMEOUT
        transport.auth_timeout = _CONNECT_TIMEOUT
        client = transport                      # stop() ikisini de kapatıyor
        got_url = False
        try:
            transport.start_client(timeout=_CONNECT_TIMEOUT)
            transport.set_keepalive(30)
            # DİKKAT: `self._transport` ancak KAZANINCA yazılıyor; yarışta
            # kaybeden uç kazananın bağlantısının üstüne yazarsa stop()
            # yanlış bağlantıyı kapatır.
            try:
                if kind == "none":
                    transport.auth_none(ep.user)
                else:
                    transport.auth_publickey(ep.user,
                                             _make_key(paramiko, kind))
            except paramiko.BadAuthenticationType as e:
                # Sunucunun istediği yöntemleri hataya yaz: hangi yöntemi
                # ekleyeceğimizi tahmin etmek zorunda kalmamak için.
                raise TunnelError("sunucu %s istiyor"
                                  % ",".join(e.allowed_types or ["?"])) from e

            url_re = re.compile(ep.url_re)
            chan = None
            if ep.session_first:
                chan = _call_with_timeout(
                    lambda: self._shell(transport, ep.shell_reply),
                    _CONNECT_TIMEOUT)

            # Gelen her bağlantıyı yerel porta bağla.
            _call_with_timeout(
                lambda: transport.request_port_forward(
                    ep.bind_addr, ep.bind_port, handler=self._on_channel),
                _FORWARD_TIMEOUT)

            # Genel adres oturum kanalına yazılıyor; oradan okuyoruz.
            if chan is None:
                chan = _call_with_timeout(
                    lambda: self._shell(transport, ep.shell_reply),
                    _CONNECT_TIMEOUT)
            buf = ""
            deadline = time.time() + _URL_TIMEOUT
            while not self._stop.is_set():
                if not got_url and kazanan.is_set():
                    raise TunnelError("başka uç önce yanıtladı")
                if not got_url and time.time() > deadline:
                    raise TunnelError(
                        "adres %.0f sn içinde gelmedi; kanaldan gelen: %r"
                        % (_URL_TIMEOUT, buf[-200:]))
                try:
                    data = chan.recv(4096)
                    if not data:
                        break
                    buf += data.decode("utf-8", "replace")
                    m = url_re.search(buf)
                    if m and not got_url:
                        with kilit:
                            if kazanan.is_set():
                                # Başka uç önce cevap verdi: bu bağlantıyı
                                # bırak, iki tünel açık tutmanın anlamı yok.
                                raise TunnelError("başka uç önce yanıtladı")
                            kazanan.set()
                            got_url = True
                            self.url = m.group(0).rstrip("/")
                            self._client = self._transport = transport
                        if self._on_url:
                            try:
                                self._on_url(self.url)
                            except Exception:
                                pass
                    buf = buf[-4000:]
                except socket.timeout:
                    if not transport.is_active():
                        break
                except Exception:
                    break
            if not got_url:
                if kazanan.is_set():
                    raise TunnelError("başka uç önce yanıtladı")
                raise TunnelError("kanal adres vermeden kapandı; gelen: %r"
                                  % buf[-200:])
            return True
        finally:
            if not got_url:
                # Çalışmayan ucu arkada bırakma: sıradaki uç denenecek.
                try:
                    client.close()
                except Exception:
                    pass
                if self._client is client:
                    self._client = self._transport = None

    @staticmethod
    def _shell(transport, reply: bool = True):
        """Adresin yazıldığı oturum kanalını açar (pty şart: bazı uçlar
        adresi yalnız etkileşimli kabukta yazıyor).

        `timeout` ŞART: paramiko'nun varsayılanı sonsuz beklemek ve tam
        burada asılı kaldı (ölçüldü: doğrulama geçtikten sonra tünel iş
        parçacığı 85+ sn hiçbir hata yazmadan sustu — hata listesi boş,
        ekranda "kuruluyor…" takılı).
        """
        chan = transport.open_session(timeout=_CONNECT_TIMEOUT)
        chan.settimeout(_CONNECT_TIMEOUT)
        if not reply:
            # `shell` isteği want_reply=False ile: paramiko'nun invoke_shell'i
            # cevabı zorunlu bekliyor (bkz. Endpoint.shell_reply).
            from paramiko.common import cMSG_CHANNEL_REQUEST
            from paramiko.message import Message
            m = Message()
            m.add_byte(cMSG_CHANNEL_REQUEST)
            m.add_int(chan.remote_chanid)
            m.add_string("shell")
            m.add_boolean(False)
            transport._send_user_message(m)
            chan.settimeout(1.0)
            return chan
        chan.get_pty()
        chan.invoke_shell()
        chan.settimeout(1.0)
        return chan

    def _on_channel(self, channel, src_addr, dest_addr) -> None:
        """Dışarıdan gelen bağlantıyı yerel sunucuya köprüler."""
        threading.Thread(target=self._pump, args=(channel,), daemon=True).start()

    def _pump(self, channel) -> None:
        sock = None
        try:
            sock = socket.create_connection(("127.0.0.1", self.local_port), timeout=15)
            sock.settimeout(None)
            channel.settimeout(None)
            a = threading.Thread(target=_copy, args=(channel, sock), daemon=True)
            b = threading.Thread(target=_copy, args=(sock, channel), daemon=True)
            a.start()
            b.start()
            a.join()
            b.join()
        except Exception:
            pass
        finally:
            for c in (channel, sock):
                try:
                    if c is not None:
                        c.close()
                except Exception:
                    pass


def _copy(src, dst) -> None:
    """Tek yönde veri taşır. Segmentler MB'larca, bu yüzden parça parça."""
    try:
        while True:
            data = src.recv(32768)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass
