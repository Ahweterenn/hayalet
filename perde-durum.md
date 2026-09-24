# Perde / birlikte izleme — nerede kaldık

Son güncelleme: 2026-09-23. Planın kendisi `perde-plan.md`'de; burası
"ne çalışıyor, ne açık, yarın nereden devam" notu.

---

## Tek cümlede

Perde'nin sunucusu Python'a taşındı ve telefonda çalışıyor: oda açılıyor,
davet linki paylaşılıyor (tünel srv.us ile kuruldu), video odaya
gönderilebiliyor. "Video 0:00'da kalıyor" hatasının kök nedeni bulundu ve
düzeltildi (aşağıda); kalan iş, telefonda gerçek bir bölümle uçtan uca
deneme.

---

## Çalışan ve doğrulanmış olanlar

| Ne | Nasıl doğrulandı |
|---|---|
| Sunucu (oda, senkron, sohbet, lider) | 2 gerçek socket.io istemcisiyle 9/9 test |
| Proxy zinciri | Masaüstünde canlı yayın: master → alt playlist → **303 KB gerçek segment** |
| SSRF + döngüsel istek koruması | Telefonda 403 döndü |
| Telefonda sunucu | PC'den katılım: WebSocket 291 ms, polling 797 ms |
| Ön plan servisi + pil kilidi | `ACQ hayalet:oda` → oda kapanınca `REL` |
| **Uzaktan erişim (SSH tüneli)** | **srv.us üzerinden kuruldu** ve BAŞKA bir ağdan (PC'nin Wi-Fi'ı) gelen HTTPS isteği telefondaki odaya ulaştı: `Server: Werkzeug/3.1.8` + `302 → /invite.html?room=…` |
| Oda arayüzü (üst şerit 👥) | Ekran görüntüleriyle; Kopyala düğmesi pano bildirimi verdi |
| Oda sayfasının hayalet teması | Ekran görüntüsü: turuncu vurgu, palet oturdu |
| "Odaya gönder" → oynatma | Telefonda gerçek bölüm: 0:00 / **1:05:36**, gerçek kare, kareler ilerliyor |
| Birim testleri | 148 geçiyor (perde + tünel yarışı + proxy playlist + segment önbelleği) |

---

## Tünel (2026-09-22): "yalnızca aynı Wi-Fi" nasıl çözüldü

Belirti: oda açılıyor ama davet linki yerel kalıyor, ekranda "yalnız aynı
Wi-Fi ağındakiler girebilir" yazıyordu. Ölçülenler:

| Ne | Sonuç |
|---|---|
| PC'nin Wi-Fi'ı (10.46.x) | Giden **22 portu tamamen kapalı** (`github.com:22` bile zaman aşımı) |
| Telefon, mobil veri | 22 açık — `localhost.run:22` banner veriyor (`SSH-2.0-lhr-2.0`) |
| localhost.run kimlik doğrulama | **Reddediyor**: anahtarsız (`none`) istek cevapsız, RSA ve ed25519 → `Authentication failed` |
| serveo.net / a.pinggy.io / bore.pub | TCP'yi kabul edip **tek bayt yazmadan** kapatıyor — kullanılamaz |
| tuns.sh (sish) | Anonim anahtarı reddediyor, pico.sh hesabı gerekiyor |
| **srv.us** | **Çalıştı** — üretilen ed25519 anahtarıyla doğrulandı, `https://<rastgele>.srv.us` verdi |

Koddaki gerçek kusurlar (sağlayıcıdan bağımsız, hepsi düzeltildi):

1. **Tek uca bağlıydık.** Artık `tunnel.ENDPOINTS` denenir ve her ucun
   kendi hatası ayrı yazılır ("tünel kurulamadı" teşhise yetmiyordu).
   Denemeler 2026-09-23'te sıralıdan **paralele** çevrildi — aşağıya bak.
2. **paramiko sonsuza kadar bekliyor.** `request_port_forward` *ve*
   `open_session`/`invoke_shell` cevap gelmezse süresiz bekler. İkisi de
   yaşandı: doğrulama geçtikten sonra iş parçacığı 85+ sn hiçbir hata
   yazmadan sustu, ekranda "kuruluyor…" takılı kaldı. Hepsi
   `_call_with_timeout` ile sınırlandı.
