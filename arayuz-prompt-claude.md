# hayalet — Claude için TEK prompt (HTML mockup)

Tamamını tek seferde yapıştır. Çıktı: tarayıcıda açılan tek bir HTML dosyası.

---

Sen kıdemli bir mobil ürün tasarımcısısın. Android için bir video uygulamasının
tüm arayüzünü tasarlayacaksın ve tasarımı **çalışan tek bir HTML sayfası** olarak
vereceksin. Aşağıda ürün, teknik kısıtlar, elimizdeki gerçek veri ve daha önce
VERİLMİŞ tasarım kararları var. Verilmiş kararları tartışma, uygula.

## 1. ÜRÜN

"hayalet" — Türkçe dizi/film izleme ve çevrimdışı indirme uygulaması. Reklamsız,
hesapsız, kişisel kullanım. İçerik iki kaynak siteden geliyor (dizipal ve
hdfilmcehennemi); kullanıcı için bu ayrım görünmez, sonuçlar tek birleşik liste.
Kullanım döngüsü: bul → izle, ya da bul → indir → çevrimdışı izle.
Logo: çay bardağından çıkan sevimli bir hayalet — boş/hata/çevrimdışı
ekranlarında maskot olarak kullanılabilir.

## 2. TEKNİK KISITLAR (tasarımı bunlar şekillendirir)

- Saf Android, Java. XML layout YOK, tüm arayüz kodla kuruluyor → tasarım
  tekrar kullanılabilir bileşenler halinde tarif edilmeli.
- Oynatma Media3/ExoPlayer ile, ayrı tam ekran Activity.
- Arka uçta gömülü Python katmanı. Sonucu: **her ağ işlemi yavaş** — arama
  5–20 sn, bir bölümün kaynağını çözmek ~10 sn, bir bölümü indirmek saatler.
  Yükleniyor ve hata durumları birinci sınıf tasarlanmalı.
- Görsel yükleyici kütüphanesi YOK ve eklenmeyecek (bkz. poster kararı).
- Hedef: 1080x2340 px, koyu tema, tek elle kullanım, arayüz dili Türkçe.
- İndirilenler tamamen çevrimdışı oynuyor — internetsiz çalışan tek akış.

## 3. PALET VE MEVCUT BİLEŞENLER (korunacak)

Zemin `#121620` · Yüzey/kart `#1B2130` · Yükseltilmiş yüzey `#242C3E`
Vurgu (turuncu) `#EA5814` · Metin `#F2F4F7` · Soluk metin `#8B93A5`

- Dolu turuncu hap buton (birincil): yükseklik 34, köşe 18, BÜYÜK HARF YOK
- Çerçeveli hap buton (ikincil)
- Kart: köşe 14, zemin #1B2130
- Tipografi: bölüm başlığı 11.5 büyük harf harf-aralıklı · alt metin 12.5 soluk ·
  normal başlık 15.5 · büyük başlık 20 · sayfa başlığı 24
- Ölçek: ekran kenar boşluğu 16, kartlar arası 12, liste satırı 64–72

## 4. ELİMİZDEKİ GERÇEK VERİ — ötesine geçme

**VAR:** yapım adı (uzun ve iki dilli tek metin) · tür (Dizi/Film) · kaynak site ·
diziyse sezon ve bölüm NUMARALARI · kaynak çözülünce kalite varyantları
(kaynağın gerçekten sunduğu, sabit liste değil), ses parçaları (Türkçe dublaj +
orijinal), Türkçe altyazı · indirme işi (durum: sırada/çözülüyor/indiriliyor/
bitti/hata/duraklamış, yüzde, inen/toplam parça, kalite, boyut) · izleme konumu
(yapım + bölüm + saniye → ilerleme yüzdesi).

**YOK** (her biri ayrı sayfa kazıması — opsiyonel yer tutucu olarak göster,
kompozisyonu bunlara dayama): IMDb puanı, yapım yılı, özet, yönetmen, oyuncular,
**bölüm adları**, tür etiketleri.

