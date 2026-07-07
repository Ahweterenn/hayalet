# hayalet

Dizipal için **tarayıcısız (headless)**, saf Python bir terminal aracı. Cloudflare/anti-bot
korumasını TLS taklidiyle aşar; dizi arar, sezon/bölüm gezer, m3u8 akışını ve Türkçe altyazıyı
çözer; **mpv** ile izletir veya **yt-dlp** ile toplu indirir.

## Kurulum

```bash
cd hayalet
python -m venv venv
venv\Scripts\activate            # Windows (PowerShell: venv\Scripts\Activate.ps1)
pip install -r requirements.txt
```

> Alternatif: `pip install -e .` ile kurup her yerden `hayalet` komutuyla çalıştırabilirsin.

Sistem araçları:
- **Bir oynatıcı** — izleme için. Otomatik algılanır, öncelik sırası **PotPlayer → VLC → mpv**.
  Kurulum örnekleri: `winget install Daum.PotPlayer` · `winget install VideoLAN.VLC` ·
  `winget install shinchiro.mpv`. `--player potplayer|vlc|mpv` ile elle seçebilirsin
  (veya `config.py > PLAYER`).
- **ffmpeg** — indirme (mux) için (PATH'te olmalı).

Hem izleme hem indirme, yerel bir **impersonating proxy** üzerinden çalışır: oynatıcı/ffmpeg
localhost'taki proxy'ye bağlanır, proxy segmentleri curl-cffi (Chrome taklidi) ile çeker. Böylece
CDN'in TLS engeli (doğrudan bağlanınca 403) aşılır, ses+görüntü birlikte gelir ve **Türkçe altyazı
master'a HLS kanalı olarak enjekte edilir** (her oynatıcı native görür).
> yt-dlp artık gerekmiyor (proxy+ffmpeg yeterli).

## Kullanım

İnteraktif menü (önerilen):
```bash
python -m hayalet
```
Akış: **Dizi ara → dizi seç → sezon seç → İzle/İndir → bölüm(ler) → kalite**.
İndirmede boşluk tuşuyla birden fazla bölüm işaretlenir.

Komut satırı (script/otomasyon):
```bash
python -m hayalet --search "house of the dragon"                 # sonuçları listele
python -m hayalet --search "..." --series 1 --season 1 --episode 1 --action extract
python -m hayalet --search "..." --series 1 --season 1 --episode 1 --action watch
python -m hayalet --search "..." --series 1 --season 1 --episodes 1,2,3 --action download --quality 1080
```

Domaini elle vermek (resolver'ı atlamak) için: `--domain https://dizipalXXXX.com`.

## Nasıl çalışır

1. **Anti-bot bypass** — `curl-cffi` gerçek Chrome TLS/JA3 imzasını taklit eder (standart
   `requests` 403 yer).
2. **Dynamic Domain Resolver** — güncel `dizipalXXXX.com` adresini bulur, `.cache`'e yazar.
3. **Arama** — `POST /bg/searchcontent` (JSON) ile dizi/film bulunur.
4. **Extractor** — bölüm sayfasındaki şifreli kaynak (`data-rm-k`, CryptoJS AES + PBKDF2-SHA512)
   Python'da çözülür → oynatıcı iframe'i → `source2.php` → gerçek `master.m3u8` + Türkçe `.vtt`.
5. **Yerel proxy** — master m3u8'de ses ayrı kanaldır ve segmentler `.jpg` gibi gizlenir; ayrıca
   CDN, TLS taklidi olmayan istemcileri (mpv/ffmpeg) 403 ile engeller. Çözüm: localhost'ta bir
   proxy açılır, playlist'lerdeki URL'ler ona yönlendirilir, segmentler `.ts` olarak sunulur ve
   curl-cffi (impersonate) ile çekilir.
6. **Aksiyon** — İzleme: mpv proxied master'ı oynatır (video + **Türkçe ses** birlikte). İndirme:
   ffmpeg proxied video + Türkçe ses (+ varsa Türkçe altyazıyı softsub) tek geçişte mux'lar →
   sesli `mp4` / altyazılıysa `mkv` (`language=tur`).

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