3. **Hata ekrana hiç ulaşmıyordu.** `_tunnel_error` yalnız `start()`
   döndüğünde yazılıyordu; arkada devam eden turların sebebi kayboluyordu.
   Artık köprü `_tunnel_err()` ile **canlı** okuyor, hatalar her deneme
   bitince (tur sonunu beklemeden) yazılıyor.
4. **Adres bayatlıyordu.** `on_url` bağlandı: tünel düşüp yeniden kurulunca
   yeni adres `publicUrl`'e kendiliğinden yazılıyor.
5. srv.us, oturum kanalı **açılmadan** gönderilen yönlendirme isteğine hiç
   cevap vermiyor → `Endpoint.session_first`.

Arayüz: tünel yoksa sebebi tek satır gösteriliyor (`tunnelNote`, örn. "Bu ağ
SSH çıkışını engelliyor — mobil veride dene"). Ham hata `tunnelError` /
`tunnelErrors` alanlarında duruyor.

**Dikkat:** srv.us adresi anahtardan türüyor ve anahtar her açılışta yeniden
üretiliyor → **her oturumda link değişir**. Sabit link istenirse anahtarı
saklamak gerekir (yapılmadı).

---

## ÇÖZÜLDÜ (2026-09-22): video 0:00'da kalıyordu

**Kök neden proxy'de, `proxy_api.handle` içindeydi.** İstek akış modunda mı
yapılacak, ADRESE bakılarak kararlaştırılıyor (`stream=not is_manifest`).
Dizipal'in alt playlist adreslerinde `.m3u8` YOK (`l.php?v=...`), dolayısıyla
akış modunda isteniyorlar. Ama yanıtın content-type'ı `mpegurl` gelince kod
playlist dalına giriyor ve gövdeyi **`resp.text`** ile okuyordu — akış
modunda o BOŞTUR. Sonuç:

* proxy **200 + 0 bayt** playlist döndürüyor,
* boş gövde **önbelleğe** yazılıp aynı adresin sonraki isteklerini de
  zehirliyor,
* oynatıcı master'ı alıp başlıyor, segment hiç gelmiyor → bekleme →
  sohbete "⏸ … bağlantı sorunu var, oda senkron için durduruldu" düşüyor,
  süre 0:00'da kalıyor.

Kararsız olmasının sebebi: aynı adres content-type'ı **bazen `text/html`
bazen `mpegurl`** döndürüyor. text/html geldiğinde "gizli manifest" dalı
çalışıyor ve o dal gövdeyi `_read_all` ile doğru okuyor — o yüzden bazen
çalışıyordu.

**Düzeltme:** akış modunda gövde `_read_all(resp)` ile okunuyor, ayrıca boş
playlist ASLA önbelleğe alınmıyor (bayat kopya varsa o veriliyor, yoksa 502).
Regresyon testleri: `tests/test_perde.py` içinde 3 test — ikisi düzeltme
geri alınınca başarısız oluyor (doğrulandı).

**Ölçülen sonuçlar (düzeltmeden sonra):**

| Nerede | Zincir |
|---|---|
| Masaüstü | master 7.352 B → alt playlist 3.181.487 B → ilk segment 942.256 B |
| **Telefon** (adb forward ile telefonun proxy'sine) | aynı: 7.352 B → 3.181.487 B → 942.256 B, ve **ikinci istek de dolu** (önbellek temiz) |

### Telefonda uçtan uca doğrulandı (2026-09-22)

Gerçek cihazda, gerçek bölümle, arayüzün kendisinden (adb ile sürülerek):
ara → *House of the Dragon* → S01E01 satırına dokun → **Odaya gönder** →
"Odaya gönderildi" → **ODAYA GİR** → isim sor → oda oynatıcısı:

* süre çubuğu **0:00 / 1:05:36** (önceden süre hiç gelmiyor, çark dönüyordu),
* ekranda bölümün gerçek karesi,
* 6 sn arayla alınan iki ekran görüntüsü farklı → **video akıyor**.

Davet satırı da aynı ekranda "Uzaktaki arkadaşların da girebilir" diyordu
(srv.us tüneli), yani iki düzeltme birlikte çalışıyor.

**Dikkat — teşhis tuzağı:** masaüstü tarayıcıda deneme YANILTICI sonuç
veriyor, çünkü **Chrome gizli/arka plan sekmede MediaSource'u açmıyor**:
`MEDIA_ATTACHED` hiç gelmez, `mediaSourceState` `closed` kalır, hls.js
master+alt playlist+ses izini yükleyip tek parça istemez ve HATA DA VERMEZ.
Belirti gerçek bir oynatma hatasıyla birebir aynı. İlk iş
`document.visibilityState`.

### Yan bulgu: hls.js CDN'den `@latest` ile geliyor

`public/room.html` satır 26: `https://cdn.jsdelivr.net/npm/hls.js@latest`.
Ana projede hls.js **yerelde paketli** (`hayalet/assets/hls.min.js`) —
oda sayfası ise her açılışta CDN'e bağımlı ve sürüm sabit değil. jsdelivr'a
erişilemeyen bir ağda oda sayfası hiç oynatamaz. Yerele almak tek satırlık
iş, yapılmadı (karar bekliyor).

---

## Oda ekranı + akış (2026-09-22, ikinci tur)

Kullanıcının şikâyeti: "tam ekran çalışmıyor, oda aç→dizi seç mantığı hantal,
web sürümündeki her şey çalışsın." Perde'nin `public/*` dosyaları zaten
birebir kopya, yani eksikler **WebView ve akış** tarafındaydı.

### WebView'da ölü olan özellikler ve sebepleri

| Özellik | Sebep | Düzeltme |
|---|---|---|
| **Tam ekran** | `WebChromeClient`'ta yalnız `onPermissionRequest` vardı; HTML5 tam ekranı `onShowCustomView`/`onHideCustomView` ister, yoksa `requestFullscreen()` **sessizce hiçbir şey yapmaz** | ikisi eklendi; geri tuşu önce tam ekrandan çıkıyor |
| **Sesli/görüntülü görüşme** | manifest'te `CAMERA`/`RECORD_AUDIO` **hiç yoktu** — uygulamanın kendinde olmayan izni `grant()` ile sayfaya vermek imkânsız, `getUserMedia` sessizce düşüyordu | izinler eklendi; sayfa isteyince Android izni isteniyor, sonuç sayfaya aktarılıyor |
| **Görüşme düğmeleri görünmüyordu** | `style.css`'te `@media (max-height:430px) and (orientation:landscape)` içindeki `body:not(.call-active) .voice-panel{display:none}`. Telefon yatayken CSS yüksekliği **~411px** (2340x1080, dpr 2.625) → kural devreye giriyor ve görüşmeyi BAŞLATAN düğmeleri de gizliyor | `hayalet.css`'te geçersiz kılma: panel başlığı görünür (video/kontrol alanları arama başlayana kadar yine gizli), tam ekran dışta |
| **"Yeni Sekmede Aç" / harici bağlantı** | `window.open` WebView'da varsayılan olarak yok sayılır | `setSupportMultipleWindows` + `onCreateWindow` → sistem tarayıcısı; oda içine harici site yüklenmesi de engellendi |
| **Ekran kararması** | oda ekranında `FLAG_KEEP_SCREEN_ON` yoktu | eklendi |
| **Her girişte isim sorusu** | `room.js`, `username` boşsa `prompt()` açıyor | ad URL'de veriliyor, soru çıkmıyor |
| **hls.js CDN'den `@latest`** | sürüm sabit değil + CDN'e erişilemeyen ağda oda hiç oynatmaz | `public/hls.min.js` (ana projedeki dosyanın birebir kopyası), `room.html` yerelden yüklüyor |

### Çok izleyici: aynı segment artık bir kez iniyor (2026-09-23)

Belirti (kullanıcı): telefon oda açtı, PC'den iki sekme linkle girdi; iki
sekme birbirini görüyor (sohbet + görüntülü görüşme) ama telefon takılıyor ve
girenler videoyu göremiyor.

Ölçüldü: oda sunucusunun portu TCP'de bağlanıyor ama **hiç HTTP cevabı
vermiyordu** — PC'den de, telefonun kendi içinden de. `adb shell`, hatta
`logcat` bile boş dönüyordu: telefon doymuştu.

**Sebep:** odadaki her izleyici aynı segmenti proxy'den istiyor ve her istek
AYRI bir yukarı akış indirmesi başlatıyordu. Üç izleyici = aynı ~1 MB'ın üç
kez inmesi, üç kez TLS, üç iş parçacığı. (Görüşmenin çalışması şaşırtmasın:
sesli/görüntülü **WebRTC**, iki PC arasında doğrudan gidiyor, telefona
uğramıyor. Video ise peer-to-peer değil.)

**Düzeltme** — `manifest.SegmentCache` + proxy'de tek uçuş:

* segment gövdesi + içerik türü önbellekte (90 sn, parça ≤ 8 MB, toplam ≤ 32 MB, LRU),
* aynı segment indirilirken gelen ikinci istek **ikinci indirmeyi başlatmaz**,
* akarken önbelleğe yazılır (ilk izleyici beklemez), yarıda kesilen indirme
  önbelleğe YAZILMAZ,
* `octet-stream`/`text` ile gelen segmentler de önbelleğe girer (o dal
  önbelleği atlıyordu — bazı kaynaklar segmenti böyle sunuyor),
* Range'li istekler kapsam dışı,
* kilit sahipliği ince bir sarmalayıcıda: hangi dönüş yolundan çıkılırsa
  çıkılsın bırakılıyor.

**Telefonda ölçüm** (aynı koşuda, 200 KB'lık hedef):

| | 3 eş zamanlı istek | sonraki istek |
|---|---|---|
| Aynı adres (yeni) | 561 / 575 / 575 ms — tek indirme paylaşılıyor | **68 ms** (önbellek) |
| Farklı adres (eski davranış) | 470 / 479 / 493 ms — üç ayrı indirme | 398 ms |

Bekleme süresi 3 sn: hls.js bir segmenti iptal edip aynı adresi hemen
isteyebiliyor, uzun bekleme oynatmayı kilitler. Süre dolarsa kendi kopyamızı
çekeriz (en kötü ihtimalde eski davranış).

### Teşhis: playlist geliyor ama segment gelmiyorsa sorun KAYNAKTA

Aynı gün, düzeltmeden sonra oynatma yine 0:00'da takıldı. Telefonun kendi
içinden yürütülen zincir testi ayrımı netleştirdi:

| Adım | Sonuç |
|---|---|
| master (proxy'den) | 7.671 B, **63 ms** |
| alt playlist | 3.113.689 B, **743 ms** |
| ilk segment | **488 B, 49.090 ms** → bizim `504 Gateway Timeout` |
| aynı segment tekrar | 7.731 B → kaynaktan **522** (Cloudflare "origin'e ulaşılamıyor") |

Playlist'ler `sn.dplayer82.site`'den geliyor, segmentler **ayrı bir CDN'den**
(`lkm-…​.cfd`) ve o gün o CDN ölüydü. Yani sunucu sağlıklı (63 ms!), önbellek
çalışıyor, oynatma kaynağın çökmesinden takılıyor. **Bir daha bu belirtiyle
karşılaşınca ilk iş bu zincir testi:** playlist hızlı + segment 504/522 =
bizde değil.

### Tünel hızı: 23.8 sn → 3.2 sn (2026-09-23)

Kullanıcı "link açmıyor, açarsa da çok yavaş, mobil veride bile 'aynı Wi-Fi'
diyor" dedi. Telefonda ölçüldü: oda sunucusu **1.2 sn**'de kalkıyor, davet
linki **23.8 sn** sonra geliyordu. Sebep uçların SIRAYLA denenmesiydi ve
sürenin tamamına yakınını ölü uç yiyordu:

* `localhost.run(none)` → yönlendirme isteğine cevap yok, **12 sn** zaman aşımı
* `localhost.run(ed25519)` → `Authentication failed`
* `localhost.run(rsa)` → telefonda RSA-2048 üretimi + yine reddedildi
* ancak bundan sonra `srv.us` sıraya geliyor (~3 sn)

**Düzeltme:** uçlar artık **paralel yarışıyor** (`_one_pass`), ilk adres veren
kazanıyor, kaybedenler `kazanan` olayını görünce kendini kapatıyor. Bir ucun
kendi yöntemleri yine sırayla deneniyor (aynı sunucuya üç eşzamanlı bağlantı
gereksiz). `srv.us` listede başa alındı (sıra artık yalnız hata okunuşunu
etkiliyor). Yeniden ölçüm: **sunucu 0.6 sn, tünel 3.2 sn** — aynı yol, aynı
cihaz, aynı hat.

Yanında iki arayüz düzeltmesi:

* Davet satırı tünel beklerken **durumu önde** yazıyor ("Uzak bağlantı
  kuruluyor… · şimdilik yalnız aynı Wi-Fi"). Önceden başta "yalnız aynı
  Wi-Fi" yazdığı için kullanıcı bozuk sanıyordu.
* "Birlikte izle" sonrası panoya **yalnızca tünel hazırsa** kopyalanıyor;
  değilse "davet linki hazırlanıyor" deniyor. Önceden yerel ağ adresi
  "davet linki kopyalandı" diye veriliyordu — uzaktaki arkadaşta çalışmaz.

Testler: 143 (paralel yarış için üç yeni test; kazanan belli olunca kaybeden
ucun kalan yöntemlerinin denenmediği de sınanıyor).

### Akış: "önce odayı aç" kaldırıldı

Eski: 👥 → "Odayı aç" → geri → diziyi bul → satır → "Odaya gönder" →
3 düğmeli kutu → "Odaya gir". (`episodeTapped` oda kapalıyken seçenek bile
sunmuyordu, `sendToRoom` "Önce odayı aç" diyip duruyordu.)

Yeni: **diziye gir → satıra dokun → "Birlikte izle"**. Oda kapalıysa
`birlikteIzle` kendisi açıyor (`odaHazirOlunca` ~20 sn'ye kadar bekler),
bölüm gönderiliyor, davet linki panoya kopyalanıyor ve doğrudan odaya
giriliyor. Ek olarak: oda ekranında "Video seç → Ara" satırı, film
detayında "👥 Birlikte izle" düğmesi artık oda kapalıyken de görünüyor.

### Telefonda doğrulananlar

* Oda KAPALIYKEN satıra dokun → "Kendim izle / Birlikte izle" çıkıyor.
* "Birlikte izle" → tek dokunuşta `PerdeActivity`'ye kadar geldi (oda açıldı,
  bölüm gönderildi, odaya girildi) — iki ayrı denemede.
* İsim sorusu çıkmıyor; süre **0:00 / 1:05:36** geliyor.
* **Tam Ekran**: oynatıcı tüm ekranı kapladı, kareler ilerledi (oynuyor),
  geri tuşu tam ekrandan çıktı, odadan atmadı.
* Görüşme düğmeleri sağ panelde göründü; "🎤 Sesli" görüşmeyi başlattı
  (başlık kırmızı "İptal Et"e döndü).
* `CAMERA`/`RECORD_AUDIO` artık sistemde tanınıyor (`granted=false`, istem
  bekliyor) — önceden izin hiç YOKTU.

### Doğrulanmayanlar (sırada)

1. "📹 Görüntülü" düğmesinin taşması için yapılan CSS düzeltmesi kuruldu ama
   ekran görüntüsüyle bakılamadı (telefon adb'den düştü).
2. Android kamera/mikrofon **izin isteminin** gerçekten çıkması: Perde'nin
   sesli akışı karşı tarafı bekliyor, `getUserMedia` o an çalışmıyor. İkinci
   bir katılımcı ya da görüntülü yol ile denenmeli.
3. Harici bağlantının sistem tarayıcısında açılması.
4. Misafir (ikinci cihaz) tarafı — eski açık madde.

---

## Yazılan kod

### Bu depo — `hayalet/perde/`

| Dosya | İş |
|---|---|
| `rooms.py` | Oda durumu, lider, yasak, bekleme (saf, testli) |
| `guard.py` | SSRF + DNS-rebinding koruması (saf, testli) |
| `manifest.py` | m3u8 yeniden yazma, TTL, bayat önbellek (saf, testli) |
| `proxy_api.py` | `/api/proxy` — curl-cffi, akış tabanlı (akış modunda gövde `_read_all` ile okunur, bkz. çözülen hata) |
| `events.py` | socket.io olayları (Perde sözleşmesiyle birebir) |
| `server.py` | WSGI + werkzeug + yönlendirmeler |
| `tunnel.py` | SSH ters tüneli — uç zinciri (localhost.run → srv.us), paramiko, her adımda zaman aşımı |
| `selftest.py` | Telefonda yığın ayağa kalkıyor mu |
| `public/` | Perde'nin arayüzü + **`hayalet.css`** (tema katmanı) |

`tests/test_perde.py` — 48 test (39 perde + 6 tünel + 3 proxy playlist).

### Android projesi (`C:\Users\Public\hayalet-android`)

- `PerdeService.java` — ön plan servisi, wake/wifi kilidi, bildirim
- `PerdeActivity.java` — oda ekranı (WebView), vurgu rengini enjekte eder
- `MainActivity.java` — üst şeritte 👥, `showRoom()`, `sendToRoom()`,
  `episodeTapped()`
- `hayalet_app.py` — `perde_start/status/stop/send`
- `build.gradle` — 9 yeni paket (socketio yığını) + 5 paket (paramiko yığını)

---

## Tasarım kararları (Perde'den ayrıldığımız yerler)

- **Liderlik IP tahminiyle değil sunucu anahtarıyla.** Röle/tünel arkasında
  herkes aynı IP'den gelir, eski yöntem yanlış kişiyi seçerdi.
- **Oda kimliği rastgele**, sabit `PERDE` değil (`room.js`'te tek satır).
- **Ev sahibi kendi ekranını loopback'ten açar** — `network_security_config`
  düz metin HTTP'ye yalnız `127.0.0.1` izin veriyor.
- `/api/shutdown` ve puppeteer scraper taşınmadı.

---

## Bu oturumda öğrenilen tuzaklar (tekrar düşmeyelim)

1. **Gradle, junction'lı `hayalet/` içindeki Python değişikliğini fark
   etmiyor.** "43 up-to-date" deyip ESKİ kodu paketliyor. Python'a
   dokunduysan `clean assembleDebug` şart.
2. **Cihazdaki Python bazen tazelenmiyor.** Yeni kod APK'da olduğu hâlde
   telefon eskisini çalıştırabiliyor; yeniden kurmak gerekti.
3. **`org.json`'da JSON `null`**, `optString(k,"")` ile okununca boş dize
   değil **`"null"` dizesi** döner. `isNull()` ile bakmazsan koşul yanlış
   çalışır (tünel yokken "uzaktan girilebilir" yazıyordu).
4. **Ekranı çizen fonksiyon, durumu sorup sonucunda kendini yeniden
   çağırmamalı** — sonsuz döngü olur ve liste sürekli yeniden kurulduğu için
   **düğmelere basılamaz**. Görsel olarak hiçbir belirti vermiyor.
5. **PowerShell ile dosya yazma** `build.gradle`'ı iki kez bozdu (BOM ve
   bozuk Türkçe karakter). Dosya düzenlemek için Edit aracı kullanılmalı.
6. **`stream=True` ile alınan yanıtta `resp.content`/`.text` boştur.** Proxy
   200 + 0 bayt dönüyordu, altyazılar boş gelirdi.
7. **cloudflared Android'de çalışmıyor** — statik Go binary'si `/etc/resolv.conf`
   bulamayıp `[::1]:53`'e düşüyor. Root'suz kaçışı yok, A planı iptal.
8. **Kör koordinatla `input tap` güvenilmez.** `uiautomator dump` ile gerçek
   koordinatı oku.
9. **"Tünel kurulmadı" demek teşhis değil.** Sebep üç ayrı yerde olabiliyor
   (ağ 22'yi kapatmış / sağlayıcı reddetmiş / paramiko sessizce asılı
   kalmış) ve çözümleri farklı. Uç + yöntem başına hata yazmak, tek satırlık
   genel mesajdan kat kat hızlı götürdü.
10. **`adb forward` + telefonda `nc` rölesi çalışmadı.** Cihazın kendi
    kabuğundan `127.0.0.1:PORT` cevap verdiği hâlde PC'den gelen istek zaman
    aşımına düştü (aynı `adb forward` gerçek uygulama portu 8477'ye
    sorunsuz gidiyor). İki deneme sonunda vazgeçildi — masaüstünden hızlı
    deneme isteniyorsa **USB internet paylaşımı** (`svc usb setFunctions
    rndis`) daha kestirme yol; ama telefonun kendi hattıyla aynı sonucu
    vermiyor: `localhost.run:22` telefondan erişilebilirken paylaşımlı
    hattan erişilemedi.
11. **PowerShell 5.1 `Invoke-WebRequest` modern TLS'te takılıyor**
    ("el sıkışması başarısız"). Tünel adresini doğrulamak için `curl.exe`
    kullan.
12. **`resp.text` / `resp.content`, `stream=True` ile alınan yanıtta BOŞTUR** —
    bu tuzağa İKİNCİ kez düşüldü (ilki altyazılarda, ikincisi alt
    playlist'lerde). Akış kararı adrese bakılarak veriliyor, gövde ise
    yanıtın content-type'ına göre okunuyor; ikisi ayrı yerde olduğu için
    sessizce uyuşmuyorlar. `stream=` verdiğin her yerde gövdeyi nasıl
    okuduğunu da kontrol et.
13. **Oynatma hatasını arka plan sekmesinde teşhis etmeye çalışma.** Chrome
    gizli sekmede MediaSource'u açmıyor; hls.js her şeyi yükleyip tek parça
    istemez ve hata da vermez — gerçek bir oynatma hatasıyla birebir aynı
    görünür. `document.visibilityState`'i ilk iş kontrol et.
14. **Perde'nin masaüstü bağımlılıkları eksikti** (`python-socketio`,
    `werkzeug`): yalnız `build.gradle`'da vardı, `hayalet.perde.server`
    masaüstünde import EDİLEMİYORDU. Artık `pip install -e ".[perde]"`.

---

## Derleme / kurulum

```powershell
$env:JAVA_HOME = "C:\Users\Public\android\jdk\jdk-17.0.20+8"
Set-Location "C:\Users\Public\hayalet-android"
# Python'a dokunduysan: clean ŞART
& "C:\Users\Public\android\gradle\gradle-8.9\bin\gradle.bat" clean assembleDebug

$adb = "C:\Users\Public\android\sdk\platform-tools\adb.exe"
$serial = "192.168.137.67:<port>"     # port her seferinde değişiyor
& $adb connect $serial
& $adb -s $serial install -r --streaming "C:\Users\Public\hayalet-android\app\build\outputs\apk\debug\app-debug.apk"
```

Geliştirme kancaları (arayüzden erişilemez):

```powershell
# odayı aç + durumu diske yaz
adb shell am start -n com.hayalet.test/.MainActivity --es perde 1   --es out /sdcard/Android/data/com.hayalet.test/files/perde.json
adb shell am start -n com.hayalet.test/.MainActivity --es perde stop --es out <...>
adb shell am start -n com.hayalet.test/.MainActivity --es perde open --es out <...>
```

Masaüstü testleri:

```powershell
python -m pytest tests -q          # 148 test
```

---

## Sürümler

- Telefonda: **0.6** (versionCode 6), APK ~40 MB
- Yayınlanan son sürüm: **v3 / 0.3** — arkadaşlara dağıtılan bu, etkilenmedi
- Yeni sürüm yayınlamak için: `yayinla.ps1 "<notlar>"`

---

## Yarın için sıra

1. ~~Oynatma sorununu çöz~~ — kök neden bulundu, düzeltildi ve telefonda
   uçtan uca doğrulandı (2026-09-22).
2. "Odaya gönder" sonrası odaya giren MİSAFİRDE de video açılıyor mu — iki
   cihazla gerçek deneme (ev sahibinde doğrulandı, misafir tarafı açık).
2b. Karar: hls.js'i CDN'den `@latest` yerine yerelden sunmak (yukarıdaki
   yan bulgu).
3. ~~Tünel adresi değişince davet linkinin güncellenmesi~~ — `on_url` ile
   çözüldü (2026-09-22).
4. Faz 5 kalanı: oda ekranında "şu an oynayan" bilgisini göstermek.
5. İstenirse: iPhone Safari denemesi (cihaz yok, bende test edilemiyor).
6. İstenirse sabit davet linki: srv.us adresi anahtardan türüyor, anahtarı
   saklamak yeter (şu an her açılışta yeni anahtar → yeni link).
7. localhost.run'ı listeden çıkarmak yerine bırakıldı: bir gün tekrar
   kabul ederse sıra ondan başlıyor. Tekrar ölçmeden atma.
