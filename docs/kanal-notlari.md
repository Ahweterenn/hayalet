# Türk TV kanalları — kaynak olarak ekleme notları (2026-09-26)

Amaç: Türk dizisi boşluğu (örneklem ölçümü: Türk dizisi 2/12, yabancı dizi
9/10, film 10/10) kanalların kendi sitelerindeki ücretsiz, yasal yayınlarla
kapatılsın. Kural: **DRM'li kaynak eklenmez, coğrafi sınırlama aşılmaz.**
Henüz hiçbir adaptör yazılmadı; bunlar yoklama sonuçları.

## Hazır: tam bölüm, düz akış, DRM yok

| Kanal | Akış | Nereden | Doğrulanan |
|---|---|---|---|
| Kanal D | HLS 1080p (`kanaldvod.duhnet.tv/.../playlist.m3u8`) | bölüm sayfasında düz yazıyor | 150 dk, 4 kalite, anahtar yok |
| Show TV | HLS 1080p (`vmcdn.ciner.com.tr/...m3u8`) | bölüm sayfasında | 143 dk, 5 kalite |
| ATV | MP4 1080p (`*.erbvr.com/vms/..._1080p_3000k.mp4`) | `/<program>/<n>-bolum/izle` sayfasında | program bölümleri; dizi bölümü ayrıca doğrulanmalı |
| TRT Çocuk | HLS 1080p (`cdn-v.pr.trt.com.tr/.../master.m3u8`) | bölüm sayfasında (`/` kaçışlı) | 11 dk |
| Star TV | HLS 720p | DYG servisi, aşağıda | 138 dk |
| TLC | HLS 720p | DYG servisi | 44 dk |
| DMAX | HLS 720p | DYG servisi | 88 dk |

### DYG video servisi (Star TV, TLC, DMAX — tek ortak katman)

Sitelerin kendi oynatıcısı her ziyaretçide bunu çağırıyor; anahtar oynatıcı
betiğinde gömülü sabit değer.

- TLC / DMAX: `https://dygvideo.dygdigital.com/api/redirect?PublisherId=<20 TLC | 27 DMAX>&ReferenceId=<EHD_nnn>&SecretKey=NtvApiSecret2014*`
  → yönlendirmeyle `tlc-p3.mncdn.com/smil:EHD_nnn_smil.smil/playlist.m3u8?st=...`.
  ReferenceId sayfada: `data-video-code="EHD_636518"`.
- Star TV: `https://dygvideo.dygdigital.com/api/video_info?akamai=true&PublisherId=1&ReferenceId=StarTv_<24 hex>&SecretKey=NtvApiSecret2014*`
  → JSON; `smil` içeren m3u8 adresi asıl akış (ilk "delivery" adresi tek parça). Hex kimlik sayfanın Next.js verisinde `"referenceId":"69d7..."`,
  `publisherId:"1"` ve `StarTv_` öneki `_next/static/chunks/*.js` içinde.
- **Tuzak (ölçüldü):** TLC/DMAX'in `/player/info?referenceId=` adresi `flavors.hls`'te
  `geoblock1_smil` (30 sn yer tutucu) veriyor — WARP açık ya da kapalı fark
  etmedi. Asıl akış o değil, yukarıdaki redirect.

## Açık kalanlar

- **NOW:** `ADMPlayer.init({gmsId, referenceId})`, DRM izi yok; akış adresi
  statik sayfada ve `app.js`'te yok (oynatıcı başka yerden yükleniyor).
  Tarayıcıda ağ trafiğine bakılmalı.
- **TRT 1:** bölüm sayfası resmi YouTube videosunu gömüyor. Odada oynar;
  uygulama içi oynatıcıda YouTube akışını çıkarmak YouTube kurallarına aykırı
  → gömülü YouTube olarak aç.
- **tabii (TRT):** SPA, sayfada DRM izi → muhtemelen uygun değil.
- **TV8:** yalnız tanıtım videosu bulundu (imzalı HLS `?st=&e=`); tam bölümler
  muhtemelen Exxen'de (ücretli).
- **Kanal 7** (izle7.haber7.net'e gidiyor), **Beyaz TV**, **TV100**, **360**:
  bölüm sayfasına ulaşılamadı, bakılmadı.

## Öncelik önerisi
1. Kanal D, Show TV, ATV (düz sayfa kazıma)
2. Ortak DYG katmanı → Star TV, TLC, DMAX
3. NOW (ağ trafiği), TRT 1 (YouTube gömülü)

## Canlı yayın — iptv-org listesi (2026-10-04)

`https://iptv-org.github.io/iptv/countries/tr.m3u` (açık kaynak iptv-org/iptv,
yalnız herkese açık yayınlar). Ölçüm: 175 kayıt, 126'sı çalışıyor (200 + #EXTM3U).
Büyük kanalların çoğu kanalın KENDİ CDN'inden: TRT 1/Haber (`*.medya.trt.com.tr`),
Kanal D/TV8 (`*.daioncdn.net`, Demirören), Star/NTV (`dogus.daioncdn.net`),
ATV/A Haber/NOW (`*.turknet.ercdn.net`). Çalışmayanlar: Beyaz TV, Kanal 7
(sertifika), TRT Spor (üçüncü taraf host), ATV Avrupa (onrender.com — yeniden
yayın). Üçüncü taraf hostlar (onrender, siteyaptim, radyotvonline) elenmeli.
Show TV listede yok.

### Free-TV/IPTV karşılaştırması (2026-10-04)
`https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_turkey.m3u8`:
16 kayıt, 16'sı çalışıyor, hepsi TRT'nin kendi sunucusu (`tv-*.medya.trt.com.tr`).
Özel kanal yok. iptv-org'da OLMAYAN: TRT Spor, TRT Spor 2, TRT Belgesel, TRT World,
TRT Arabi. Ⓖ = yalnız Türkiye IP'si. Karar önerisi: iki listeyi birleştir,
aynı adres tek kayıt, kanalın kendi sunucusu olmayan hostları ele.
FAST (Pluto/Samsung TV Plus/Plex) listeleri: matthuisman'ın listeleri DMCA ile
kapandı; kendi barındırılan kazıyıcılar (FastChannels) var ama Türkiye'de
bu servislerin kanalları ölçülmedi — kararsız kaynak.
