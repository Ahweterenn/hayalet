# Birlikte izleme odası — notlar

Uygulamanın "Birlikte izle" özelliği: telefon/tablet (ya da PC) küçük bir
sunucu olur, arkadaşlar davet linkini tarayıcıda açıp katılır; oynatma,
sohbet ve görüntülü görüşme herkeste senkron. Bu dosya mimariyi ve ÖLÇÜLEREK
bulunmuş tuzakları tutar — tekrar düşmemek için.

## Parçalar

| Yer | Ne yapar |
|---|---|
| `hayalet/oda/server.py` | WSGI + werkzeug; sayfayı, `/api/proxy`, `/api/img`, socket.io'yu sunar |
| `hayalet/oda/events.py` | socket.io olayları: katılma, senkron, sohbet, yetki, öneri, katalog, WebRTC sinyali |
| `hayalet/oda/rooms.py` | saf oda modeli (kişiler, lider, kumanda modu, öneriler, sohbet geçmişi) |
| `hayalet/oda/catalog.py` | odanın içinden arama / bölüm / akış çözme; istemciye yalnız opak `ref` gider |
| `hayalet/oda/proxy_api.py`, `manifest.py`, `guard.py` | video proxy'si (curl-cffi), playlist yeniden yazımı, SSRF süzgeci |
| `hayalet/oda/cloudflared.py`, `tunnel.py` | uzaktan erişim: ana yol cloudflared, yedek SSH (srv.us, localhost.run) |
| `hayalet/oda/web/` | oda sayfası (derleme adımı yok): `oda.js`, `player.js`, `call.js`, `library.js`, `ui.js`, `oda.css` |
| Android `OdaService` / `OdaActivity` | ön plan servisi (sunucu + tünel), WebView + `HayaletApp` köprüsü |

Yetki: **ev sahibi** (sunucunun ürettiği anahtarla bağlanan) içerik koyar,
öneriyi onaylar, kişi çıkarır, kumanda modunu seçer. **Kumanda**
(`controlMode`): `all` herkes, `host` yalnız ev sahibi oynatır/durdurur/sarar;
yetkisiz hareket `control-denied` ile geri çevrilir. **Misafir** içerik
önerir (katalogdan ya da link), izler, konuşur. Sohbette ad ve renk
sunucudaki kayıttan gelir; istemcinin bildirdiği ada güvenilmez.

## Nasıl denenir

- Mantık: `pytest` (oda testleri `tests/test_oda.py`, `test_oda_yetki.py`,
  `test_cloudflared.py`; ağsız).
- Sayfa: masaüstünde gerçek katalogla bir `OdaServer(catalog=Catalog(...))`
  kaldır, Playwright ile iki tarayıcı aç (sahte kamera:
  `--use-fake-ui-for-media-stream --use-fake-device-for-media-stream`).
  **Gerçek Chrome kullan** (`channel="chrome"`): Playwright'ın Chromium'u
  H.264 çözmüyor, video siyah kalır. Playwright proje bağımlılığı değil:
  kur, ölç, kaldır. Her düzen için ekran görüntüsü al ve bak.
- Senkron ölçüldü (2026-09-24): iki taraf arasında 0,00 sn fark; durdur,
  sar, oynat karşıya aynen geçiyor.

## Tuzaklar (hepsi ölçüldü)