**Gerçek adlarla sına** — bir kelimelikten yedi kelimeliğe:
`Dark` · `Interstellar` · `The Last of Us` · `Spider-Man - Marvel's Spider-Man` ·
`Örümcek Adam Yepyeni Bir Gün - Spider Man Brand New Day` ·
`Iron Man: Technovore'un Yükselişi - Iron Man: Rise of Technovore` ·
`Yüzüklerin Efendisi Kralın Dönüşü - The Lord of the Rings: The Return of the King`

## 5. VERİLMİŞ KARARLAR — tartışma, uygula

**A) Poster görseli YOK → TİPOGRAFİK KAPAK KARTI.**
Gerekçe: poster kazımak görsel yükleyici + disk önbelleği + her raf açılışında
onlarca ağ isteği demek; çevrimdışıyken raflar boş kalırdı.
Kart üç katmanlı: (1) adın baş harfi arkada, taşacak kadar büyük, düşük
opaklıkta — kapak hissini veren asıl şey; (2) üstünde asıl ad, büyük punto;
(3) altta küçük tür rozeti "Film"/"Dizi". Renk **adından türetilir** (aynı ad =
hep aynı renk, kullanıcı kartı okumadan renginden tanısın); gökkuşağı olmasın,
koyu ve düşük doygunlukta, lacivert-turuncu dile uyumlu bir ton aralığı belirle.
Kartta **sadece tire öncesi Türkçe kısım** görünür. Ölçü: büyük 110x165 köşe 12,
küçük (liste satırı) 48x72 köşe 8. İleride poster eklenirse aynı kutuya oturmalı.
**Gerçek afiş, fotoğraf, illüstrasyon, karakter çizme — sadece renk ve tipografi.**

**B)** Puan/yıl/özet/oyuncu/bölüm adı listelerde gösterilmez; detayda
"sonraki aşamada eklenecek" yer tutucusu olarak durur.

**C)** Sezon/bölüm SAYISI liste satırlarında gösterilmez (öğrenmek dizinin
sayfasını çekmeyi gerektiriyor). Detayda gösterilir.

**D)** Kaynak site adı liste satırlarında gösterilmez — yalnızca yapım detayında
küçük ve soluk tek satır ("Kaynak: hdfilmcehennemi").

**E)** İndirme **tek işçi + sıralı kuyruk**. "Eşzamanlı indirme sayısı" ayarı
OLMAYACAK (paralel indirme bandı bölüştürüp ikisini de geciktiriyor; sırayla
inince ilk bölüm çok daha erken izlenebiliyor).

**F)** Kalite akışı: Ayarlardaki varsayılan kalite sessizce uygulanır, İndir
butonunda küçük "480p" etiketi görünür, başka kalite için butona **uzun basılır**
ve diyalog açılır. Her indirmede kalite sorma.

**G)** Oynatıcıda Cast/yayınla YOK (desteklenmiyor). PiP (küçük ekran) VAR.

**H)** Alt sekmeler: Ana Sayfa · Diziler · Filmler · İndirilenler. Ayarlar'a ana
sayfa başlığındaki ⚙ ikonundan gidilir — erişilemeyen ekran bırakma.

**I)** Yükleniyor, hata ve çevrimdışı durumları zorunlu, ayrı ayrı çizilecek.

## 6. ÇİZİLECEK EKRANLAR (bu öncelik sırasıyla)

1. **Ana sayfa** — logo + "hayalet" + ⚙; yatay raflar: "Kaldığın yerden devam"
   (kart altında ince turuncu ilerleme çizgisi + oynat rozeti + "S02E03"),
   "Son eklenenler", "Popüler"; her rafın sağında "Tümünü gör".
2. **Arama + sonuçlar** — arama alanı, "Tümü (12) / Diziler (3) / Filmler (9)"
   filtre çipleri, sonuç listesi.
3. **Arama — yükleniyor** — iskelet satırlar + "Aranıyor… bu 5–20 saniye
   sürebilir". Bu ürünün en çok bakılan ekranı, ciddiye al.
4. **Yapım detayı — dizi** — büyük tipografik kapak, tam ad (iki dilli, iki
   satır), "Kaynak: dizipal", sezon çipleri + [Sezonu indir], bölüm listesi
   (her satır "3. Bölüm" + [İzle] [İndir]), altta opsiyonel bilgi yer tutucusu.
