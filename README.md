# hayalet

**Tarayıcısız (headless)**, saf Python bir terminal aracı — Dizipal (dizi) ve hdfilmcehennemi.nl
(film), `--site` ile seçilir. Cloudflare/anti-bot korumasını TLS taklidiyle aşar; arar, sezon/bölüm
gezer (ya da filmi doğrudan çözer), m3u8 akışını ve Türkçe altyazıyı çözer; **tarayıcıda** (hls.js)
izletir veya **ffmpeg** ile toplu indirir.

## Yasal uyarı

Bu proje **kişisel ve eğitim amaçlıdır**; bir tarayıcının halka açık bir web sayfasında zaten
yapabileceği istekleri (arama, sayfa gezme, oynatıcının kendi yüklediği akışı çekme) otomatikleştirir.
Hiçbir video/altyazı dosyasını barındırmaz, depolamaz veya yeniden dağıtmaz — yalnızca kaynak sitenin
sunduğu genel-erişimli akışa yerel olarak bağlanır.

Dizipal (veya eriştiği herhangi bir kaynak site) ile **hiçbir bağlantısı, ortaklığı yoktur**. İçerik
üzerindeki tüm haklar ilgili hak sahiplerine aittir. Bu aracı kullanmak, bulunduğun ülkenin telif hakkı
ve ilgili mevzuatına uyma sorumluluğunu **sana** yükler; geliştirici(ler) araçtan doğabilecek herhangi
bir kullanım için sorumluluk kabul etmez. Proje **"olduğu gibi"**, hiçbir garanti verilmeksizin sunulur.

## Kurulum

```bash
cd hayalet
python -m venv venv
venv\Scripts\activate            # Windows (PowerShell: venv\Scripts\Activate.ps1)
pip install -r requirements.txt
```

> Alternatif: `pip install -e .` ile kurulursa `hayalet` konsol komutu oluşur ve
> her yerden `hayalet` yazarak çalıştırabilirsin (yoksa `python -m hayalet` ya da
> `baslat.bat`).

Sistem araçları:
- **Bir tarayıcı** — izleme için (varsayılan tarayıcın otomatik açılır, ek kurulum gerekmez).
- **ffmpeg** — indirme (mux) için (PATH'te olmalı).

Hem izleme hem indirme, yerel bir **impersonating proxy** üzerinden çalışır: tarayıcı/ffmpeg
localhost'taki proxy'ye bağlanır, proxy segmentleri curl-cffi (Chrome taklidi) ile çeker. Böylece
CDN'in TLS engeli (doğrudan bağlanınca 403) aşılır, ses+görüntü birlikte gelir ve **Türkçe altyazı
master'a HLS kanalı olarak enjekte edilir** (her oynatıcı native görür).

## Kullanım

İnteraktif menü (önerilen):
```bash
python -m hayalet          # veya kuruluysa sadece: hayalet
```
Akış: **ara → sonuç seç → İzle/İndir**. Dizide araya sezon/bölüm seçimi girer;
**filmde doğrudan İzle/İndir** (film tek dosyadır, bölüm sorulmaz). İndirmede boşluk
tuşuyla birden fazla bölüm işaretlenir. `--site` verilmezse arama **hem dizipal hem
hdfilmcehennemi'de aynı anda** yapılır, sonuçlar tek listede (hangi siteden geldiği
etiketiyle) gösterilir — hangi sitede olduğunu düşünmene gerek yok. Tek bir siteyle
sınırlamak için: `hayalet --site dizipal`.

> **Kalite:** indirmeler her zaman **mevcut en yüksek kalitede** yapılır (soru sorulmaz).

Komut satırı (script/otomasyon):
```bash
python -m hayalet --search "house of the dragon"                 # sonuçları listele
python -m hayalet --search "..." --series 1 --season 1 --episode 1 --action extract
python -m hayalet --search "..." --series 1 --season 1 --episode 1 --action watch
python -m hayalet --search "..." --series 1 --season 1 --episodes 1,2,3 --action download
python -m hayalet --site hdfilmcehennemi --search "matrix" --series 0 --action watch   # film sitesi
```

