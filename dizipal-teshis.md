# Dizipal segment sorunu — teşhis günlüğü

Belirti: odaya gönderilen Dizipal bölümü oynamıyor. Playlist'ler geliyor,
**segmentler gelmiyor**; oynatıcı 0:00'da donuyor ve sohbete "bağlantı sorunu"
düşüyor. Aynı anda eski web sürümü (tarayıcı + sniffer eklentisi) aynı
diziyi oynatabiliyor.

Bu dosya ölçüm günlüğü: her satır ya bir ölçüm ya da elenen bir hipotez.
Tahmin yazılmıyor — yalnız çalıştırılıp görülen.

---

## Ölçüm ortamı

| | Telefon | PC |
|---|---|---|
| Ağ | Turkcell mobil veri (100.84.x) | Wi-Fi 10.46.x |
| curl_cffi | **0.16.1b1** (Android arm64 tek wheel, `build.gradle`'da sabit) | **0.16.2** |
| Dizipal alan adı | `dizipal1583.com` | `dizipal1583.com` |
| Oynatıcı hostu | `sn.dplayer82.site` | `sn.dplayer82.site` |

---

## Zincirin durumu (telefon, tekrar tekrar ölçüldü)

```
master:        7.671 bayt      43–150 ms   200 OK
alt playlist:  3.113.689 bayt  772–939 ms  200 OK        (proxy'de yeniden yazılmış hâli)
ilk segment:         488 bayt  49.1 sn     504 Gateway Timeout   ← bizim proxy: "kaynak yanıt vermedi"
tekrar:            7.730 bayt  44.5 sn     522                   ← CDN: "origin'e ulaşılamıyor"
```

Segment hostu playlist hostundan **farklı**: `lkm-cahiu1.wxq7k7dgq6.cfd`
(Cloudflare arkasında).

---

## Kritik ölçüm: adres kimde üretildi?

| Ne | Sonuç |
|---|---|
| **PC'de üretilen** segment adresi, **PC'den** çekildi | 200, 942.256 bayt, 3,5 sn |
| **PC'de üretilen** segment adresi, **telefonun proxy'sinden** çekildi | **200, 942.256 bayt, 0,8 sn** |
| **Telefonda üretilen** segment adresi, telefondan | 504 (49 sn yanıtsız) |
| **Telefonda üretilen** segment adresi, PC'den | 522 |

**Sonuç:** telefonun ağı ve bizim proxy kodumuz sağlam — telefon, PC'nin
ürettiği adresten videoyu indirebiliyor. Bozuk olan, **telefonun siteden
aldığı adresin kendisi**.

---

## Elenen hipotezler (hepsi denendi)

| Hipotez | Nasıl elendi |
|---|---|
| Farklı Dizipal aynası | İki taraf da `dizipal1583.com` (telefonun `hayalet-cache/domain.json`'ı okundu) |
| Farklı oynatıcı sunucusu | İkisi de `sn.dplayer82.site` |
| Biz yanlış kaynağı seçiyoruz | `source2.php` **tek** kaynak veriyor: `playlist` 1 giriş, `sources` 1 öğe, `state:true`, `expired:false` |
| Yeniden çözersek başka sunucu gelir | İki ayrı çözümleme de aynı hostu verdi |
| Persona / User-Agent / TLS profili (isim olarak) | PC'de **altı personanın hepsi** segment indirdi (206, 65.536 bayt) |
| İstek başlıkları (Origin/Sec-Fetch/Accept-Language) | PC'de sade başlıkla da proxy'nin başlıklarıyla da 200 |
| Çerez (site `PHPSESSID`'sini CDN'e göndermek) | Çerez ekliyken de 200 |
| Oturum/bağlantı tekrar kullanımı (keep-alive) | Taze oturumla da 200 |
| Segment adresinin yolu farklı | Telefon ve PC'de **birebir aynı** yol |
| Token süresi dolmuş | Taze mint'te de aynı hata |
| Mobil hat engeli | Telefon, PC'nin ürettiği adresi indirebiliyor |

---

## Kalan tek ölçülebilir fark: TLS parmak izi

`tls.browserleaks.com/json` üzerinden:

| Nerede | JA3 | JA4 |
|---|---|---|
| **Telefon** (proxy üzerinden, Android arm64, curl_cffi 0.16.1b1) | `cd08e31494f9531f560d64c695473da9` | `t13d1516h2_8daaf6152771_e5627efa2ab1` |
| PC `chrome` (0.16.2) | `8de1d070815b6b36695dba4360cc96ec` | `t13d1516h2_8daaf6152771_806a8c22fdea` |
| PC `chrome131` | `b1cfee6d01aeab31a936fc4080f64eaa` | `t13d1516h2_8daaf6152771_02713d6af862` |
| PC `safari17_0` | `773906b0efdefa24a7f2b8eb6985bf37` | `t13d2014h2_a09f3c656075_14788d8d241b` |
| PC `chrome_android` | `d5fe3942b1fdc5dd432a902021bff481` | `t13d1516h2_8daaf6152771_02713d6af862` |

Telefonun imzası PC'deki **hiçbir** profille eşleşmiyor; UA "Windows Chrome"
diyor. Çalışan hipotez: CDN mint anında bu uyumsuzluğu görüp kullanılamaz bir
adres veriyor.

---

## Yan bulgular (ayrı işler)

1. **`merge.probe_video` yanlış pozitif veriyor.** `Range: bytes=0-4095` ile
   alınan playlist parçasını "segment geldi" sayıyor: telefonda üç profil de
   "ok, 4096 bayt" dedi ama gerçek zincir yine 504 verdi. Segment kararı
   içerik türüne/gerçek akışa bağlanmalı.
2. **Proxy küçük metin yanıtlarını kırpıyor olabilir.** Parmak izi testinde
   JSON yarım geldi (1.228 bayt, "unterminated string"). Segmentlerle ilgisiz
   ama gerçek bir hata.
3. Eski web sürümünün Node sunucusu (`server.js`) depoda yok (`reference/` altında yalnız
   sniffer eklentisi var). Karşılaştırma için kullanıcıdan yolu istendi.

---

## Sıradaki adım

Telefondaki sürümü (curl_cffi **0.16.1b1**) PC'ye kurup:
1. JA3/JA4 telefonunkiyle aynı mı?
2. Aynıysa: aynı sürümle Dizipal akışı üretilince segment ölüyor mu?

Aynı çıkarsa hata masaüstünde yeniden üretilmiş olur ve çözüm orada aranır
(telefon gerekmez). Farklı çıkarsa fark Android derlemesinden kaynaklanıyordur
ve arm64 bir ortam gerekir.

---

## 2026-09-23 akşamı — telefon yokken yapılanlar

### 1. Parmak izi farkı SÜRÜMDEN değil, Android DERLEMESİNDEN

Telefondaki sürüm (`curl_cffi==0.16.1b1`) PC'ye kuruldu ve ölçüldü:

| Profil | PC 0.16.1b1 (JA4) | PC 0.16.2 (JA4) | Telefon (JA4) |
|---|---|---|---|
| chrome | `…806a8c22fdea` | `…806a8c22fdea` | `…e5627efa2ab1` |
| chrome131 | `…02713d6af862` | `…02713d6af862` | — |
| chrome_android | `…02713d6af862` | `…02713d6af862` | — |

Yani aynı sürüm Windows'ta PC parmak izini üretiyor. Fark **Android arm64
wheel'inin kendi derlemesinden** geliyor. Sonuç: hata Windows'ta yeniden
üretilemez; doğrulama için arm64 bir ortam (telefon ya da arm64 emülatör)
şart.

### 2. Aday çözüm hazır: curl_cffi 0.16.1b1 → 0.16.3

PyPI'da Android arm64 için daha yeni wheel'ler var (cp313, Chaquopy'nin
sürümüyle uyumlu): `0.16.2`, `0.16.3` (son kararlı), `0.16.4b1`.
Telefondaki pin en eskilerden bir betaydı.

* `app/build.gradle` → `install "curl_cffi==0.16.3"` (gerekçesi dosyada yazılı)
* APK **derlendi ve paketlendi**: `curl_cffi-0.16.3.dist-info` (40 MB, 20:39)
* Doğrulanmadı: parmak izi değişti mi ve Dizipal segmentleri geliyor mu —
  bunun için telefon gerekiyor.

### 3. `probe_video` yanlış pozitifi düzeltildi

Önceki hâli `Range` ile gelen HTML hata sayfasını segment sanıyordu (telefonda
üç profil de "ok, 4096 bayt" demişti, gerçek zincir yine 504 veriyordu). Artık:
durum kodu 200/206, içerik türü HTML değil, gövde ≥ 8 KB (Cloudflare'in 522
sayfası 7,3 KB) ve gövde `<html` ile başlamıyor. Üç yeni test eklendi
(hata sayfası, küçük gövde, master'ın playlist olmaması). Toplam **162 test**.

### Telefon geldiğinde yapılacak sıra

1. Yeni APK'yı kur (`curl_cffi 0.16.3`).
2. `--es probe chrome --es out …` kancasıyla parmak izi + segment sınaması:
   JA4 hâlâ `…e5627efa2ab1` mi, yoksa PC'ninkine mi döndü?
3. Zincir testi (`master → alt playlist → ilk segment`) — segment 200 geliyorsa
   Dizipal çözüldü demektir.
4. Gelmiyorsa: `--es probe <profil>` ile profil süpürmesi (artık sınama
   güvenilir), sonra gerekirse `0.16.4b1`.

---

## 2026-09-23 22:10 — Node sürümü okundu: çözüm mimaride

Kullanıcı eski web sürümünün çalışan Node kodunu verdi (`server.js`, `room.js`,
`scraper.js`) + yerel eklenti (`reference/örnek eklenti/`) okundu.

### Bulgu: eski web sürümü akışı KENDİSİ üretmiyor

| | Eski web sürümü (çalışıyor) | hayalet mobil (çalışmıyor) |
|---|---|---|
| m3u8 adresini kim üretiyor | **Gerçek Chrome**: `scraper.js` puppeteer ile sayfayı açıp play'e basıyor, `.m3u8`'i ağ trafiğinden dinliyor. Eklenti aynı işi kullanıcının tarayıcısında yapıyor. | curl-cffi (TLS taklidi) |
| İstek başlıkları | Eklenti gerçek isteğin `cookie/referer/origin/user-agent/authorization`'ını çalıp odaya veriyor (`background.js:580-631`), proxy aynen tekrar oynatıyor | Yalnız `referer` (bizim ürettiğimiz) |
| Proxy'nin TLS'i | **Düz Node `http.get`, taklit YOK** | curl-cffi |

Node proxy'sinin taklide ihtiyacı olmaması tesadüf değil: adres gerçek bir
tarayıcı tarafından üretildiği için CDN onu zaten geçerli sayıyor.

### Manifest işi bizde birebir aynı — fark orada değil

`hayalet/oda/manifest.py` ile `server.js` satır satır karşılaştırıldı; şunlar
zaten portlanmış: `inject_master_params` (master query'sini alt adreslere
taşıma), `rewrite_body` + `URI="..."` niteliği, `ttl_seconds` (master 300 sn /
VOD 120 sn / canlı 4 sn), bayat kopya yedeği, `ld.php → l.php` 404 tekrarı,
sahte `.jpg` segmentine `video/mp2t` verme, gizli manifest (text/plain gelen
m3u8) tespiti. Yani proxy tarafında eksiğimiz yok — ölçüm de bunu söylüyordu:
telefonun proxy'si PC'nin ürettiği adresi sorunsuz indiriyor.

### Elenen ek hipotez: "link tek kullanımlık"

`server.js` yorumu dizipal/dplayer linklerini "kısa ömürlü ve tek kullanımlık"
diye anıyor. Denendi: PC'de üretilen AYNI segment adresi iki kez çekildi, ikisi
de 200 ve **aynı boyut** (942.256 bayt) → segmentler tek kullanımlık değil.
Master da tekrar tekrar 200 verdi. Bu hipotez düşüyor.

### Bugün yapılan (telefon bağlıyken)

1. `clean assembleDebug` + kurulum → `lastUpdateTime=2026-09-23 22:10:10`.
   **Gotcha:** düz `assembleDebug` "UP-TO-DATE" diyor; Chaquopy junction'lı
   Python dizinindeki değişikliği görmüyor (kaynak 20:40/20:47, APK 20:39
   olmasına rağmen). Python değişikliği girecekse `clean` ŞART.
2. TLS profil süpürmesi (`--es probe <profil>`) başlatıldı; ilk üç profil
   120 sn içinde dosyaya yazmadı, sonra telefon USB'den düştü ve geri gelmedi
   (`adb devices` boş). Ölçüm yarım kaldı.

### Sıradaki: `scraper.js`'i WebView'e portla

Telefonda gerçek tarayıcı motoru var. Gizli bir WebView bölüm/iframe adresini
açar, `WebViewClient.shouldInterceptRequest` ile `master.m3u8` isteğini ve
başlıklarını yakalar, Python'a verir; oda bunları `headers` olarak saklar.
Bu, çalışan sürümün mimarisinin birebir karşılığı ve curl-cffi parmak izini
denklemden çıkarır.

---

## 2026-09-23 22:32 — Çözüm yazıldı: source2.php gerçek tarayıcıda

Teşhisin işaret ettiği yere göre **yalnız zehirlenen adım** devredildi.
Zincirin tamamı değil: çözme (`data-rm-k`), iframe, `openPlayer`, altyazı ve
sonraki bütün akış Python'da kaldı. Devredilen tek istek `extractor.py`'nin
3. adımı — `source2.php?v=<playList>` — çünkü oynatılacak CDN düğümünü seçen
istek bu.

### Değişenler

| Dosya | Ne |
|---|---|
| `hayalet/core/extractor.py` | `browser_fetch` kancası (modül düzeyi, varsayılan `None`) + `_source2_json()`: kanca varsa onu kullanır, **başarısızsa sessizce curl-cffi'ye düşer** |
| `tests/test_extractor_hook.py` | 6 test: kanca yokken curl, kanca varken tarayıcı kazanır ve ikinci istek gitmez, None/boş/HTML dönerse yedeğe düşer, kanca patlarsa akış kırılmaz |
| `BrowserFetch.java` (Android) | WebView'i ana iş parçacığında kurar, iframe sayfasını **Referer ile** yükler, `onPageFinished`'de aynı kökenden `fetch()` eder, `@JavascriptInterface` ile gövdeyi döner; reklam alan adlarını `shouldInterceptRequest`'te keser; `CountDownLatch` + 20 sn üst sınır; WebView her yolda ana iş parçacığında yıkılır |
| `MainActivity.java` | `BrowserFetch.init(this)` (Context kaydı) |
| `hayalet_app.py` | `_tarayici_kancasi_kur()` — `from java import jclass` ile köprüyü `extractor.browser_fetch`'e bağlar; `_ensure()`'dan bir kez çağrılır, kurulamazsa sessizce vazgeçer |

Tasarım kuralı: kanca **hiçbir koşulda akışı kıramaz**. Masaüstünde hiç
kurulmaz (davranış değişmez), telefonda kurulamaz/zaman aşımına uğrarsa eski
curl-cffi yolu aynen çalışır. Kötü bir adres almak, hiç adres almamaktan iyi.

Reklam kara listesi Python'daki `config.AD_INTERCEPT_DOMAINS`'ten parametre
olarak geçiyor — tek kaynak bozulmasın.

Test: **169 geçiyor** (163 → 169). APK derlendi (22:32), `BrowserFetch`
`classes3.dex` içinde doğrulandı.

### DOĞRULANMADI — telefon gerekiyor

Telefon USB'den düştü ve geri gelmedi. Sebep bulundu: Windows aygıt listesinde
**"SAMSUNG Mobile USB Remote NDIS Network Device" hâlâ OK** — telefon önceki
IP sınamasında verdiğim `svc usb setFunctions rndis` yüzünden tethering
kipinde kalmış, ADB işlevi açığa çıkmıyor. PC'den düzeltilemiyor.

Telefon dönünce yapılacak sıra:
1. Kabloyu çıkar–tak; USB kipi "Dosya aktarımı"na dönmezse
   Geliştirici Seçenekleri → "Varsayılan USB yapılandırması" → Dosya aktarımı.
   (Ya da kablosuz hata ayıklama açılıp portu bildirilirse USB'ye hiç gerek yok.)
2. APK'yı kur (`clean assembleDebug` şart — junction'lı Python'ı Gradle
   yoksa görmüyor).
3. Doğrulayıcı ölçüm: **aynı bölüm için** `source2.php`'nin döndürdüğü `file`
   adresini iki yolla al ve karşılaştır —
   kancasız (curl-cffi) vs kancalı (WebView). Adresler farklıysa ve yalnız
   WebView'inki segment veriyorsa teşhis doğrulanmış, Dizipal çözülmüş olur.
4. Sonuç ne olursa olsun buraya yaz.

---

## 2026-09-24 — PC, kullanıcının telefon hotspot'u (Turkcell) üzerinden

| Bölüm | Alt playlist | Segment hostu | İlk / orta / son segment |
|---|---|---|---|
| Behzat Ç. S01E01 | 69.984 B, 653 parça | `lkm-cahiu1.kuwv2pao78.cfd` | **522**, 522, 522 (her biri ~20 sn) |
| House of the Dragon S01E01 | 143.205 B, 1.313 parça | `lkm-at73vk.wxq7k7dgq6.cfd` | 206 (0,6–1,0 sn) |

Tablette de aynısı görüldü: Behzat açılmadı, HotD ve Kurtlar Vadisi oynadı.
PC de aynı ağda, yani fark cihazdan ya da ağdan kaynaklanmıyor. Bölüm uzunluğu da
sebep değil: playlist'i daha büyük olan HotD oynuyor. Bozuk olan **`lkm-cahiu1`
CDN düğümü**. 23 Eylül'de telefonda 522 veren düğüm de buydu (`lkm-cahiu1.wxq7k7dgq6.cfd`).
Bu düğüm hangi bölümlere atanmışsa onlar her yerden oynamıyor; sorun sitenin tarafında.

### DÜZELTME — düğüm ölü DEĞİL, 522 sahte (aynı gün, sonradan ölçüldü)

Behzat tarayıcıda oynuyor ve tarayıcı da **aynı `lkm-cahiu1` düğümünü** kullanıyor (her parça
ayrı bir rastgele `.cfd` alan adında, hepsi 200). Tarayıcıda 200 dönen **birebir aynı adres**,
aynı PC'den ve aynı ağdan curl-cffi ile şöyle cevap veriyor:

| Başlıklar | Sonuç |
|---|---|
| yalnız `Referer` (bizim eski isteğimiz) | **522**, 20 sn |
| yalnız `Origin` | 403, 0,3 sn |
| `Origin` + `Referer` | **200**, 0,4 sn |

Kök neden şu: düğüm `Origin` başlığı olmayan isteğe 20 saniye bekletip gerçek bir çökme gibi görünen
bir 522 dönüyor. hls.js bu başlığı çapraz kökenli XHR'de kendiliğinden ekliyor, biz eklemiyorduk.
HotD'nin düğümü bu kontrolü yapmadığı için o oynuyordu. Yukarıdaki "telefonda üretilen adres bozuk"
ölçümü de büyük ihtimalle bununla açıklanıyor.

Düzeltme `network.media_headers()` ile yapıldı; `proxy._fetch` ve `merge.probe_video` bunu kullanıyor.
Oda proxy'sinin `proxy_api._upstream_headers` fonksiyonu zaten Origin gönderiyordu. Doğrulama (PC):
Behzat için probe 1,2 sn'de True döndü, proxy üzerinden segment 200 (852.580 B, 1,3 sn).
HotD de hâlâ 200. 169 test geçiyor.