5. **İndirilenler** — üstte "Uygulama verisi 2.6 GB · Cihazda boş alan 85 GB",
   filtre çipleri ve **üç ayrı satır durumu**: indiriliyor
   ("%34 · 542/1583 parça · 480p" + ilerleme + [Durdur]) · duraklamış/hatalı
   ("Duraklamış · 330/800 parça" + [Devam et] [Kaldır]) · bitmiş
   ("480p · 1.35 GB" + [İzle] [Kaldır]).
6. **Oynatıcı (kontroller açık)** — üstte geri + iki satırlık uzun başlık + PiP;
   ortada 10sn geri / oynat-duraklat / 10sn ileri; altta ilerleme çubuğu; en
   altta [Kalite] [Ses] [Altyazı]; bölüm bitmeye yakınken "Sonraki bölüm: …
   S1E02" şeridi; kilit ikonu.
7. **Diziler** — tür filtre çipleri + dikey liste (küçük tipografik kart + ad).
8. **Filmler** — aynı yapı.
9. **Yapım detayı — film** — sezon/bölüm yok; [İzle] ve [İndir · 480p].
10. **Arama — hata** — maskot + "Arama başarısız oldu" + [Tekrar dene].
11. **Çevrimdışı** — "İnternet yok, indirilenlerden izleyebilirsin" +
    [İndirilenler'e git].
12. **Ayarlar** — İndirme (klasör, varsayılan kalite) · Oynatma (altyazı dili,
    boyut, renk, arka plan) · Depolama (önbelleği temizle, uygulama verisi) ·
    Hakkında.
13. **Kategori / türler** — tür ızgarası + seçili türün sonuçları.
14. **Tipografik kart sayfası** — kartın anatomisi (katmanlar + ölçüler
    işaretli), yukarıdaki 8 gerçek adla 8 kartlık ızgara (renk çeşitliliği ve
    uzun/kısa ad davranışı görünsün), büyük ve küçük varyant yan yana.

## 7. KAÇINILACAK HATALAR (önceki denemelerde gerçekten çıktı)

- Aynı bilgiyi iki yerde gösterme (kartın üstünde bir rozet, satırın sağında
  aynısı). Bir bilgi, bir yer.
- Alt sekmede yanlış sekmeyi aktif gösterme (arama ekranındayken
  "İndirilenler"in turuncu olması gibi).
- Filtre çiplerini üst üste bindirme, rafı alt sekme çubuğunun üstüne taşırma.
- Erişilemeyen ekran bırakma.
- Kısa, tek dilli sahte adlarla çizip gerçekte bozulan düzen üretme.

## 8. ÇIKTI — tek HTML dosyası

Tasarımı **çalışan tek bir HTML sayfası** olarak ver: tek dosya, harici kaynak
yok, tüm CSS ve JS içeride.

- Her ekran 390px genişliğinde bir telefon çerçevesi içinde, üstünde başlığıyla;
  çerçeveler duyarlı bir ızgarada yan yana dizilsin.
- Metinler **gerçek metin** olsun (resim değil): yukarıdaki gerçek yapım adlarını
  aynen kullan, **kısaltma**. Uzun ad kutuyu taşırıyorsa gizleme — kırpma
  kuralını CSS'te uygula ve nasıl davrandığını göster.
- Renkler tam olarak verilen hex değerleri; ölçüler 1:1 px (16dp = 16px).
- Tipografik kapak kartının rengi addan türetilsin: adın karakterlerinden basit
  bir hash ile ton seç, aynı ad hep aynı rengi versin. Bu küçük JS fonksiyonu
  sayfada gerçekten çalışsın ki kartların tutarlı ve farklı renklendiğini göreyim.
- **Ortak CSS sınıfları kullan, markup'ı tekrar etme.** Süslemeye değil doğruluğa
  harca; animasyon/geçiş efekti gereksiz.
- Sayfanın en altına: yeni bileşenlerin ölçü tablosu + her önemli karar için tek
  cümlelik gerekçe listesi.
- Yanıt uzunluğu sınırına yaklaşırsan **ekranları öncelik sırasına göre bırak**
  ve dosyayı geçerli biçimde bitir; yarım kalan HTML verme. Kaç ekranı
  çizebildiğini sonda belirt.

Türkçe yaz.