Domaini elle vermek (resolver'ı atlamak) için: `--domain https://dizipalXXXX.com` (yalnızca `--site` ile).
Site seçimi: `--site dizipal` (varsayılan) veya `--site hdfilmcehennemi`.

### İndirme konumu
İndirmeler kullanıcının **`Downloads`** klasörüne yapılır:
- **Dizi** → `Downloads/<Dizi Adı>/<Dizi> - S01E01.mkv` (bölümler tek klasörde toplanır).
- **Film** → `Downloads/<Film Adı>.mkv` (tek dosya, ayrı klasör açılmadan).

## Nasıl çalışır

1. **Anti-bot bypass** — `curl-cffi` gerçek Chrome TLS/JA3 imzasını taklit eder (standart
   `requests` 403 yer).
2. **Dynamic Domain Resolver** — güncel `dizipalXXXX.com` adresini bulur, `.cache`'e yazar.
3. **Arama** — `POST /bg/searchcontent` (JSON) ile dizi/film bulunur.
4. **Extractor** — bölüm sayfasındaki şifreli kaynak (`data-rm-k`, CryptoJS AES + PBKDF2-SHA512)
   Python'da çözülür → oynatıcı iframe'i → `source2.php` → gerçek `master.m3u8` + Türkçe `.vtt`.
5. **Yerel proxy** — master m3u8'de ses ayrı kanaldır ve segmentler `.jpg` gibi gizlenir; ayrıca
   CDN, TLS taklidi olmayan istemcileri (tarayıcı/ffmpeg) 403 ile engeller. Çözüm: localhost'ta bir
   proxy açılır, playlist'lerdeki URL'ler ona yönlendirilir, segmentler `.ts` olarak sunulur ve
   curl-cffi (impersonate) ile çekilir.
6. **Aksiyon** — İzleme: proxy'nin sunduğu hls.js sayfası tarayıcıda açılır, proxied master'ı
   oynatır (video + **Türkçe ses** birlikte; sağ üstteki ⚙ menüsünden kalite/ses/altyazı ve
   altyazı boyut/renk/arka plan ayarı, klavye kısayolları: `f` tam ekran, boşluk oynat/duraklat,
   ok tuşları sar/ses). İndirme: ffmpeg proxied video (**en yüksek kalite varyantı**) + tüm sesler
   (+ varsa Türkçe altyazıyı softsub) tek geçişte mux'lar → sesli `mp4` / çoklu ses ya da
   altyazılıysa `mkv` (`language=tur`).

> **İkinci site (hdfilmcehennemi):** aynı proxy/ffmpeg altyapısını kullanır ama çıkarım farklıdır —
> arama `/search?q=` JSON'u, kaynak `/rplayer/` sayfasındaki paketlenmiş (packer) + karıştırılmış
> JS'ten çözülür (bkz. `hayalet/sites/hdfilmcehennemi_adapter.py`). Yeni bir site eklemek =
> `hayalet/sites/` altına bir adapter yazmak; ortak katmanlar değişmez.

## Ölü dublaj kaynakları → otomatik fallback

Bazı **"Türkçe Dublaj"** sürümleri `canvascascade.site` host'una gider. Bu bir anti-bot **değil**,
**ölü/park edilmiş bir domain**dir: CHEQ interstitial → RTB reklam → `sk-park.php` domain-parking
zinciri. Arkasında video yoktur (tarayıcı da açamaz, sadece park reklamı görür).

Araç bunu tespit edince (`DeadSourceError`) **otomatik olarak** aynı içeriğin **orijinal /
altyazılı** sürümünü bulup ona geçer (o `dplayer82`'de canlı) ve şunu bildirir:

> *Dublaj kaynağı ölü → orijinal/altyazılı sürüme geçildi: Loki (S1E1)*

**Dürüst not:** Bu, Türkçe **dublaj sesi yerine orijinal ses + Türkçe altyazı** verir; dublaj
sesi kaynakta gerçekten yok. Muadili bulunamazsa net hata döner. `dplayer82` içerikleri (çoğunluk)
zaten doğrudan çalışır.

> Site bundle'ı yenilenirse `data-rm-k` **passphrase**'i değişebilir; `config.py > RMK_PASSPHRASE`
> güncellenerek düzeltilir.

## Android uygulaması

Native Android proje ayrı çalışma klasöründedir:
`C:\Users\Public\hayalet-android`

Gereksinimler:
- Android SDK: `C:\Users\Public\android\sdk`
- Java 17: `C:\Users\Public\android\jdk\jdk-17.0.20+8`
- Gradle 8.9: `C:\Users\Public\android\gradle\gradle-8.9`
- Telefon ve bilgisayar aynı ağda olmalı; telefonda **Geliştirici seçenekleri > Kablosuz hata ayıklama** açık olmalı.

### APK derleme

PowerShell:

```powershell
$env:JAVA_HOME = "C:\Users\Public\android\jdk\jdk-17.0.20+8"
Set-Location "C:\Users\Public\hayalet-android"
& "C:\Users\Public\android\gradle\gradle-8.9\bin\gradle.bat" clean assembleDebug
```

APK çıktısı:
`C:\Users\Public\hayalet-android\app\build\outputs\apk\debug\app-debug.apk`

### Telefona bağlanma ve kurulum

Telefonun kablosuz hata ayıklama ekranında görünen IP ve portu kullan. Port her
seferinde değişebilir; örnek:

```powershell
$adb = "C:\Users\Public\android\sdk\platform-tools\adb.exe"
$serial = "192.168.1.46:46041"
$apk = "C:\Users\Public\hayalet-android\app\build\outputs\apk\debug\app-debug.apk"

& $adb start-server
& $adb connect $serial
& $adb devices
& $adb -s $serial install -r --streaming $apk
& $adb -s $serial shell am force-stop com.hayalet.test
& $adb -s $serial shell monkey -p com.hayalet.test 1
```

`install` çıktısı **Success** olmalı. Bağlantı reddedilirse telefondaki kablosuz
hata ayıklamayı kapatıp aç, ekrandaki yeni portu kullan ve `adb connect` komutunu
tekrarla. Aynı telefonda mDNS satırı görünürse IP:port yerine şu seri de
kullanılabilir:

```powershell
& $adb devices
# örnek: adb-RFCY413HFQY-v8Rxhn._adb-tls-connect._tcp
```

Kurulumdan sonra doğrulama:

```powershell
& $adb -s $serial shell dumpsys package com.hayalet.test |
    Select-String "versionName|lastUpdateTime"
```

Android Python kodu ana repodaki `hayalet` klasörüyle senkron/junction
üzerinden paketlenir. Python değişikliğinden sonra APK'nın gerçekten güncel
olması için `clean assembleDebug` çalıştırılmalıdır.