**Tünel ve ağ**
- cloudflared Android'de DNS'siz çalışıyor: hızlı tünel kaydını Python
  yapar, sunucu IP'lerini Python çözüp `--edge` ile verir. Tablette 1,0–1,4
  MB/s; paramiko'lu SSH tüneli aynı gün 0,23–0,29 MB/s idi. İkili
  `jniLibs/arm64-v8a/libcloudflared.so` olarak paketlenir (Android yalnız
  nativeLibraryDir'den çalıştırmaya izin veriyor).
- SSH tünelinde 4 MB soket tamponu hızı 4 katına çıkardı (Windows).
- localhost.run oturum kanalındaki isteklere cevap vermiyor ama adresi
  yazıyor: `shell` cevap beklenmeden gönderilmeli.
- Güvenli İnternet filtresi `srv.us`'u aralıklı engelledi; `trycloudflare.com`
  açık kaldı.
- **"Yalnız aynı ağda çalışıyor"un sebebi tünel hızıydı**: uzaktaki misafirin
  bütün video trafiği tünelden geçiyor, yerel ağda geçmiyor.

**Proxy**
- `stream=True` ile alınan yanıtta `resp.content`/`.text` BOŞTUR (iki kez
  düşüldü). Akışta gövde `_read_all` ile okunur; boş playlist önbelleğe alınmaz.
- Master'ın sorgu parametreleri yalnız AYNI hosttaki alt adreslere taşınır:
  başka hosttaki segmente eklenince Dizipal'in CDN'i 522 döndü.
- Segment isteklerinde `Origin` şart (bazı CDN düğümleri onsuz 20 sn bekletip
  sahte 522 dönüyor).
- curl-cffi oturumu tarayıcı kimliğine göre ayrı: odanın içinden başka siteye
  geçince eski kimlikle istenmesin (webdramaturkey yalnız chrome kabul ediyor).
- Aynı segment tek kez iner (`SegmentCache` + tek uçuş); bekleme 3 sn —
  uzun bekleme hls.js'in iptal/tekrar döngüsünde oynatmayı kilitledi.
- Playlist hızlı gelip segment 504/522 dönüyorsa önce tarayıcının AYNI adresi
  alıp alamadığına bak: alıyorsa sorun başlıklarda, almıyorsa kaynakta.

**Sayfa**
- Tam ekranda DOM'da eleman TAŞINMAZ: eski sayfa görüşme/sohbet panelini
  taşıyordu, videolar duruyordu. Artık sayfanın tamamı tam ekrana geçer,
  düzeni CSS değiştirir. Bildirim kutusu da uygulama kabının İÇİNDE olmalı,
  yoksa tam ekranda görünmez.
- Sinema düzeni kararı görünen alana değil CİHAZ ekranına bakar
  (`screen.width/height`): klavye görünen yüksekliği düşürünce tablet
  "yatay telefon" sanılıp sohbet gizleniyordu.
- `replaceChildren(null)` ekrana "null" yazar; `ui.fill()` kullan.
- Kullanıcı metni asla `innerHTML`'e girmez (`h()` textContent yazar);
  eskiden arayan adı kaçışsız basılıyordu (XSS).
- Altyazı kendi katmanımızda çizilir; iz `hidden` modda tutulur. Bazı VTT'lerde
  aynı satır aynı zamanlı iki kez var (Slow Horses 1x01'de 4 yer): çizerken
  aynı anda görünen özdeş satırlar teke indirilir.
- Oynatıcı uygulamanınkinin (PlayerControls.java) kopyası: iki satırlı
  başlık; kilit, çark (sağdan kayan panel: Kalite/Ses/Altyazı + altyazı
  ayarları); cam daire oynat; kalan/toplam süre geçişi; kenarda çift dokunuş
  sarar (art arda birikir), ortada doldur; yana kaydırma zamanda sarar;
  dikey kaydırma parlaklık/ses (dolum çubuklu gösterge). "Sonraki bölüm"
  yalnız ev sahibinde: hap son 5 dk, jenerikte (son 25 sn) kart, bitince 5 sn
  geri sayım. **Hız menüsü bilerek yok**: odada herkes aynı hızda izler,
  kayma düzeltmesi hızı zaten 1'e çekiyor.
- PC'de denerken Dizipal CDN'i segmentlere 403 verebilir (ağdan); arayüzü
  herkese açık bir test HLS'iyle (`set_video(..., now={..., "hasNext": True})`)
  dene. Odaya konan içerik duraklatılmış başlar: betik önce oynat'a basmalı.
- Kısa sahne (dikey telefonda 16:9 şerit) kararı `@container` ile sahnenin
  kendi yüksekliğinden verilir; ortadaki düğmeler, "Sonraki bölüm" ve
  altyazı üst üste biniyordu.
- Otomatik oynatma engelinde çıkan "Başlatmak için dokun" katmanı açıkken
  kontroller gizlenir (`.prompting`), yoksa ortadaki düğmelerle çakışıyor.
- CDP ile telefon taklidinde `screenOrientation` da verilmeli; yoksa cihazın
  gerçek yönü (yatay) kalır ve sayfa sinema düzenine geçer. Taklit, CDP
  oturumu kapanınca sıfırlanır: boyut + yenile + ekran görüntüsü tek oturumda.
- Oynatma hatasını arka plan sekmesinde teşhis etme: Chrome gizli sekmede
  MediaSource'u açmıyor ve hata da vermiyor.

**Görüşme (WebRTC)**
- ICE adayları karşı açıklama kurulmadan eklenemez; kuyrukta beklemezse
  gecikmeli tarafta bağlantı hiç başlamıyordu (0/4 → 6/6).
- Aynı anda arama: socket kimliği küçük olan arayan kalır (0/4 → 6/6).
- `disconnected` hemen kapatmaz (8 sn tolerans, sonra ICE yeniden başlatma,
  yalnız arayan). Görüşme sürerken gelen teklif mevcut bağlantıyı yeniden
  müzakere eder.
- `setParameters` müzakereden önce sessizce başarısız oluyor; sınırlar
  bağlantıdan sonra uygulanır.
- Bedava TURN (openrelay) ölü; yedek aktarıcı yok. İki taraf da doğrudan
  bağlantıya izin vermeyen NAT'taysa görüşme kurulamaz.

**Android**
- Gradle junction'lı `hayalet/` içindeki Python/web değişikliğini fark
  etmiyor: `clean assembleDebug` şart.
- Sistem çubukları yalnız tam ekran videoda gizlenir; gizliyken
  `adjustResize` çalışmıyor ve klavye sohbet kutusunu örtüyor. Android 15'te
  içerik çubukların altına uzandığı için oda kabı `setFitsSystemWindows(true)`.
- WebView'da HTML5 tam ekran `onShowCustomView` ister; kamera/mikrofon için
  manifest izni şart.
- `org.json`'da JSON `null`, `optString` ile `"null"` dizesi döner; `isNull()`.
- Davet linkinin uygulamada açılması (ayarsız): https tünel adresi rastgele
  alt alan, doğrulanamıyor; Android 12+ onu kullanıcı ayardan izin vermeden
  uygulamaya vermiyor. Çözüm: Android tarayıcısında giriş ekranında
  "hayalet uygulamasında aç" düğmesi, `hayalet://oda?u=<oda adresi>`
  (DavetActivity, yalnız tünel alan adlarını kabul eder). Ölçülen: Chrome
  ve Mi tarayıcısı sayfa açılırken uygulamaya KENDİLİĞİNDEN geçmiyor
  (`/i`'nin intent:// yönlendirmesi yok sayılıp fallback açılıyor — adb ile
  açılan linkte; gerçek mesaj dokunuşunda denenmedi); dokunuşla hayalet://
  ikisinde de çalışıyor, intent:// Mi'de çalışmıyor. Tablet Chrome'u
  masaüstü kimliği gönderiyor (`X11; Linux`), Android tespiti dokunmatik
  Linux'u da sayar.
- uiautomator dokunuşları ekran yenilenirken kaybolabiliyor: "düğme
  çalışmıyor" demeden önce logcat'e bak.
