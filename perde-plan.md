# Perde'yi telefonda çalıştırma planı

Perde (`github.com/Ahweterenn/Perde`) şu an PC'de Node.js ile çalışıyor.
Amaç: aynı sunucuyu Python'a taşıyıp hayalet APK'sının içinde çalıştırmak,
böylece odayı açan kişi PC olmadan, telefonundan birlikte izleme başlatabilsin.

Bu plan yazılmadan önce aşağıdaki şeyler **ölçüldü**, tahmin edilmedi.
Ölçüm sonuçları "Faz 0" başlığında.

---

## Faz 0 — Yapılabilirlik (BİTTİ, ölçüldü)

### Ö1. python-socketio, gerçek socket.io 4.x istemcisiyle konuşuyor mu? → EVET

Perde'nin `package.json`'ı `socket.io ^4.6.1` kullanıyor. Masaüstünde
`python-socketio 5.17` sunucusu + `socket.io-client 4.8.3` istemcisi ile
Perde'nin **gerçek olay adları** (`join-room`, `room-state`, `play`,
`chat-message`, `room-users`) denendi:

| Taşıma | Sonuç |
|---|---|
| `websocket` | GEÇTİ (11 ms'de bağlandı) |
| `polling` | GEÇTİ (26 ms'de bağlandı) |

`socket.to(room)` semantiği de doğrulandı: `play` olayını gönderen istemciye
geri dönmüyor (python-socketio'da karşılığı `skip_sid=sid`).

### Ö2. Hangi WSGI sunucusu? → werkzeug, wsgiref DEĞİL

İlk deneme `wsgiref.simple_server` ile yapıldı: **polling çalıştı, WebSocket
çöktü**. Sebep, `wsgiref`'in `environ`'da ham soketi vermemesi; engine.io
`_upgrade_websocket` içinde patlıyor. `werkzeug.serving.run_simple(...,
threaded=True)` ile ikisi de geçti. Werkzeug saf Python.

### Ö3. Gerekli paketler Chaquopy'ye girer mi? → EVET, ama yedi değil DOKUZ

İlk sayım eksikti. Bağımlılık kapanışı makineyle çıkarıldı:

```
python-socketio  -> bidict, python-engineio
python-engineio  -> simple-websocket
simple-websocket -> wsproto
wsproto          -> h11
werkzeug         -> markupsafe        <-- ilk listede YOKTU
bidict           -> typing-extensions <-- ilk listede YOKTU (py<3.10 koşullu)
```

> **Tuzak:** `app/build.gradle` içindeki pip bloğu `options "--no-deps", "--pre"`
> kullanıyor. Yani **her bağımlılık tek tek yazılmalı**; biri eksikse APK
> sorunsuz derlenir ve hata ancak telefonda `ImportError` olarak çıkar.
> `markupsafe` saf Python değil, ama Chaquopy deposunda arm64 yapısı **var**
> (kuruldu, doğrulandı).

**APK'ya gerçek maliyeti ölçüldü: 36,9 MB → 37,8 MB, yani +0,9 MB.**

### Ö4. İstemci tarafı ne bekliyor?

- `room.html` → `/socket.io/socket.io.js` dosyasını **sunucudan** çekiyor.
  python-socketio bunu servis etmiyor. Perde'de hazır kopya var:
  `admin-extension/socket.io.min.js` (49 KB) — onu bu yola koyacağız.
- `hls.js` → jsdelivr CDN'inden geliyor. hayalet'in kendi kopyası var
  (`hayalet/assets/hls.min.js`), yerelden servis edip CDN bağımlılığını
  kaldırabiliriz.
- WebRTC → `room.js:2445` kendi STUN/TURN listesini taşıyor (Google STUN +
  openrelay TURN). Sunucu tarafında iş yok, sadece sinyal taşınıyor.
- iPhone → `room.js:1148` `Hls.isSupported()` false olunca `videoEl.src` ile
  **yerel HLS**'e düşüyor (satır 1290-1294). Yani iPhone'da oynar, ama aynı
  dalda `hideHlsControls()` çağrıldığı için kalite/ses/altyazı menüsü gizli.

### Ö5. cloudflared telefonda çalışır mı? → HAYIR (DNS ile tıkandı)

Binary telefona gönderilip çalıştırıldı:

```
cloudflared version 2026.9.1   ✔ çalışıyor (cihaz: arm64-v8a)
```

Ama tünel kurulamadı:

```
failed to request quick Tunnel: dial tcp: lookup api.trycloudflare.com
  on [::1]:53: read udp ...: connection refused
```

Bunun **ağ sorunu olmadığı** ayırt edici testle gösterildi:

| Test | Sonuç |
|---|---|
| `ping api.trycloudflare.com` (Bionic çözümleyici) | ✔ 104.16.230.132'ye çözdü |
| `ls /etc/resolv.conf` | ✗ yok |
| `id` | `uid=2000(shell)` — **root yok** |

Sebep: Android'de `/etc/resolv.conf` yoktur; DNS'i Bionic kütüphanesi yapar.
cloudflared saf Go ile statik derlenmiş (cgo yok), Go'nun kendi çözümleyicisi
`/etc/resolv.conf` bulamayınca `[::1]:53`'e düşüyor ve orada kimse dinlemiyor.

Kapalı olan kaçış yolları: `/etc/resolv.conf` yazmak (root ister),
127.0.0.1:53'e yerel DNS vekili koymak (1024 altı port, root ister),
Go'ya çözümleyici yolunu bildirmek (Go desteklemiyor).

> **Sonuç: cloudflared'ı gömme seçeneği (eski "A planı") root'suz cihazda
> ölü.** 37 MB'lık APK şişmesi zaten pahalıydı; artık çalışmadığı da ölçüldü.
> Öneri B seçeneğine kaydı (aşağıya bakın).

> **GÜNCELLEME (2026-09-24): bu sonuç YANLIŞ çıktı, cloudflared artık ana tünel.**
> cloudflared yalnız iki yerde DNS'e ihtiyaç duyuyor ve ikisi de dışarıdan
> karşılanabiliyor: hızlı tünel kaydını (`api.trycloudflare.com`) Python
> yapıyor, sunucu IP'lerini de Python çözüp `--edge` ile veriyor. Tablette
> root'suz çalıştı; uygulama içinden oda açılınca trycloudflare linki 3-6 sn'de
> geliyor. Ölçülen hız 0,97-1,41 MB/s (SSH tüneli aynı gün 0,23-0,29 MB/s). APK
> +11 MB. Ayrıntı: `hayalet/perde/cloudflared.py`; SSH tüneli yedek olarak kaldı.

### Ö6. curl-cffi iş parçacığı güvenliği → uyarı yeniden ÜRETİLEMEDİ

CLAUDE.md tek bir curl-cffi istemcisinin iş parçacığı güvenli olmadığını
söylüyor. Kırmaya çalışıldı, kırılmadı:

| Desen | Sonuç |
|---|---|
| Paylaşılan oturum, 32 parçacık × 5 tur (1 MB) | 160/160 başarılı |
| Paylaşılan oturum, **eş zamanlı akış** 10 parçacık × 2 MB, yavaş okuma | 30/30 başarılı |
| Parçacık başına oturum (aynı yükler) | 160/160 ve 30/30 |

Yine de parçacık başına oturum kullanılacak: maliyeti sıfır ve depo bu
endişeyi zaten belgelemiş. Ama "zorunlu" değil, **ucuz sigorta**.

Aynı koşumda werkzeug da ölçüldü: 32 eş zamanlı istekte hata yok, geri
dönüş medyanı 75-80 ms. Telefondaki 3-4 izleyici için fazlasıyla yeterli.

> Werkzeug her isteği stderr'e logluyor — telefonda **kapatılmalı**
> (`logging.getLogger("werkzeug").setLevel(ERROR)`), yoksa logcat boğuluyor.

### Ö7. Worker video trafiğini taşıyabilir mi? → EVET (ama Referer şart)

Asıl soru: bu CDN'ler TLS parmak izine mi bakıyor, yoksa sadece `Referer`'a mı?
Üç canlı sitede, gerçek akışlarla, curl-cffi ile **düz `requests`** karşılaştırıldı
(düz istek = Worker'ın yapabildiği şey):

| Site | master playlist | alt playlist | segment |
|---|---|---|---|
| dizipal | düz istek **kararsız** (bir koşumda 504) | düz ✔ | düz ✔ (3,4 MB, `image/jpeg`) |
| hdfilmcehennemi | düz ✔ | düz ✔ | düz ✔ (2,1 MB) |
| diziyou | düz ✔ | düz ✔ | düz ✔ |

**Referer olmadan aynı segmentler:**

| Site | Referersiz sonuç |
|---|---|
| dizipal | **403** |
| hdfilmcehennemi | **404** |
| diziyou | 200 (umursamıyor) |

Sonuç: baytların %99'unu taşıyan segmentler TLS taklidi istemiyor, yani
Worker taşıyabilir — **ama `Referer` başlığını iletmek zorunda.** Yalnız
Dizipal'in master playlist'i güvenilmez; onu telefon curl-cffi ile çözüp
yeniden yazar, segment adreslerini Worker'a yönlendirir.

Bu, ısınma/bant genişliği sorusunun ölçülmüş cevabıdır: **video telefona
hiç uğramayabilir.**

### Ö8. Yığın gerçekten telefonda kalkıyor mu? → EVET

`hayalet/perde/selftest.py` yazıldı, APK'ya girdi ve telefonda çalıştırıldı
(`--es perde 1` geliştirme kancası):

```json
{ "paketler": { hepsi "var" },
  "port": 8099, "yerel_ip": "192.168.137.67",
  "el_sikismasi": "0{\"sid\":\"...\",\"upgrades\":[\"websocket\"], ...}",
  "sonuc": "GECTI" }
```

Ardından **PC'den telefona** gerçek `socket.io-client` ile bağlanıldı:

| Yol | Sonuç |
|---|---|
| Düz HTTP `GET /` | 200 |
| socket.io **websocket** | bağlandı 291 ms, yankı 305 ms |
| socket.io **polling** | bağlandı 797 ms, yankı 1126 ms |

Yani "aynı Wi-Fi'da oda açma" (C seçeneği) bugün çalışır durumda.

---

## Mimari karar

**Perde sunucusu bu depoya `hayalet/perde/` olarak girer.**

Gerekçe: `hayalet/` klasörü Android projesine junction'lı, yani buraya yazmak
doğrudan APK'nın Python kaynağını yazmak demek. Ayrıca masaüstünde
`python -m hayalet --perde` ile telefon olmadan test edilebilir ve `tests/`
altındaki pytest düzenine girer.

`hayalet_app.py` (Android köprüsü, bu depoda değil) yalnızca ince bir
başlat/durdur/durum sarmalayıcısı alır — iş mantığı oraya taşınmaz.

### Ne taşınıyor, ne taşınmıyor

| Perde parçası | Karar |
|---|---|
| `server.js` oda/senkron/sohbet mantığı | Python'a taşınır |
| `server.js` `/api/proxy` | Python'a taşınır, **curl-cffi** ile |
| `public/*` (room.html, room.js, style.css, invite.html) | **Aynen kopyalanır**, minimum düzenleme |
| `admin-extension/` (Chrome eklentisi) | **Taşınmaz** — hayalet zaten m3u8'i kendi çıkarıyor |
| `scraper.js` + `/api/extract-video` (puppeteer) | **Taşınmaz** — yerine hayalet'in kendi çıkarıcıları |
| `native-host/` ("Sunucuyu Başlat" butonu) | **Taşınmaz** — Android'de anlamsız |
| `/api/shutdown` | **Taşınmaz** — telefonda servis bildiriminden durdurulur |
| `cloudflare-worker/` | Aynen kalır, opsiyonel (aşağıda "bant genişliği") |

### Asıl kazanç: proxy'yi curl-cffi'ye bağlamak

Perde'nin `/api/proxy`'si düz `http.get` kullanıyor. hayalet'in `Network`
sınıfı curl-cffi ile TLS parmak izi taklidi yapıyor — CLAUDE.md'de yazılı
olduğu gibi hem Dizipal hem hdfilmcehennemi düz istekte 403 veriyor.
Proxy'yi `Network` üzerinden kurarsak:

- Chrome eklentisine gerek kalmaz (linkler zaten hayalet'ten geliyor),
- eklentinin yakaladığı kısa ömürlü/tek kullanımlık linklerin süresi dolma
  derdi azalır (hayalet gerektiğinde yeniden çıkarabilir).

---

## Dosya dosya iş listesi

### Yeni: `hayalet/perde/` (bu depo)

| Dosya | İçerik | Test |
|---|---|---|
| `__init__.py` | `start(...)`, `stop()`, `status()` dış yüzü | — |
| `rooms.py` | Oda durumu, lider hesabı, ban listesi, altyazı karşılaştırma | pytest (saf) |
| `manifest.py` | m3u8 yeniden yazma, TTL, önbellek, `URI="..."` etiketleri | pytest (saf) |
| `guard.py` | SSRF koruması, engelli host, DNS sabitleme, kendine zincirlenme | pytest (saf) |
| `events.py` | socket.io olay işleyicileri | pytest (sahte sio) |
| `proxy_api.py` | `/api/proxy` — curl-cffi ile, akış (stream) tabanlı | canlı |
| `server.py` | WSGI uygulaması, yönlendirmeler, werkzeug sunucusu | canlı |
| `tunnel.py` | cloudflared başlatma + URL yakalama + yeniden bağlanma | canlı |
| `public/` | Perde'den kopya statik dosyalar | — |

### Değişecek: mevcut dosyalar

| Dosya | Değişiklik |
|---|---|
| `hayalet/cli.py` | `--perde` bayrağı (masaüstünde test için) |
| `app/build.gradle` | 7 yeni pip satırı + `versionCode` artışı |
| `AndroidManifest.xml` | `PerdeService` kaydı, `foregroundServiceType="dataSync"` |
| `hayalet_app.py` | `perde_start/stop/status/set_video` köprüsü |
| `MainActivity.java` | "Birlikte izle" girişi + oda ekranı (WebView) |
| yeni `PerdeService.java` | Ön plan servisi, wake/wifi kilidi (`DlService` deseni) |

---

## Socket.io olay sözleşmesi (birebir korunacak)

İstemciden sunucuya:

| Olay | Yük | Davranış |
|---|---|---|
| `join-room` | `{roomId, username}` | Ban kontrolü → odaya ekle → `room-state` (yalnız gönderene), `user-joined` (diğerlerine), `room-users` (herkese), `role-updated` |
| `set-video` | `{roomId, videoUrl, subtitles[], headers?, subHeaders?}` | Aynı video+altyazı ise **yok say** (oynatmayı sıfırlama), değilse `video-changed` |
| `play` / `pause` / `seek` | `{roomId, currentTime}` | Oda durumunu yaz → **gönderen hariç** yay |
| `sync-heartbeat` | `{roomId, currentTime, isPlaying}` | **Yalnız lider** kabul edilir, gönderen hariç yayılır |
| `buffering-start/end` | `{roomId, currentTime}` | İlk giren/son çıkan `room-buffering` yayar |
| `chat-message` | `{roomId, message, username}` | Herkese (gönderen dahil) |
| `admin-command` | `{roomId, command, args, username}` | Lider değilse reddet. `clearvideo`, `clearall`, `announce`, `kick` |
| `change-nick` | `{roomId, newName}` | `room-users` + sistem mesajı |
| `webrtc-*` (7 olay) | değişken | Odaya aynen ilet (sinyal taşıma) |
| `disconnect` | — | Kullanıcıyı çıkar, `user-left`, buffering listesinden düş |

Sunucudan istemciye: `room-state`, `role-updated`, `kicked`, `user-joined`,
`user-left`, `room-users`, `video-changed`, `play`, `pause`, `seek`,
`room-buffering`, `sync-heartbeat`, `chat-message`, `clear-chat`,
`system-message`, `tunnel-info`, `webrtc-*`.

### Liderlik: IP tahmini yerine sunucu anahtarı

Perde liderliği IP + kullanıcı adıyla tahmin ediyor (`computeLeader`).
**Tünel arkasında bu bozulur** — herkes tünelin IP'siyle gelir.
`getClientIp` `x-forwarded-for`'a bakıyor, o korunmalı; ama telefonda daha
sağlamı var:

> Sunucu açılışta rastgele bir `hostToken` üretir. Uygulamanın kendi WebView'ı
> bu anahtarla bağlanır ve lider olur. Tahmin yok, NAT derdi yok.

Lider ayrılırsa `computeLeader`'ın yedek sırası (IP+ad → IP → ad → ilk
kullanıcı) korunur.

---

## `/api/proxy` — taşınacak davranışlar

`server.js:380-751` küçük görünüp içinde çok sayıda yara bandı taşıyor.
Hiçbiri atlanmamalı; her biri gerçek bir kırılmadan doğmuş:

1. **SSRF koruması** — `http/https` dışını, loopback/özel/link-local hedefleri
   reddet. IPv4-mapped IPv6 (`::ffff:127.0.0.1`) dahil.
   *Telefonda daha kritik:* sunucu ev ağının içinde, korumasız bırakılırsa
   uzaktaki izleyici modeme istek attırabilir.
2. **DNS-rebinding koruması** — host adını çöz, IP'yi doğrula, bağlantıyı **o
   IP'ye sabitle**; `Host` başlığı ve TLS SNI orijinal alan adı kalsın.
3. **Kendine zincirlenme engeli** — `url=` başka bir `/api/proxy` adresini
   gösteriyorsa reddet (ucuz DoS vektörü).
4. **Manifest önbelleği** — master 5 dk, VOD 2 dk, canlı 4 sn.
5. **Bayat (stale) yedek** — upstream RESET verirse son iyi kopyayı sun.
   Dizipal/dplayer linkleri kısa ömürlü olduğu için bu sık devreye giriyor.
6. **Manifest yeniden yazma** — satır satır; `#` ile başlayanlarda yalnız
   `URI="..."` / `URI='...'`, diğerlerinde tüm satır. Master'ın query
   parametreleri alt URI'lara taşınır (`injectMasterParams`).
7. **Sahte resim segmentleri** — `.jpg/.png` uzantılı ama aslında video olan
   segmentler `video/mp2t` olarak geçirilir.
8. **Gizli manifest** — `text/plain`/`octet-stream` ile gelen gövde `#EXTM3U`
   ile başlıyorsa manifest muamelesi görür.
9. **Altyazı düzeltme** — `WEBVTT` başlığı yoksa eklenir, SRT zaman damgasındaki
   virgül noktaya çevrilir.
10. **`ld.php` → `l.php`** tek seferlik yeniden deneme (404'te).
11. **Yönlendirme takibi**, **`Range` geçirme**, **güvenlik başlıklarını
    temizleme** (`x-frame-options`, `content-security-policy`, HSTS).
12. **Manifest hatasını aynen iletme** — upstream 404/410 verdiyse HTML gövdesini
    m3u8 gibi göndermek yerine hatayı ilet, önbelleğe alma.

### Python'a özgü iki yeni kural

- **curl-cffi iş parçacığı güvenli değil** (CLAUDE.md'de resolver için yazılı).
  Her istek kendi `Network`'ünü kullanmalı → `threading.local()` ile iş
  parçacığı başına istemci, ya da küçük bir havuz.
- **Segmentler akışla geçmeli.** `Network._fetch` zaten `stream=True`
  destekliyor (`proxy.py:818`). Segment 1-3 MB; belleğe toplamak telefonda
  birkaç izleyicide şişer.

---

## Tünel — üç seçenek

Telefonun dışarıdan erişilebilir olması şart; ev ağının arkasında değil.

| Seçenek | Durum | Not |
|---|---|---|
| ~~**A. cloudflared'ı APK'ya gömmek**~~ | ❌ **ÖLÇÜLDÜ, ÇALIŞMIYOR** | Go çözümleyicisi Android'de DNS yapamıyor (Ö5). Root olmadan kaçışı yok |
| **B. Kendi röle Worker'ımız** | ✅ **birincil yol** | Telefon DIŞARI bağlanır → Python/Bionic DNS'i kullanır, Go sorunu hiç doğmaz. NAT derdi yok, 0 MB |
| **C. Aynı Wi-Fi, tünelsiz** | ✅ **bugün çalışıyor** (Ö8) | Uzaktaki arkadaş erişemez; **iPhone'da kamera/mikrofon çalışmaz** (Safari `getUserMedia` için HTTPS ister) |

**Öneri değişti: C ile başla, B'yi gerçek çözüm olarak kur.** A planı ölü.

### B seçeneği neden sağlam

cloudflared'ı tıkayan şey Go'nun kendi DNS çözümleyicisiydi. Python'da bu
sorun **yok**: Chaquopy'nin soketleri Bionic'in `getaddrinfo`'sunu kullanıyor
ve bu telefonda kanıtlandı — uygulama beş sitenin hepsini çözüp indiriyor.
Telefon **dışarı** bağlandığı için gelen bağlantı/NAT/port açma da gerekmiyor.

Kurgu:

```
telefon (Python)  --kalıcı WebSocket-->  Cloudflare Worker (Durable Object)
                                              ^
arkadaşın tarayıcısı --- https://... ---------+
```

Durable Objects **ücretsiz planda mevcut** (SQLite destekli; WebSocket
hibernation dahil). Ücretsiz sınırlar: 100.000 istek/gün, 5 GB depolama.
Sinyal trafiği kilobit mertebesinde, segmentler ayrı Worker proxy'sinden
gidecekse bile 2 saatlik film × 3 izleyici ≈ 3.600 istek — sınırın çok altında.

İş yükü: ~150 satır Python (röle istemcisi) + ~150 satır Worker JS.

> Worker proxy'si segmentlerde **`Referer` başlığını iletmek zorunda**
> (Ö7: referersiz Dizipal 403, hdfilmcehennemi 404 veriyor).

---

## Bant genişliği — asıl sınır bu

Video, sunucu olan telefondan geçer: telefon CDN'den indirir, izleyicilere
yükler. 1080p HLS ≈ 5 Mbps. İki izleyici = ~10 Mbps yükleme. Ev Wi-Fi'ında
sınırda, mobil veride zor.

**Kaçış yolu zaten istemcide var:** `room.html:31` `proxyBase` query
parametresini okuyor, `room.js:38` `/api/proxy`'yi onunla değiştiriyor.
`PERDE_PROXY_BASE` bir Cloudflare Worker'a bakarsa **video telefona hiç
uğramaz**; telefon yalnızca senkron/sohbet taşır (kilobit mertebesinde).

> **Doğrulanması gereken:** Worker'ın `fetch`'i bu CDN'lerden 403 yiyor mu?
> Worker, curl-cffi gibi TLS taklidi yapamaz. Segmentler genelde yalnız
> `Referer` kontrol eder, sayfalar ise parmak izi kontrol eder — yani muhtemelen
> çalışır, ama **ölçülmeden varsayılmamalı**.

---

## Android tarafı

### PerdeService.java (yeni)

`DlService` deseni birebir: ön plan servisi + `dataSync` tipi + kalıcı
bildirim ("Oda açık · 2 izleyici · Durdur"). `DlService.acquireLocks`'taki
wake/wifi kilidi aynen gerekli — telefon uykuya girerse sunucu ölür.

### Oda ekranı

Odaya açan kişi de odada olmalı (senkron için). Yani **ev sahibi de WebView
içindeki `room.html`'i kullanır**, ExoPlayer'ı değil.

> Sonuç: birlikte izlerken uygulamanın kendi oynatıcısının artıları
> (PiP, pinch-zoom, MediaSession, kaldığın yerden devam) **devrede olmaz**.
> Bu bir kayıp; kullanıcıya baştan söylenmeli.

WebView ayarları: `javaScriptEnabled`, `domStorageEnabled`,
`mediaPlaybackRequiresUserGesture=false`, `setWebChromeClient` +
`onPermissionRequest` (kamera/mikrofon için).

### "Odaya gönder" akışı

hayalet'in `build_stream(...)` çıktısı bir `MergedStream` — birden çok ses
kanalı olan **sentetik** master. Perde istemcisi tek bir `videoUrl` bekliyor.
Köprü: Perde sunucusuna `/v/<id>.m3u8` uç noktası eklenir
(`proxy.py:virtual()` ile aynı iş), alt URL'ler `/api/proxy?url=...` olarak
yazılır. `set-video` bu adresi ve Türkçe altyazıyı yollar.

---

## Güvenlik

- SSRF + DNS sabitleme **kaldırılamaz** (yukarıda 1-2).
- `/api/shutdown` taşınmıyor.
- Davet linki = odaya tam erişim. Perde'nin README'sindeki uyarı geçerli:
  tanımadığın kişiyle paylaşma. Oda kimliği rastgele üretilmeli
  (Perde'deki sabit `PERDE` değil).
- `escapeHtml` sistem mesajlarında korunmalı — sohbet mesajları kullanıcı
  adıyla birlikte HTML olarak basılıyor.
- `hostToken` loglanmamalı.

---

## Test düzeni

Depodaki mevcut ayrım korunur: saf mantık pytest'te, canlı doğrulama elle.

1. **pytest (ağsız):** `manifest.py` (gerçek m3u8 örnekleriyle yeniden yazma),
   `guard.py` (engelli host tablosu, IPv4-mapped IPv6, kendine zincirlenme),
   `rooms.py` (lider sırası, ban, altyazı eşitliği, yinelenen `set-video`).
2. **Masaüstü uçtan uca:** `python -m hayalet --perde` → tarayıcıdan
   `localhost:3000` → ikinci sekme ile senkron. Telefon hiç gerekmez.
   Node istemcisiyle yapılan Faz 0 denemesi (`sio_test_*.js/py`) regresyon
   testi olarak saklanabilir.
3. **Telefon:** önce aynı Wi-Fi (seçenek C), sonra tünel.
4. **Karşı cihazlar:** Android Chrome, masaüstü Chrome, **iPhone Safari**.
   iPhone'u ben test edemiyorum — yerel HLS dalı (`room.js:1290`) ve
   `getUserMedia`'nın HTTPS şartı orada doğrulanmalı.

> `room.js`/`room.html`'e dokunulursa `node --check` ile sözdizimi
> doğrulanmalı (CLAUDE.md'deki oynatıcı sayfası kuralıyla aynı gerekçe).

---

## Fazlar

| Faz | İş | Çıktı |
|---|---|---|
| **0** | Yapılabilirlik | **BİTTİ** — yukarıdaki ölçümler |
| **1** | ✅ **BİTTİ** — `rooms.py` + `events.py` + `server.py` + statik dosyalar | İki gerçek socket.io istemcisiyle senkron doğrulandı (9/9) |
| **2** | ✅ **BİTTİ** — `proxy_api.py` + `manifest.py` + `guard.py` (curl-cffi) | Canlı akış uçtan uca: master → alt playlist → 303 KB gerçek segment |
| **3** | ✅ **BİTTİ** — `PerdeService` + `PerdeActivity` + Ayarlar girişi | Telefon oda açıyor; PC'den katılım, senkron 8/9, pil kilidi bırakılıyor |
| **4** | `relay.py` (Python röle istemcisi) + Worker JS + Durable Object | Uzaktaki arkadaş linkle giriyor |
| **5** | hayalet entegrasyonu: "Odaya gönder", `/v/<id>.m3u8` | Dizi sayfasından tek dokunuşla odaya |
| **6** | Cilalama: pil, ısınma, Worker proxy ölçümü, iPhone testi | Yayına hazır |

Fazlar sırayla ve **her biri çalışır durumda** teslim edilmeli; 1 ve 2
telefon olmadan doğrulanabildiği için hata ayıklaması ucuz.

---

## Açık riskler

| Risk | Durum |
|---|---|
| ~~Paketler Chaquopy'ye girer mi~~ | ✅ kapandı (Ö3, Ö8) — +0,9 MB |
| ~~Werkzeug yükü kaldırır mı~~ | ✅ kapandı (Ö6) — 32 eş zamanlı istek, hatasız |
| ~~curl-cffi iş parçacığı güvenliği~~ | ✅ kapandı (Ö6) — kırılamadı; yine de parçacık başına oturum |
| ~~Worker CDN'den 403 yer mi~~ | ✅ kapandı (Ö7) — yemiyor, ama `Referer` şart |
| ~~cloudflared telefonda çalışır mı~~ | ✅ kapandı (Ö5) — **çalışmıyor**, A planı iptal |
| ~~Telefon sunucusuna dışarıdan erişim~~ | ✅ kapandı (Ö8) — aynı Wi-Fi'da PC'den bağlanıldı |
| **Worker rölesinin gecikmesi** | AÇIK — B yazılınca ölçülecek; senkron kayması kabul edilebilir mi |
| **iPhone Safari** | AÇIK — cihaz yok, bende test edilemez. Yerel HLS dalı + otomatik oynatma kısıtı + HTTPS'siz `getUserMedia` |
| **Telefon ısınması / pil** | AÇIK — gerçek oturumda ölçülecek. Worker proxy'si devredeyse yük çok düşük olmalı |
| Chaquopy'de `run_simple` | `use_reloader=False` şart; selftest'te öyle kullanıldı ve sorun çıkmadı |

---

## Bilinen sınırlar (kullanıcıya baştan söylenecek)

- Birlikte izlerken uygulamanın kendi oynatıcısı (PiP, zoom, devam etme)
  kullanılmaz; oda kendi sayfasını açar.
- iPhone'da kalite/ses/altyazı menüsü görünmez (yerel HLS dalı).
- Odayı açan telefon kapanırsa/uykuya dalarsa oda düşer; kalıcı kayıt yok.
- Uzun maratonlarda PC'yi sunucu yapmak hâlâ daha rahat; telefon "PC yokken"
  seçeneği.
